"""优胜策略领域服务：字段冲突裁决、来源权重排序与优先级回退。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SurvivorshipRuleService(BaseModuleService):
    MODULE = "survivorship_rule"
    ENTITY = "优胜策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "实体模型", "选择字段", "判定方式", "来源优先级", "时间优先级", "回退策略", "策略版本"]
    REQUIRED_FIELDS = ["策略编号", "实体模型", "选择字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def resolve_field_conflict(self, rule_id: int, field_name: str,
                                candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """按优胜策略解决字段冲突：从多个候选值中选择最优值。

        读取优胜策略配置，按判定方式、来源优先级和时间优先级
        对冲突字段的候选值进行排名，选择优胜字段值并记录落选值。
        """
        rule = self.get_entry(rule_id)
        if not rule or not candidates:
            return {"field": field_name, "winner": None, "losers": [], "reason": "无候选值"}
        method = rule.get("判定方式", "最长值优先")
        source_order = [s.strip() for s in rule.get("来源优先级", "").split(",") if s.strip()]
        fallback = rule.get("回退策略", "保留原始值")

        winner_idx = 0
        if method == "来源优先级":
            for idx, candidate in enumerate(candidates):
                src = candidate.get("_source", "")
                if src in source_order and source_order.index(src) < (
                    source_order.index(candidates[winner_idx].get("_source", "")) if candidates[winner_idx].get("_source", "") in source_order else len(source_order)
                ):
                    winner_idx = idx
        elif method == "时间优先级":
            candidates_sorted = sorted(
                [(i, c) for i, c in enumerate(candidates)],
                key=lambda x: x[1].get(rule.get("时间优先级", "_timestamp"), ""),
                reverse=True,
            )
            winner_idx = candidates_sorted[0][0]
        else:
            # 最长值优先策略：选择最长非空值
            winner_idx = max(
                range(len(candidates)),
                key=lambda i: len(str(candidates[i].get("value", ""))) if candidates[i].get("value") else 0,
            )

        winner = candidates[winner_idx]
        losers = [c for i, c in enumerate(candidates) if i != winner_idx]
        # 优胜策略裁决：按判定方式选择最优字段值并记录落选候选
        return {
            "field": field_name,
            "winner": winner,
            "losers": losers,
            "rule_method": method,
            "fallback": fallback,
        }

    def apply_all_survivorship_rules(self, entity_model: str,
                                      merged_record: dict[str, Any],
                                      candidates_map: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """对所有冲突字段应用优胜策略，产出最终黄金记录字段集。"""
        rows = store.rows(self.MODULE)
        rules = [
            r for r in rows
            if r.get("实体模型") == entity_model and r.get("status") == "已生效"
        ]
        for rule in rules:
            field = rule.get("选择字段", "")
            if field in candidates_map:
                result = self.resolve_field_conflict(
                    rule["id"], field, candidates_map[field]
                )
                # 优胜策略应用：对冲突字段按规则选择最优值
                if result["winner"]:
                    merged_record[field] = result["winner"].get("value")
                    merged_record[f"_survivorship_{field}"] = result
        return merged_record

    def query_active_survivorship_rules(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定实体模型的所有已生效优胜策略。

        遍历优胜策略存储，筛选已生效且匹配实体模型的策略，
        按策略版本排序返回供字段冲突裁决使用。
        """
        rows = store.rows(self.MODULE)
        rules = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已生效"
        ]
        # 查询优胜策略并按版本排序
        rules.sort(key=lambda r: r.get("策略版本", ""), reverse=True)
        return rules

    def validate_survivorship_coverage(self, entity_model: str,
                                         fields: list[str]) -> dict[str, Any]:
        """校验优胜策略对实体模型所有关键字段的覆盖情况。

        查询优胜策略存储，校验实体模型的每个字段是否都有
        对应的优胜策略规则，输出覆盖缺口分析报告。
        """
        rules = self.query_active_survivorship_rules(entity_model)
        covered = {r.get("选择字段", "") for r in rules}
        uncovered = [f for f in fields if f not in covered]
        return {
            "entity_model": entity_model,
            "total_fields": len(fields),
            "covered_fields": len(covered & set(fields)),
            "uncovered_fields": uncovered,
            "coverage_rate": round(len(covered & set(fields)) / max(len(fields), 1) * 100, 2),
        }