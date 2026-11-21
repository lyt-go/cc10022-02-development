"""导入批次领域服务：批次拆分、分片预检、确认入库与进度统计。

入库分片预检的职责边界：

* ``split_large_batch`` 只做门店数据切分并登记分片，不做任何写库；
* ``precheck_shard`` / ``recheck_shard`` 按入库规则的字段映射做逐片预检，
  产出每条数据的门店编码转换值或拒绝理由，预检阶段绝不写门店目标表；
* 分片可能因失败中断，补跑时按分片+源行幂等去重，已完成记录不重复计入；
* 两个分片一前一后完成时入库规则可能已切换版本，逐片结果各自快照规则版本，
  清单据此暴露版本漂移，导入人凭清单决定能否确认入库；
* 只有 ``confirm_import`` 在确认后才向门店目标表写入记录。
"""
from __future__ import annotations

import csv
import datetime
import io
from typing import Any

from app.services.base import BaseModuleService
from app.services.data_import_rule import (
    STORE_CODE_TARGET_FIELD,
    DataImportRuleService,
)
from app.store import store


def target_module_of(batch: dict[str, Any]) -> str:
    """门店目标表模块名：确认入库时才会被创建并写入。"""
    return f"import_target::{batch.get('实体模型', 'store')}"


class ImportBatchService(BaseModuleService):
    MODULE = "import_batch"
    ENTITY = "导入批次"
    OPERATION_TERM = "导入"
    LIST_FIELDS = ["批次编号", "来源系统", "实体模型", "总记录数", "成功数", "失败数", "重复数", "批次状态"]
    REQUIRED_FIELDS = ["批次编号", "来源系统", "实体模型"]
    STATUS_ORDER = ["待处理", "处理中", "已完成", "已取消"]
    ACTION_RULES = {"开始处理": "处理中", "标记完成": "已完成", "取消导入": "已取消"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._rule_service = DataImportRuleService()

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(str(value))
        except (ValueError, TypeError):
            return default

    # ------------------------------------------------------------------
    # 批次切分
    # ------------------------------------------------------------------
    def split_large_batch(
        self,
        batch_id: int,
        shard_size: int = 1000,
        raw_rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """切分大批次为多个分片，登记逐片预检容器，但不执行预检也不写目标表。

        传入 ``raw_rows`` 时把源数据按分片大小落到各分片（用于真实逐片预检）；
        不传时保留按总记录数估算的兼容行为。每个分片维护自己的预检结果与
        规则版本快照，互不影响。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return []
        total = self._safe_int(batch.get("总记录数", "0"))
        if raw_rows:
            total = len(raw_rows)
            batch["总记录数"] = str(total)
        effective_size = max(shard_size, 1)
        num_shards = max((total + effective_size - 1) // effective_size, 1)
        shards: list[dict[str, Any]] = []
        for i in range(num_shards):
            if raw_rows:
                start, end = i * effective_size, min((i + 1) * effective_size, total)
                shard_rows = [dict(row) for row in raw_rows[start:end]]
            else:
                shard_rows = []
            shard = {
                "shard_index": i + 1,
                "shard_total": num_shards,
                "parent_batch": batch.get("批次编号"),
                "source_system": batch.get("来源系统"),
                "entity_model": batch.get("实体模型"),
                "shard_size": len(shard_rows) if raw_rows else min(effective_size, total - i * effective_size),
                "status": "切分完成",
                "rule_version": None,
                "rule_id": None,
                "processed_keys": [],
                "precheck_results": [],
                "raw_rows": shard_rows,
                "attempts": 0,
            }
            shards.append(shard)
        batch["shards"] = shards
        batch["shard_count"] = num_shards
        batch["precheck_status"] = "待预检"
        batch["confirmed"] = False
        return shards

    def _find_shard(self, batch: dict[str, Any], shard_index: int) -> dict[str, Any] | None:
        for shard in batch.get("shards", []):
            if int(shard.get("shard_index", -1)) == shard_index:
                return shard
        return None

    @staticmethod
    def _row_key(shard_index: int, row_no: int) -> str:
        return f"S{shard_index}-R{row_no}"

    # ------------------------------------------------------------------
    # 逐片预检（只读，不写门店目标表）
    # ------------------------------------------------------------------
    def precheck_shard(
        self,
        batch_id: int,
        shard_index: int,
        *,
        rule_id: int | None = None,
        fail_after: int | None = None,
    ) -> dict[str, Any]:
        """对单个分片执行逐行预检。

        幂等：以「分片+源行」为处理键，补跑因失败中断的分片时，已完成的记录
        直接跳过、不再次计入结果。``fail_after`` 用于模拟处理到第 N 行后中断。
        预检全程只计算门店编码转换值/拒绝理由，不写任何目标表。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        shard = self._find_shard(batch, shard_index)
        if shard is None:
            return {"ok": False, "message": f"分片 {shard_index} 不存在"}
        if batch.get("confirmed"):
            return {"ok": False, "message": "批次已确认入库，预检结果已锁定"}

        rule = self._rule_service.resolve_active_rule(
            str(batch.get("来源系统", "")), str(batch.get("实体模型", "")), rule_id
        )
        if rule is None:
            shard["status"] = "预检失败"
            return {"ok": False, "message": "未找到已生效的入库规则，分片预检无法执行"}

        # 记录预检前的目标表行数，用于自证预检不写目标表；用 table_size
        # 避免一次只读查询就凭空创建目标表。
        target_module = target_module_of(batch)
        target_size_before = store.table_size(target_module)

        shard["attempts"] = int(shard.get("attempts", 0)) + 1
        processed: set[str] = set(shard.get("processed_keys", []))
        results: list[dict[str, Any]] = list(shard.get("precheck_results", []))
        raw_rows: list[dict[str, Any]] = shard.get("raw_rows", [])
        already_done = len(processed)  # 本次补跑前已完成的记录数

        processed_this_run = 0
        interrupted = False
        for offset, record in enumerate(raw_rows, start=1):
            key = self._row_key(shard_index, offset)
            if key in processed:
                # 补跑：已完成记录跳过，绝不重复计入。
                continue
            source_ref = f"{batch.get('批次编号')}/分片{shard_index}/源第{offset}行"
            preview = self._rule_service.preview_record(
                record, rule, shard_index=shard_index, row_no=offset, source_ref=source_ref
            )
            preview["row_key"] = key
            results.append(preview)
            processed.add(key)
            processed_this_run += 1
            if fail_after is not None and processed_this_run >= fail_after:
                interrupted = True
                break

        shard["processed_keys"] = sorted(processed)
        shard["precheck_results"] = results
        # 快照本分片使用的规则版本：两个分片可能命中不同版本。
        shard["rule_id"] = rule.get("规则编号")
        shard["rule_version"] = rule.get("规则版本")
        if interrupted:
            shard["status"] = "预检中断"
        elif len(processed) >= len(raw_rows):
            shard["status"] = "预检完成"
        else:
            shard["status"] = "预检中断"

        target_size_after = store.table_size(target_module)
        batch["precheck_status"] = self._overall_precheck_status(batch)
        return {
            "ok": True,
            "message": "分片预检中断，可补跑剩余记录" if interrupted else "分片预检完成",
            "interrupted": interrupted,
            "shard_index": shard_index,
            "rule_id": rule.get("规则编号"),
            "rule_version": rule.get("规则版本"),
            "processed_total": len(processed),
            "processed_this_run": processed_this_run,
            "skipped_already_done": already_done,
            "accepted": sum(1 for r in results if r.get("accepted")),
            "rejected": sum(1 for r in results if not r.get("accepted")),
            "target_writes": target_size_after - target_size_before,
        }

    def recheck_shard(
        self,
        batch_id: int,
        shard_index: int,
        *,
        rule_id: int | None = None,
    ) -> dict[str, Any]:
        """修订源数据后复查整片：清空该分片旧预检结果后重新预检。

        复查只针对指定分片，其他分片的结果与版本快照保持不变。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        shard = self._find_shard(batch, shard_index)
        if shard is None:
            return {"ok": False, "message": f"分片 {shard_index} 不存在"}
        shard["processed_keys"] = []
        shard["precheck_results"] = []
        shard["status"] = "切分完成"
        return self.precheck_shard(batch_id, shard_index, rule_id=rule_id)

    @staticmethod
    def _overall_precheck_status(batch: dict[str, Any]) -> str:
        shards = batch.get("shards", [])
        if not shards:
            return "待预检"
        statuses = {s.get("status") for s in shards}
        if statuses == {"预检完成"}:
            return "预检完成"
        if "预检失败" in statuses and len(statuses) == 1:
            return "预检失败"
        if "预检中断" in statuses or any(s.get("precheck_results") for s in shards):
            return "预检中"
        return "待预检"

    # ------------------------------------------------------------------
    # 预检清单汇总
    # ------------------------------------------------------------------
    def get_precheck_checklist(self, batch_id: int) -> dict[str, Any]:
        """把逐片预检结果汇成导入人可确认的导入清单。

        每条记录带门店编码转换值或拒绝理由，并标注来源分片与命中的规则版本；
        跨分片规则版本不一致时给出版本漂移提示，由导入人决定是否确认入库。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        shards = batch.get("shards", [])
        items: list[dict[str, Any]] = []
        versions: set[str] = set()
        for shard in shards:
            if shard.get("rule_version"):
                versions.add(str(shard.get("rule_version")))
            for result in shard.get("precheck_results", []):
                items.append({
                    "row_key": result.get("row_key"),
                    "source_ref": result.get("source_ref"),
                    "shard_index": result.get("shard_index"),
                    "row_no": result.get("row_no"),
                    "rule_id": result.get("rule_id"),
                    "rule_version": result.get("rule_version"),
                    "store_code_field": result.get("store_code_field"),
                    "store_code_value": result.get("store_code_value", ""),
                    "mapped_record": result.get("mapped_record", {}),
                    "accepted": bool(result.get("accepted")),
                    "reject_reason": result.get("reject_reason", ""),
                })
        items.sort(key=lambda item: (int(item["shard_index"]), int(item["row_no"])))
        accepted = sum(1 for item in items if item["accepted"])
        rejected = len(items) - accepted
        version_drift = len(versions) > 1
        shard_summary = [
            {
                "shard_index": shard.get("shard_index"),
                "status": shard.get("status"),
                "rule_version": shard.get("rule_version"),
                "processed": len(shard.get("processed_keys", [])),
                "shard_size": shard.get("shard_size"),
                "accepted": sum(1 for r in shard.get("precheck_results", []) if r.get("accepted")),
                "rejected": sum(1 for r in shard.get("precheck_results", []) if not r.get("accepted")),
                "attempts": shard.get("attempts", 0),
            }
            for shard in shards
        ]
        confirmable = (
            bool(items)
            and all(s.get("status") == "预检完成" for s in shards)
            and rejected == 0
            and not version_drift
            and not batch.get("confirmed")
        )
        blockers: list[str] = []
        if not shards:
            blockers.append("批次尚未切分，无分片预检结果")
        elif not items:
            blockers.append("各分片尚未产出预检结果，请先执行逐片预检")
        pending = [s for s in shards if s.get("status") != "预检完成"]
        if pending:
            blockers.append("存在未完成预检的分片：" + "、".join(f"分片{s['shard_index']}({s['status']})" for s in pending))
        if rejected:
            blockers.append(f"有 {rejected} 条记录预检被拒，需修订后复查")
        if version_drift:
            blockers.append(f"分片命中的入库规则版本不一致（{ '、'.join(sorted(versions)) }），存在版本漂移")
        if batch.get("confirmed"):
            blockers.append("批次已确认入库")
        return {
            "ok": True,
            "batch_id": batch.get("批次编号"),
            "precheck_status": batch.get("precheck_status", "待预检"),
            "total_rows": len(items),
            "accepted": accepted,
            "rejected": rejected,
            "rule_versions": sorted(versions),
            "version_drift": version_drift,
            "confirmable": confirmable,
            "blockers": blockers,
            "shards": shard_summary,
            "items": items,
        }

    def export_precheck_report(self, batch_id: int) -> dict[str, Any]:
        """生成导入预检文件（CSV 文本）。

        被拒记录（例如上传的 N-10 在某片没映射出门店编码）必须能定位原数据行
        及命中的规则版本，供导入人修订后复查。
        """
        checklist = self.get_precheck_checklist(batch_id)
        if not checklist.get("ok"):
            return checklist
        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "处理键", "原数据定位", "分片", "源行号", "规则编号", "规则版本",
            "目标字段", "门店编码转换值", "预检结论", "拒绝理由",
        ])
        for item in checklist["items"]:
            writer.writerow([
                item["row_key"],
                item["source_ref"],
                item["shard_index"],
                item["row_no"],
                item["rule_id"],
                item["rule_version"],
                item["store_code_field"],
                item["store_code_value"],
                "通过" if item["accepted"] else "拒绝",
                item["reject_reason"],
            ])
        return {
            "ok": True,
            "filename": f"precheck_{checklist['batch_id']}.csv",
            "media_type": "text/csv; charset=utf-8",
            "content": output.getvalue(),
            "total": checklist["total_rows"],
            "rejected": checklist["rejected"],
        }

    # ------------------------------------------------------------------
    # 确认入库：唯一允许写门店目标表的环节
    # ------------------------------------------------------------------
    def confirm_import(self, batch_id: int) -> dict[str, Any]:
        """导入人确认后，才把预检通过的记录写入门店目标表。

        预检阶段不写库；此处二次校验清单可确认（全片完成、无拒绝、无版本漂移），
        通过后仅写入 accepted 记录，并按门店编码去重。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return {"ok": False, "message": "批次不存在"}
        checklist = self.get_precheck_checklist(batch_id)
        if not checklist.get("confirmable"):
            return {
                "ok": False,
                "message": "预检清单不满足确认入库条件",
                "blockers": checklist.get("blockers", []),
            }

        target_module = target_module_of(batch)
        target_rows = store.rows(target_module)
        existing_codes = {
            str(row.get(STORE_CODE_TARGET_FIELD))
            for row in target_rows
            if row.get(STORE_CODE_TARGET_FIELD) is not None
        }
        written, duplicated = 0, 0
        for item in checklist["items"]:
            if not item["accepted"]:
                continue
            code = str(item["store_code_value"])
            if code in existing_codes:
                duplicated += 1
                continue
            entry = {"id": max((int(r.get("id", 0)) for r in target_rows), default=0) + 1}
            entry.update(item["mapped_record"])
            entry[STORE_CODE_TARGET_FIELD] = code
            entry["来源批次"] = batch.get("批次编号")
            entry["规则版本"] = item["rule_version"]
            target_rows.append(entry)
            existing_codes.add(code)
            written += 1

        batch["confirmed"] = True
        batch["confirm_at"] = datetime.datetime.utcnow().isoformat()
        batch["precheck_status"] = "已确认入库"
        for shard in batch.get("shards", []):
            shard["status"] = "已入库"
        batch["成功数"] = str(checklist["accepted"] - duplicated)
        batch["失败数"] = str(checklist["rejected"])
        batch["重复数"] = str(duplicated)
        batch["批次状态"] = "已完成"
        batch["status"] = "已完成"
        batch["pending"] = False
        return {
            "ok": True,
            "message": "确认入库完成",
            "target_module": target_module,
            "written": written,
            "duplicated": duplicated,
            "rejected": checklist["rejected"],
        }

    # ------------------------------------------------------------------
    # 既有统计能力（保留原行为）
    # ------------------------------------------------------------------
    def import_batch_from_source(self, source_system: str, entity_model: str,
                                  raw_records: int) -> dict[str, Any]:
        """导入数据批次：从来源系统采集数据并入库到目标实体模型。

        导入批次数据到import_batch存储，包含来源系统、实体模型、
        总记录数、成功数、失败数和重复数统计，并标记批次状态。
        """
        batch_id = f"BATCH-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        total = raw_records
        # 导入批次数据：采集来源系统记录，执行数据校验和入库
        success_count = max(int(total * 0.92), 0)  # 模拟92%成功率
        failed_count = total - success_count
        duplicate_count = max(int(success_count * 0.06), 0)  # 模拟6%重复率
        import_batch = {
            "批次编号": batch_id,
            "来源系统": source_system,
            "实体模型": entity_model,
            "总记录数": str(total),
            "成功数": str(success_count),
            "失败数": str(failed_count),
            "重复数": str(duplicate_count),
            "批次状态": "已完成",
        }
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update(import_batch)
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = failed_count > 0
        rows.append(entry)
        return entry

    def query_batch_summary(self, source_system: str | None = None) -> dict[str, Any]:
        """查询导入批次汇总统计，包含成功数、失败数和重复数聚合。

        聚合查询导入批次存储中的所有批次数据，按来源系统分组汇总
        总记录数、成功数、失败数和重复数，输出批次质量概览。
        """
        rows = store.rows(self.MODULE)
        if source_system:
            rows = [r for r in rows if r.get("来源系统") == source_system]
        total_records = sum(self._safe_int(r.get("总记录数", "0")) for r in rows)
        total_success = sum(self._safe_int(r.get("成功数", "0")) for r in rows)
        total_failed = sum(self._safe_int(r.get("失败数", "0")) for r in rows)
        total_duplicate = sum(self._safe_int(r.get("重复数", "0")) for r in rows)
        return {
            "total_batches": len(rows),
            "total_records": total_records,
            "total_success": total_success,
            "total_failed": total_failed,
            "total_duplicate": total_duplicate,
            "success_rate": round(total_success / max(total_records, 1) * 100, 2),
        }

    def validate_batch_integrity(self, batch_id: int) -> dict[str, Any]:
        """校验导入批次的完整性：成功+失败+重复数是否等于总记录数。

        读取导入批次数据，校验总记录数是否等于成功数+失败数+重复数，
        验证批次数据的完整性并生成校验报告。
        """
        batch = self.get_entry(batch_id)
        if not batch:
            return {"ok": False, "reason": "批次不存在"}
        total = int(batch.get("总记录数", "0"))
        success = int(batch.get("成功数", "0"))
        failed = int(batch.get("失败数", "0"))
        duplicate = int(batch.get("重复数", "0"))
        consistent = total == (success + failed + duplicate)
        return {
            "batch_id": batch.get("批次编号"),
            "integrity_ok": consistent,
            "total": total,
            "calculated": success + failed + duplicate,
        }

    def archive_completed_batches(self, before_date: str) -> int:
        """归档指定日期之前已完成的导入批次数据。

        查询导入批次存储，将已完成且时间在指定日期之前的批次
        标记为归档状态，释放活跃存储空间供新批次使用。
        """
        rows = store.rows(self.MODULE)
        archived = 0
        for row in rows:
            if row.get("status") == "已完成":
                row["status"] = "已归档"
                row["archived_at"] = datetime.datetime.utcnow().isoformat()
                archived += 1
        return archived
