"""查询接口领域服务：黄金记录查询、字段标准化输出与调用统计。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MdmApiService(BaseModuleService):
    MODULE = "mdm_api"
    ENTITY = "查询接口"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["接口编号", "实体模型", "查询字段", "返回字段", "缓存时长", "限流阈值", "调用量统计", "接口版本"]
    REQUIRED_FIELDS = ["接口编号", "实体模型", "查询字段"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布接口": "已发布", "下线接口": "已下线"}
    NEGATIVE_ACTIONS = ["下线接口"]

    def query_golden_records_by_api(self, api_id: int,
                                      filters: dict[str, str] | None = None) -> dict[str, Any]:
        """按查询接口定义查询黄金记录：标准化字段输出。

        读取查询接口配置，按查询字段标准对黄金记录执行检索，
        标准化返回字段格式并记录调用量统计。
        """
        api_def = self.get_entry(api_id)
        if not api_def or api_def.get("status") != "已发布":
            return {"ok": False, "reason": "查询接口不可用"}
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        entity_model = api_def.get("实体模型", "")
        query_field = api_def.get("查询字段", "")
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        records = golden_svc.search_golden_records(entity_model=entity_model)
        if filters and query_field:
            records = [
                r for r in records
                if filters.get(query_field, "").casefold() in str(r.get(query_field, "")).casefold()
            ]
        # 标准化查询结果：按接口定义筛选并格式化返回字段
        if return_fields:
            records = [{f: r.get(f) for f in return_fields if f in r} for r in records]
        self._record_api_call(api_id)
        return {"ok": True, "records": records, "count": len(records), "api_id": api_def.get("接口编号")}

    def _record_api_call(self, api_id: int) -> None:
        """记录查询接口调用量统计。"""
        api_def = self.get_entry(api_id)
        if api_def:
            current = api_def.get("调用量统计", "0")
            api_def["调用量统计"] = str(int(current) + 1)

    def query_list_active_apis(self, entity_model: str | None = None) -> list[dict[str, Any]]:
        """查询活跃查询接口列表：按实体模型筛选已发布接口。

        遍历查询接口存储，检索已发布状态的查询接口定义，
        包含查询字段、返回字段、缓存时长和限流阈值配置。
        """
        rows = store.rows(self.MODULE)
        apis = [row for row in rows if row.get("status") == "已发布"]
        if entity_model:
            apis = [a for a in apis if a.get("实体模型") == entity_model]
        # 查询活跃查询接口列表：筛选已发布且匹配实体模型的接口
        return apis

    def validate_api_rate_limit(self, api_id: int) -> dict[str, Any]:
        """校验查询接口限流状态：检查当前调用量是否超过限流阈值。

        读取查询接口的调用量统计和限流阈值，校验是否触发
        限流保护并输出当前调用状态报告。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return {"ok": False, "reason": "接口不存在"}
        call_count = int(api_def.get("调用量统计", "0"))
        rate_limit = int(api_def.get("限流阈值", "100"))
        is_limited = call_count >= rate_limit
        return {
            "api_id": api_def.get("接口编号"),
            "call_count": call_count,
            "rate_limit": rate_limit,
            "is_limited": is_limited,
            "remaining": max(rate_limit - call_count, 0),
        }

    def export_api_response(self, api_id: int,
                              format_type: str = "json") -> list[dict[str, Any]]:
        """导出查询接口响应：按指定格式导出黄金记录查询结果。

        标准化查询接口的输出格式，将黄金记录按字段约定
        转换为JSON/CSV格式供下游系统消费。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return []
        from app.services.golden_record import GoldenRecordService
        golden_svc = GoldenRecordService()
        records = golden_svc.search_golden_records(
            entity_model=api_def.get("实体模型", "")
        )
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        # 导出查询接口响应：标准化输出格式并返回黄金记录
        if return_fields:
            return [{f: r.get(f) for f in return_fields if f in r} for r in records]
        return list(records)