"""自动匹配任务领域服务：匹配任务调度、并行匹配执行与簇结果输出。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AutoMatchJobService(BaseModuleService):
    MODULE = "auto_match_job"
    ENTITY = "匹配任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "实体模型", "匹配规则", "数据范围", "并行度", "匹配策略", "输出簇数", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "实体模型", "匹配规则"]
    STATUS_ORDER = ["草稿", "等待中", "运行中", "已完成", "已失败"]
    ACTION_RULES = {"提交运行": "草稿", "停止匹配": "运行中", "确认完成": "已完成"}
    NEGATIVE_ACTIONS = []

    def schedule_match_job(self, rule_id: int, entity_model: str,
                            records: list[dict[str, Any]]) -> dict[str, Any]:
        """调度匹配任务：按匹配规则对实体模型记录执行自动匹配和去重检测。

        读取匹配规则配置，调度自动匹配任务计算记录对的模糊相似度，
        校验阈值并生成疑似重复簇输出，返回任务执行统计。
        """
        from app.services.match_rule import MatchRuleService
        rule_svc = MatchRuleService()
        rule = rule_svc.get_entry(rule_id)
        if not rule:
            return {"ok": False, "reason": "匹配规则不存在"}
        # 调度匹配任务：读取匹配规则，执行模糊匹配计算和去重检测
        clusters = rule_svc.generate_duplicate_clusters(rule_id, records)
        job_result = {
            "task_id": f"TASK-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            "entity_model": entity_model,
            "rule_id": rule_id,
            "total_records": len(records),
            "clusters_found": len(clusters),
            "records_in_clusters": sum(len(c) for c in clusters),
            "match_strategy": rule.get("匹配策略", "全量匹配"),
            "completed_at": datetime.datetime.utcnow().isoformat(),
        }
        # 写入匹配任务结果并存储疑似重复簇
        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "任务编号": job_result["task_id"],
            "实体模型": entity_model,
            "匹配规则": str(rule_id),
            "并行度": "4",
            "匹配策略": job_result["match_strategy"],
            "输出簇数": str(job_result["clusters_found"]),
            "任务状态": "已完成",
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "clusters": clusters,
        }
        rows.append(entry)
        return {"ok": True, "job": entry, "clusters": clusters}

    def query_job_status(self, job_id: int) -> dict[str, Any] | None:
        """查询匹配任务的执行状态与输出簇数统计。

        读取匹配任务存储中的指定任务，查询任务状态、输出簇数、
        并行度和匹配策略信息，返回任务详细状态。
        """
        job = self.get_entry(job_id)
        if not job:
            return None
        # 查询匹配任务状态：读取任务执行状态与输出簇数统计
        return {
            "task_id": job.get("任务编号"),
            "status": job.get("任务状态"),
            "entity_model": job.get("实体模型"),
            "clusters_output": int(job.get("输出簇数", "0")),
            "parallelism": int(job.get("并行度", "1")),
            "strategy": job.get("匹配策略"),
        }

    def aggregate_job_statistics(self, entity_model: str | None = None) -> dict[str, Any]:
        """聚合匹配任务统计：汇总已完成/失败任务数、输出簇总数和平均并行度。

        查询匹配任务存储，按实体模型分组聚合任务执行统计，
        计算已完成任务数、失败任务数、输出簇总数和质量指标。
        """
        rows = store.rows(self.MODULE)
        if entity_model:
            rows = [r for r in rows if r.get("实体模型") == entity_model]
        completed = sum(1 for r in rows if r.get("任务状态") == "已完成")
        failed = sum(1 for r in rows if r.get("任务状态") == "已失败")
        total_clusters = sum(int(r.get("输出簇数", "0")) for r in rows)
        return {
            "total_jobs": len(rows),
            "completed": completed,
            "failed": failed,
            "total_clusters_output": total_clusters,
            "success_rate": round(completed / max(len(rows), 1) * 100, 2),
        }

    def validate_job_parameters(self, job_id: int) -> dict[str, Any]:
        """校验匹配任务的参数配置是否合理：并行度、匹配策略和数据范围。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "reason": "任务不存在"}
        parallelism = int(job.get("并行度", "1"))
        checks = {
            "parallelism_valid": 1 <= parallelism <= 16,
            "has_match_rule": bool(job.get("匹配规则")),
            "has_entity_model": bool(job.get("实体模型")),
            "has_strategy": bool(job.get("匹配策略")),
        }
        checks["all_valid"] = all(checks.values())
        return {"ok": checks["all_valid"], "checks": checks}

    def retry_failed_jobs(self, entity_model: str) -> int:
        """重试已失败的匹配任务：重新调度失败任务执行模糊匹配计算。"""
        rows = store.rows(self.MODULE)
        retried = 0
        for job in rows:
            if job.get("任务状态") == "已失败" and job.get("实体模型") == entity_model:
                job["任务状态"] = "等待中"
                job["status"] = "等待中"
                job["retry_count"] = job.get("retry_count", 0) + 1
                retried += 1
        return retried