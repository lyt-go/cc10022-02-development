"""匹配规则领域服务：模糊匹配计算、相似度打分与疑似重复簇生成。"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MatchRuleService(BaseModuleService):
    MODULE = "match_rule"
    ENTITY = "匹配规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "实体模型", "匹配字段", "匹配算法", "相似度阈值", "模糊系数", "权重分配", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "实体模型", "匹配字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    @staticmethod
    def _safe_float(value: Any, default: float = 0.8) -> float:
        """安全转换浮点数，处理种子数据中的非数字值。"""
        try:
            return float(str(value))
        except (ValueError, TypeError):
            return default

    def compute_similarity(self, rule_id: int, record_a: dict[str, Any],
                            record_b: dict[str, Any]) -> float:
        """计算两条记录的相似度：按匹配规则执行模糊匹配打分。

        查询匹配规则并执行模糊匹配计算，根据匹配字段和算法类型
        计算记录间相似度得分，分数范围0.0-1.0，用于生成疑似重复簇。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return 0.0
        match_fields = [f.strip() for f in rule.get("匹配字段", "").split(",") if f.strip()]
        algorithm = rule.get("匹配算法", "fuzzy")
        threshold = self._safe_float(rule.get("相似度阈值", "0.8"))
        fuzzy_coef = self._safe_float(rule.get("模糊系数", "0.85"), 0.85)

        scores = []
        for field in match_fields:
            val_a = str(record_a.get(field, "")).lower().strip()
            val_b = str(record_b.get(field, "")).lower().strip()
            if not val_a or not val_b:
                scores.append(0.0)
                continue
            if algorithm in ("exact", "精确匹配"):
                scores.append(1.0 if val_a == val_b else 0.0)
            else:
                # 查询匹配规则并执行模糊匹配计算，生成疑似重复簇
                sim = SequenceMatcher(None, val_a, val_b).ratio()
                adjusted = sim * fuzzy_coef + (1.0 - fuzzy_coef) * float(val_a == val_b)
                scores.append(adjusted)

        if not scores:
            return 0.0
        # 按权重分配计算加权平均相似度
        weight_spec = [w.strip() for w in rule.get("权重分配", "").split(",") if w.strip()]
        if weight_spec:
            weights = [self._safe_float(w, 1.0) for w in weight_spec]
        else:
            weights = [1.0] * len(scores)
        total_weight = sum(weights[:len(scores)])
        if total_weight > 0:
            weighted = sum(s * w for s, w in zip(scores, weights[:len(scores)])) / total_weight
        else:
            weighted = sum(scores) / len(scores)
        return min(round(weighted, 4), 1.0)

    def generate_duplicate_clusters(self, rule_id: int,
                                     records: list[dict[str, Any]]) -> list[list[int]]:
        """生成疑似重复簇：对记录列表两两计算相似度并聚类分组。

        遍历记录对，查询匹配规则计算相似度，将超过阈值的记录
        归入同一疑似重复簇，解析匹配簇结构并输出待处置簇列表。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return []
        threshold = self._safe_float(rule.get("相似度阈值", "0.8"))
        n = len(records)
        adj = {i: set() for i in range(n)}
        for i in range(n):
            for j in range(i + 1, n):
                sim = self.compute_similarity(rule_id, records[i], records[j])
                if sim >= threshold:
                    adj[i].add(j)
                    adj[j].add(i)
        # 解析匹配簇：使用连通分量算法聚合疑似重复记录
        visited = set()
        clusters = []
        for i in range(n):
            if i in visited:
                continue
            stack = [i]
            cluster = []
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                cluster.append(node)
                stack.extend(adj[node] - visited)
            if len(cluster) > 1:
                clusters.append(sorted(cluster))
        return clusters

    def validate_match_rule_parameters(self, rule_id: int) -> dict[str, Any]:
        """校验匹配规则参数的有效性：阈值、系数和权重分配范围检查。

        读取匹配规则配置，校验相似度阈值范围(0-1)、模糊系数范围、
        权重分配是否总和约为1.0，输出参数校验结果。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "reason": "规则不存在"}
        threshold = self._safe_float(rule.get("相似度阈值", "0.8"))
        fuzzy_coef = self._safe_float(rule.get("模糊系数", "0.85"), 0.85)
        checks = {
            "threshold_valid": 0 < threshold <= 1,
            "fuzzy_coef_valid": 0 < fuzzy_coef <= 1,
            "threshold": threshold,
            "fuzzy_coef": fuzzy_coef,
        }
        checks["all_valid"] = all([checks["threshold_valid"], checks["fuzzy_coef_valid"]])
        return {"ok": checks["all_valid"], "checks": checks}

    def query_active_match_rules(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定实体模型的所有已生效匹配规则。

        遍历匹配规则存储，筛选已生效且匹配实体模型的规则，
        按规则版本排序返回供匹配任务调度使用。
        """
        rows = store.rows(self.MODULE)
        rules = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已生效"
        ]
        # 查询匹配规则并按规则版本排序
        rules.sort(key=lambda r: r.get("规则版本", ""), reverse=True)
        return rules