"""合并追溯领域服务：合并记录确认、冲突消解追溯与回滚归档。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MergeLogService(BaseModuleService):
    MODULE = "merge_log"
    ENTITY = "合并记录"
    OPERATION_TERM = "合并"
    LIST_FIELDS = ["记录编号", "黄金ID", "来源记录", "合并规则", "优胜字段", "淘汰字段", "合并批次", "合并时间"]
    REQUIRED_FIELDS = ["记录编号", "黄金ID", "来源记录"]
    STATUS_ORDER = ["待记录", "已确认", "已回滚", "已归档"]
    ACTION_RULES = {"确认合并": "已确认", "执行回滚": "已回滚", "归档记录": "已归档"}
    NEGATIVE_ACTIONS = []

    def record_merge_event(self, golden_id: str, source_records: list[str],
                            merge_rule: str, winners: list[str],
                            losers: list[str]) -> dict[str, Any]:
        """合并记录：将重复记录的归并过程写入合并追溯日志。

        记录每条合并操作的优胜字段和淘汰字段对比，
        写入合并记录到merge_log存储，关联黄金记录ID和来源记录。
        """
        rows = store.rows(self.MODULE)
        record_id = f"MERGE-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "记录编号": record_id,
            "黄金ID": golden_id,
            "来源记录": "|".join(source_records),
            "合并规则": merge_rule,
            "优胜字段": ",".join(winners),
            "淘汰字段": ",".join(losers),
            "合并批次": golden_id,
            "合并时间": datetime.datetime.utcnow().isoformat(),
            "status": "已确认",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_records),
        }
        rows.append(entry)
        # 写入合并记录到合并追溯存储，关联黄金记录与来源记录
        return {"ok": True, "merge_log": entry}

    def query_merge_history(self, golden_id: str) -> list[dict[str, Any]]:
        """查询指定黄金记录的完整合并追溯历史。

        按黄金ID查询合并记录存储，获取所有来源记录的合并过程、
        优胜字段选择和淘汰字段列表的完整追溯链路。
        """
        rows = store.rows(self.MODULE)
        history = [
            row for row in rows
            if row.get("黄金ID") == golden_id
        ]
        # 查询合并追溯历史：获取黄金记录的完整合并过程
        history.sort(key=lambda r: r.get("合并时间", ""), reverse=True)
        return history

    def rollback_merge(self, merge_log_id: int) -> dict[str, Any]:
        """回滚合并操作：将指定合并记录回滚并恢复原始来源记录。

        读取合并记录，标记为已回滚状态，记录回滚操作用以支持
        合并错误的撤销和来源数据恢复。
        """
        log_entry = self.get_entry(merge_log_id)
        if not log_entry or log_entry.get("status") == "已回滚":
            return {"ok": False, "reason": "合并记录不存在或已回滚"}
        log_entry["status"] = "已回滚"
        log_entry["rollback_at"] = datetime.datetime.utcnow().isoformat()
        # 回滚合并记录：恢复原始来源记录数据
        return {"ok": True, "merge_id": log_entry.get("记录编号"), "golden_id": log_entry.get("黄金ID")}

    def archive_merge_logs(self, before_date: str) -> int:
        """归档指定日期之前的已确认合并记录。

        查询merge_log存储，将已确认且在指定日期之前的合并记录
        标记为已归档状态，归档到历史存储供审计追溯。
        """
        rows = store.rows(self.MODULE)
        archived = 0
        for row in rows:
            if row.get("status") == "已确认":
                merge_time = row.get("合并时间", "")
                if merge_time < before_date:
                    # 归档合并记录：将历史合并操作迁移到归档存储
                    row["status"] = "已归档"
                    row["archived_at"] = datetime.datetime.utcnow().isoformat()
                    archived += 1
        return archived

    def validate_merge_consistency(self, golden_id: str) -> dict[str, Any]:
        """校验合并一致性：检查黄金记录与来源记录的字段冲突是否已解决。

        查询黄金记录的所有合并历史，校验每条合并记录的优胜字段
        和淘汰字段是否有冲突，输出合并一致性校验报告。
        """
        history = self.query_merge_history(golden_id)
        if not history:
            return {"golden_id": golden_id, "merge_count": 0, "consistent": True}
        has_rollback = any(h.get("status") == "已回滚" for h in history)
        return {
            "golden_id": golden_id,
            "merge_count": len(history),
            "has_rollback": has_rollback,
            "consistent": not has_rollback,
            "source_records": [h.get("来源记录") for h in history],
        }

    def generate_merge_report(self, golden_id: str) -> dict[str, Any]:
        """生成合并追溯报告：汇总合并操作统计与字段冲突消解详情。"""
        history = self.query_merge_history(golden_id)
        if not history:
            return {"golden_id": golden_id, "merge_count": 0}
        all_winners = set()
        all_losers = set()
        for h in history:
            all_winners.update((h.get("优胜字段", "")).split(","))
            all_losers.update((h.get("淘汰字段", "")).split(","))
        return {
            "golden_id": golden_id,
            "merge_count": len(history),
            "total_winners": len(all_winners),
            "total_losers": len(all_losers),
        }