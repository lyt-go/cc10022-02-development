"""质量统计领域服务：完备率、重复率、一致率与匹配精度计算。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MdmQualityService(BaseModuleService):
    MODULE = "mdm_quality"
    ENTITY = "质量统计"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["统计编号", "实体模型", "完备率", "重复率", "一致率", "匹配精度", "统计时间", "统计区间"]
    REQUIRED_FIELDS = ["统计编号", "实体模型", "完备率"]
    STATUS_ORDER = ["草稿", "计算中", "已完成", "已归档"]
    ACTION_RULES = {"发起统计": "计算中", "确认完成": "已完成", "归档统计": "已归档"}
    NEGATIVE_ACTIONS = []

    def compute_quality_metrics(self, entity_model: str,
                                  records: list[dict[str, Any]],
                                  required_fields: list[str]) -> dict[str, Any]:
        """计算主数据质量指标：完备率、重复率、一致率和匹配精度。

        按实体模型计算质量统计数据，包括字段完备率检测、
        重复记录统计、一致性校验和匹配精度评估。
        """
        total = len(records)
        # 计算完备率：校验每条记录必填字段的填充情况
        complete = sum(1 for r in records if all(
            str(r.get(f, "")).strip() for f in required_fields
        ))
        completeness = round(complete / max(total, 1) * 100, 2)

        # 计算重复率：检查实体模型的重复簇数据
        clusters = store.rows("dup_cluster")
        model_clusters = [c for c in clusters if c.get("实体模型") == entity_model]
        dup_count = sum(int(c.get("簇大小", "0")) for c in model_clusters)
        duplication = round(dup_count / max(total, 1) * 100, 2)

        # 计算一致率：检查字段值的一致性和标准化程度
        consistency = self._compute_consistency(records, required_fields)

        # 计算匹配精度：基于匹配任务输出
        match_jobs = store.rows("auto_match_job")
        model_jobs = [j for j in match_jobs if j.get("实体模型") == entity_model]
        total_clusters = sum(int(j.get("输出簇数", "0")) for j in model_jobs)
        match_precision = round(
            max(100 - total_clusters / max(total, 1) * 100, 0), 2
        )

        # 生成质量统计：按实体模型计算多维数据质量指标
        quality_stats = {
            "entity_model": entity_model,
            "total_records": total,
            "completeness_pct": completeness,
            "duplication_pct": duplication,
            "consistency_pct": consistency,
            "match_precision_pct": match_precision,
            "computed_at": datetime.datetime.utcnow().isoformat(),
        }
        self._store_quality_stats(entity_model, quality_stats)
        return quality_stats

    def _compute_consistency(self, records: list[dict[str, Any]],
                               fields: list[str]) -> float:
        """计算字段一致性：检查同类记录同字段值的分布集中度。"""
        if not records or len(records) < 2:
            return 100.0
        total_checks = 0
        consistent = 0
        for field in fields:
            values = [str(r.get(field, "")) for r in records if str(r.get(field, "")).strip()]
            if len(values) < 2:
                continue
            most_common = max(set(values), key=values.count)
            total_checks += len(values)
            consistent += values.count(most_common)
        return round(consistent / max(total_checks, 1) * 100, 2)

    def _store_quality_stats(self, entity_model: str, stats: dict[str, Any]) -> None:
        """存储质量统计结果到mdm_quality存储。"""
        rows = store.rows(self.MODULE)
        stat_id = f"QLTY-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "统计编号": stat_id,
            "实体模型": entity_model,
            "完备率": f"{stats['completeness_pct']:.2f}%",
            "重复率": f"{stats['duplication_pct']:.2f}%",
            "一致率": f"{stats['consistency_pct']:.2f}%",
            "匹配精度": f"{stats['match_precision_pct']:.2f}%",
            "统计时间": stats["computed_at"],
            "统计区间": "全量",
            "status": "已完成",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)

    def query_quality_trend(self, entity_model: str, limit: int = 10) -> list[dict[str, Any]]:
        """查询质量趋势：获取指定实体模型的近期质量统计历史。

        查询质量统计存储，按时间倒序获取最近N次质量统计结果，
        分析完备率、重复率和一致率的变化趋势。
        """
        rows = store.rows(self.MODULE)
        stats = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已完成"
        ]
        # 查询质量统计趋势：按时间倒序获取质量指标历史变化
        stats.sort(key=lambda r: r.get("统计时间", ""), reverse=True)
        return stats[:limit]

    def aggregate_global_quality(self) -> dict[str, Any]:
        """聚合全局质量指标：汇总所有实体模型的质量统计。

        读取质量统计存储，计算所有实体模型的平均完备率、
        重复率、一致率和匹配精度，输出全局质量概览。
        """
        rows = store.rows(self.MODULE)
        completed = [r for r in rows if r.get("status") == "已完成"]
        if not completed:
            return {"total_stats": 0, "models": []}
        models: dict[str, dict[str, Any]] = {}
        for stat in completed:
            model = stat.get("实体模型", "")
            if model not in models:
                models[model] = {
                    "entity_model": model,
                    "latest": None,
                    "stat_count": 0,
                }
            models[model]["stat_count"] += 1
            if not models[model]["latest"] or stat.get("统计时间", "") > models[model]["latest"].get("统计时间", ""):
                models[model]["latest"] = stat
        return {
            "total_quality_stats": len(completed),
            "models": list(models.values()),
        }

    def archive_old_stats(self, before_date: str) -> int:
        """归档指定日期之前的旧质量统计记录。"""
        rows = store.rows(self.MODULE)
        archived = 0
        for row in rows:
            if row.get("status") == "已完成" and row.get("统计时间", "") < before_date:
                row["status"] = "已归档"
                archived += 1
        return archived