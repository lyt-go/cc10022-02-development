"""记录级血缘领域服务：字段路径关联、变换追溯与血缘图谱构建。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RecordLineageService(BaseModuleService):
    MODULE = "record_lineage"
    ENTITY = "血缘记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["血缘编号", "黄金ID", "来源记录", "来源系统", "字段路径", "变换记录", "生效时间", "失效时间"]
    REQUIRED_FIELDS = ["血缘编号", "黄金ID", "来源记录"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def record_field_lineage(self, golden_id: str, source_record_id: str,
                               source_system: str, field_path: str,
                               transformations: list[str]) -> dict[str, Any]:
        """记录字段级血缘：将黄金记录字段关联到原始来源记录的字段路径。

        关联血缘记录到record_lineage存储，记录每个黄金字段的完整
        来源路径和变换历史，构建端到端的字段级追溯链路。
        """
        rows = store.rows(self.MODULE)
        lineage_id = f"LINE-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "血缘编号": lineage_id,
            "黄金ID": golden_id,
            "来源记录": source_record_id,
            "来源系统": source_system,
            "字段路径": field_path,
            "变换记录": "|".join(transformations),
            "生效时间": datetime.datetime.utcnow().isoformat(),
            "失效时间": "",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "transform_count": len(transformations),
        }
        rows.append(entry)
        # 关联血缘记录：建立黄金字段到原始来源的完整追溯链路
        return {"ok": True, "lineage": entry}

    def query_field_lineage(self, golden_id: str) -> list[dict[str, Any]]:
        """查询指定黄金记录的所有字段级血缘关联。

        按黄金ID查询血缘记录存储，获取每条字段从来源系统、
        原始记录到黄金记录的完整关联路径和变换历史。
        """
        rows = store.rows(self.MODULE)
        lineage = [
            row for row in rows
            if row.get("黄金ID") == golden_id and row.get("status") == "已生效"
        ]
        # 查询血缘关联：获取黄金记录的完整字段级追溯路径
        lineage.sort(key=lambda r: r.get("生效时间", ""))
        return lineage

    def build_lineage_graph(self, golden_id: str) -> dict[str, Any]:
        """构建血缘图谱：基于字段级血缘记录生成DAG关系图。

        查询黄金记录的所有血缘记录，关联来源系统和字段路径信息，
        构建从来源记录到黄金记录的完整血缘有向无环图。
        """
        lineages = self.query_field_lineage(golden_id)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        visited = set()
        for lineage in lineages:
            source = lineage.get("来源记录", "")
            field = lineage.get("字段路径", "")
            if source and source not in visited:
                visited.add(source)
                nodes.append({"id": source, "type": "source_record", "system": lineage.get("来源系统")})
            field_node_id = f"{golden_id}:{field}"
            if field_node_id not in visited:
                visited.add(field_node_id)
                nodes.append({"id": field_node_id, "type": "golden_field", "field": field})
            edges.append({
                "from": source,
                "to": field_node_id,
                "transformations": (lineage.get("变换记录", "")).split("|"),
            })
        # 构建血缘图谱：关联来源记录和字段路径生成DAG关系图
        return {"golden_id": golden_id, "nodes": nodes, "edges": edges}

    def validate_lineage_completeness(self, golden_id: str,
                                       expected_sources: list[str]) -> dict[str, Any]:
        """校验血缘完整性：确认黄金记录的每条来源都有字段级血缘关联。

        查询血缘记录存储，校验每个预期来源是否都有对应的字段
        路径血缘记录，输出血缘完备性校验报告。
        """
        lineages = self.query_field_lineage(golden_id)
        covered = set()
        for lineage in lineages:
            covered.add(lineage.get("来源记录", ""))
        missing = [s for s in expected_sources if s not in covered]
        return {
            "golden_id": golden_id,
            "expected_sources": len(expected_sources),
            "lineage_entries": len(lineages),
            "covered_sources": len(covered),
            "missing_sources": missing,
            "completeness": round(len(covered) / max(len(expected_sources), 1) * 100, 2),
        }

    def expire_lineage_entries(self, golden_id: str, expire_reason: str) -> int:
        """失效血缘记录：当黄金记录更新时标记旧血缘为失效状态。

        将指定黄金记录的所有已生效血缘记录标记为已停用，
        并设置失效时间，保留历史追溯但不再用于当前关联图谱。
        """
        rows = store.rows(self.MODULE)
        expired = 0
        for row in rows:
            if row.get("黄金ID") == golden_id and row.get("status") == "已生效":
                row["status"] = "已停用"
                row["失效时间"] = datetime.datetime.utcnow().isoformat()
                row["expire_reason"] = expire_reason
                expired += 1
        return expired

    def export_lineage_trace(self, golden_id: str) -> dict[str, Any]:
        """导出血缘追溯：将黄金记录的完整字段级血缘导出供审计分析。"""
        lineages = self.query_field_lineage(golden_id)
        trace: dict[str, list[dict[str, Any]]] = {}
        for lineage in lineages:
            field = lineage.get("字段路径", "")
            if field not in trace:
                trace[field] = []
            trace[field].append({
                "source_system": lineage.get("来源系统"),
                "source_record": lineage.get("来源记录"),
                "transformations": (lineage.get("变换记录", "")).split("|"),
                "effective_time": lineage.get("生效时间"),
            })
        # 导出血缘追溯：构建字段级完整来源追溯链路
        return {"golden_id": golden_id, "field_trace": trace, "total_entries": len(lineages)}

    def query_lineage_by_system(self, source_system: str) -> list[dict[str, Any]]:
        """查询给定来源系统影响的所有黄金记录，反向追溯血缘。"""
        rows = store.rows(self.MODULE)
        return [
            row for row in rows
            if row.get("来源系统") == source_system and row.get("status") == "已生效"
        ]