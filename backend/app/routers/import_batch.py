"""批次导入分片预检专用接口。

这些路径固定在通用资源路由之前注册，避免被 ``/api/{module}`` 动态匹配抢先。
所有写门店目标表的动作只发生在确认入库接口；预检类接口一律只读。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.import_batch import ImportBatchService

router = APIRouter(prefix="/api/import_batch", tags=["批次导入预检"])

service = ImportBatchService()


class SplitPayload(BaseModel):
    shard_size: int = Field(default=1000, ge=1)
    raw_rows: list[dict[str, Any]] = Field(default_factory=list)


class PrecheckPayload(BaseModel):
    shard_index: int = Field(ge=1)
    rule_id: int | None = None
    fail_after: int | None = Field(default=None, ge=1)


class RecheckPayload(BaseModel):
    shard_index: int = Field(ge=1)
    rule_id: int | None = None


@router.post("/{batch_id}/split")
def split_batch(batch_id: int, payload: SplitPayload) -> dict[str, Any]:
    shards = service.split_large_batch(
        batch_id, payload.shard_size, raw_rows=payload.raw_rows or None
    )
    if not shards:
        return {"ok": False, "message": f"导入批次 {batch_id} 不存在，无法切分"}
    return {"ok": True, "message": f"已切分为 {len(shards)} 个分片", "shards": shards}


@router.post("/{batch_id}/precheck")
def precheck_shard(batch_id: int, payload: PrecheckPayload) -> dict[str, Any]:
    return service.precheck_shard(
        batch_id,
        payload.shard_index,
        rule_id=payload.rule_id,
        fail_after=payload.fail_after,
    )


@router.post("/{batch_id}/recheck")
def recheck_shard(batch_id: int, payload: RecheckPayload) -> dict[str, Any]:
    return service.recheck_shard(batch_id, payload.shard_index, rule_id=payload.rule_id)


@router.get("/{batch_id}/precheck/checklist")
def precheck_checklist(batch_id: int) -> dict[str, Any]:
    return service.get_precheck_checklist(batch_id)


@router.get("/{batch_id}/precheck/export", response_model=None)
def export_precheck(batch_id: int):
    """下载导入预检文件；批次不存在时返回 JSON 错误，否则返回 CSV 流。"""
    report = service.export_precheck_report(batch_id)
    if not report.get("ok"):
        return report
    return StreamingResponse(
        iter([report["content"]]),
        media_type=report["media_type"],
        headers={"Content-Disposition": f'attachment; filename="{report["filename"]}"'},
    )


@router.post("/{batch_id}/confirm")
def confirm_import(batch_id: int) -> dict[str, Any]:
    return service.confirm_import(batch_id)
