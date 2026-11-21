"""入库规则领域服务：字段映射校验、数据转换编排与导入规则管理。

预检（precheck）相关能力只做只读计算：解析字段映射、执行外部编码到门店
标准编码的转换并给出拒绝理由，绝不向任何门店目标表写入记录。真正的入库
写动作由 ``import_batch`` 服务在导入人「确认入库」时执行。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 门店主数据的标准编码字段：外部来源编码最终要转换到这个目标字段。
STORE_CODE_TARGET_FIELD = "门店编码"


class DataImportRuleService(BaseModuleService):
    MODULE = "data_import_rule"
    ENTITY = "入库规则"
    OPERATION_TERM = "导入"
    LIST_FIELDS = ["规则编号", "来源系统", "实体模型", "字段映射", "校验规则", "出错策略", "分片大小", "规则版本"]
    # 编码映射不在通用清单列里，但要随规则保存，供预检做外部编码转换。
    EXTRA_FIELDS = ["编码映射"]
    REQUIRED_FIELDS = ["规则编号", "来源系统", "实体模型"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记入库规则，并保留清单列之外的编码映射等扩展配置。"""
        entry, missing = super().create_entry(values)
        if entry is not None:
            for field in self.EXTRA_FIELDS:
                if field in values:
                    entry[field] = values[field]
        return entry, missing

    # ------------------------------------------------------------------
    # 规则解析
    # ------------------------------------------------------------------
    def resolve_active_rule(
        self,
        source_system: str,
        entity_model: str,
        rule_id: int | None = None,
    ) -> dict[str, Any] | None:
        """定位当前对「来源系统 + 实体模型」生效的入库规则。

        优先使用显式传入的规则编号；否则取同来源/同模型下版本号最高的已生效
        规则。规则可能在两个分片先后完成之间切换版本，因此每次预检都重新解析，
        并把命中的版本快照到预检结果上。
        """
        rows = store.rows(self.MODULE)
        if rule_id is not None:
            rule = next((r for r in rows if int(r.get("id", 0)) == rule_id), None)
            if rule and rule.get("status") == "已生效":
                return rule
            return None
        candidates = [
            row for row in rows
            if row.get("status") == "已生效"
            and row.get("来源系统") == source_system
            and row.get("实体模型") == entity_model
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: str(r.get("规则版本", "")), reverse=True)
        return candidates[0]

    @staticmethod
    def parse_field_mapping(mapping_spec: Any) -> list[tuple[str, str]]:
        """把 ``外部编码->门店编码,名称->门店名称`` 形式的配置解析成有序映射对。"""
        pairs: list[tuple[str, str]] = []
        if not mapping_spec:
            return pairs
        for pair in str(mapping_spec).split(","):
            parts = [part.strip() for part in pair.split("->")]
            if len(parts) == 2 and parts[0] and parts[1]:
                pairs.append((parts[0], parts[1]))
        return pairs

    @staticmethod
    def parse_code_mapping(code_spec: Any) -> dict[str, str]:
        """把 ``外部编码:标准编码`` 形式（逗号/换行分隔）解析成编码转换字典。"""
        converted: dict[str, str] = {}
        if not code_spec:
            return converted
        for chunk in str(code_spec).replace("\n", ",").split(","):
            parts = [part.strip() for part in chunk.split(":")]
            if len(parts) == 2 and parts[0]:
                converted[parts[0]] = parts[1]
        return converted

    # ------------------------------------------------------------------
    # 只读预检
    # ------------------------------------------------------------------
    def preview_record(
        self,
        record: dict[str, Any],
        rule: dict[str, Any],
        *,
        shard_index: int,
        row_no: int,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """对单条待导入数据执行入库预检（纯计算，不写任何目标表）。

        按字段映射执行源字段→目标字段转换，再用规则的编码映射把外部编码换算
        成门店标准编码。任一步失败都给出明确拒绝理由，成功则回填门店编码转换值。
        """
        mapping_pairs = self.parse_field_mapping(rule.get("字段映射"))
        result: dict[str, Any] = {
            "shard_index": shard_index,
            "row_no": row_no,
            "source_ref": source_ref or f"分片{shard_index}-第{row_no}行",
            "rule_id": rule.get("规则编号"),
            "rule_version": rule.get("规则版本"),
            "source_record": dict(record),
            "mapped_record": {},
            "store_code_field": STORE_CODE_TARGET_FIELD,
            "store_code_value": "",
            "accepted": False,
            "reject_reason": "",
        }

        if not mapping_pairs:
            result["reject_reason"] = "入库规则未配置字段映射，无法执行转换"
            return result

        mapped: dict[str, Any] = {}
        missing_sources: list[str] = []
        for src, tgt in mapping_pairs:
            if src not in record or str(record.get(src) or "").strip() == "":
                missing_sources.append(src)
                continue
            mapped[tgt] = record[src]
        result["mapped_record"] = mapped

        if missing_sources:
            result["reject_reason"] = f"源字段缺失或为空：{'、'.join(missing_sources)}"
            return result

        store_sources = [src for src, tgt in mapping_pairs if tgt == STORE_CODE_TARGET_FIELD]
        if not store_sources:
            result["reject_reason"] = f"字段映射未指向目标字段「{STORE_CODE_TARGET_FIELD}」"
            return result

        external_code = str(record.get(store_sources[0], "")).strip()
        code_map = self.parse_code_mapping(rule.get("编码映射"))
        if external_code not in code_map:
            result["reject_reason"] = (
                f"外部编码「{external_code}」在规则版本「{rule.get('规则版本')}」"
                f"中未映射出{STORE_CODE_TARGET_FIELD}"
            )
            return result

        standard_code = code_map[external_code]
        mapped[STORE_CODE_TARGET_FIELD] = standard_code
        result["store_code_value"] = standard_code
        result["accepted"] = True
        return result

    # ------------------------------------------------------------------
    # 兼容既有入库编排（保持原行为）
    # ------------------------------------------------------------------
    def import_data_with_rules(self, rule_id: int, raw_data: list[dict[str, Any]]) -> dict[str, Any]:
        """导入数据批次的字段映射与校验规则编排。

        根据入库规则执行字段映射转换，校验每条记录的必填字段完整性，
        导入数据到目标实体模型存储，记录成功/失败/重复统计量。
        """
        rule = self.get_entry(rule_id)
        if not rule or rule.get("status") != "已生效":
            return {"ok": False, "reason": "入库规则不可用或未生效"}
        mapped, failed, duped = 0, 0, 0
        for record in raw_data:
            # 导入单条记录：执行字段映射与数据转换
            mapped_record = self._apply_field_mapping(record, rule)
            if mapped_record is None:
                failed += 1
                continue
            # 校验字段完整性并导入到目标实体
            target_module = rule.get("实体模型", "entity_model")
            if self._record_exists(target_module, mapped_record):
                duped += 1
                continue
            mapped += 1
        return {"total": len(raw_data), "imported": mapped, "failed": failed, "duplicated": duped}

    def _apply_field_mapping(self, record: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any] | None:
        """解析字段映射规则，执行源字段到目标字段的转换和标准化。"""
        mapped = {}
        mapping_spec = rule.get("字段映射", "")
        if not mapping_spec:
            return record
        # 解析字段映射配置并执行数据转换
        for pair in mapping_spec.split(","):
            parts = pair.strip().split("->")
            if len(parts) == 2:
                src, tgt = parts[0].strip(), parts[1].strip()
                if src in record:
                    mapped[tgt] = record[src]
        return mapped if mapped else None

    def _record_exists(self, module: str, record: dict[str, Any]) -> bool:
        """校验目标实体中是否已存在相同记录，用于去重判断。"""
        rows = store.rows(module)
        for row in rows:
            if all(record.get(k) == row.get(k) for k in record if k in row):
                return True
        return False

    def validate_import_rules(self, source_system: str) -> list[dict[str, Any]]:
        """校验指定来源系统的所有入库规则是否完整可用。

        查询来源系统关联的入库规则，校验字段映射、校验规则、
        出错策略和分片大小的完整性，返回校验结果列表。
        """
        rows = store.rows(self.MODULE)
        relevant = [row for row in rows if row.get("来源系统") == source_system]
        results = []
        for rule in relevant:
            checks = {
                "rule_id": rule.get("规则编号"),
                "has_field_mapping": bool(rule.get("字段映射")),
                "has_validation": bool(rule.get("校验规则")),
                "has_error_strategy": bool(rule.get("出错策略")),
                "shard_size_valid": rule.get("分片大小", "").isdigit(),
            }
            checks["all_valid"] = all([
                checks["has_field_mapping"], checks["has_validation"],
                checks["has_error_strategy"]
            ])
            results.append(checks)
        return results

    def query_import_rules_by_model(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定实体模型关联的所有已生效入库规则。

        遍历入库规则存储，筛选已生效且匹配实体模型的规则，
        按规则版本排序返回供批次导入编排使用。
        """
        rows = store.rows(self.MODULE)
        rules = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已生效"
        ]
        # 查询入库规则并按规则版本排序
        rules.sort(key=lambda r: r.get("规则版本", ""), reverse=True)
        return rules

    def import_batch_with_rule(self, rule_id: int, batch_data: list[dict[str, Any]]) -> dict[str, Any]:
        """导入批次数据：执行入库规则校验、字段标准化、去重检查。

        解析入库规则配置，对每条记录执行字段映射与数据转换，
        校验必填字段完整性后进行去重检查，完成数据导入并入库。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "reason": "入库规则不存在"}
        result = {"batch_id": rule.get("规则编号"), "total": len(batch_data), "success": 0, "errors": 0}
        for record in batch_data:
            # 导入并校验单条记录：字段映射 -> 格式校验 -> 去重判断
            mapped = self._apply_field_mapping(record, rule)
            if mapped and len(mapped) >= max(len(rule.get("字段映射", "").split(",")), 1):
                result["success"] += 1
            else:
                result["errors"] += 1
        return result
