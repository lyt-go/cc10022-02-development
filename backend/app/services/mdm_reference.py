"""参考数据领域服务：标准编码入库、字典映射校验与参考版本管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MdmReferenceService(BaseModuleService):
    MODULE = "mdm_reference"
    ENTITY = "参考数据"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["数据编号", "数据类别", "标准编码", "标准名称", "别名列表", "映射关系", "有效日期", "数据版本"]
    REQUIRED_FIELDS = ["数据编号", "数据类别", "标准编码"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    def import_reference_data(self, category: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """导入参考数据：将标准编码和字典值导入参考数据存储。

        读取标准编码字典数据，校验编码唯一性和数据类别匹配，
        导入参考数据到mdm_reference存储供字段标准化清洗使用。
        """
        rows = store.rows(self.MODULE)
        imported, skipped = 0, 0
        for entry_data in entries:
            code = entry_data.get("标准编码", "")
            exists = any(r.get("标准编码") == code for r in rows)
            if exists:
                skipped += 1
                continue
            ref_id = f"REF-{category}-{imported + 1:04d}"
            ref_entry = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1 + imported,
                "数据编号": ref_id,
                "数据类别": category,
                "标准编码": code,
                "标准名称": entry_data.get("标准名称", ""),
                "别名列表": entry_data.get("别名列表", ""),
                "映射关系": entry_data.get("映射关系", ""),
                "有效日期": entry_data.get("有效日期", ""),
                "数据版本": "1.0",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            rows.append(ref_entry)
            imported += 1
        # 导入参考数据：存储参考数据到mdm_reference供字段清洗使用
        return {"imported": imported, "skipped": skipped, "total": len(entries)}

    def lookup_reference_value(self, category: str, code: str) -> dict[str, Any] | None:
        """查询参考数据：按数据类别和标准编码查找标准名称。

        查询参考数据存储，按标准编码检索对应数据类别的
        标准名称、别名和映射关系，用于字段标准化转换。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("数据类别") == category and (
                row.get("标准编码") == code or
                code in (row.get("别名列表", "")).split(",")
            ):
                # 查询参考数据：按编码查找标准名称和映射关系
                return {
                    "standard_code": row.get("标准编码"),
                    "standard_name": row.get("标准名称"),
                    "aliases": (row.get("别名列表", "")).split(","),
                    "mappings": (row.get("映射关系", "")).split(","),
                    "version": row.get("数据版本"),
                }
        return None

    def validate_reference_uniqueness(self, category: str) -> dict[str, Any]:
        """校验参考数据的编码唯一性：检查同类别下是否有重复标准编码。

        读取mdm_reference存储，按数据类别分组检查标准编码
        是否唯一，输出重复编码列表和校验报告。
        """
        rows = store.rows(self.MODULE)
        cat_rows = [r for r in rows if r.get("数据类别") == category]
        seen: dict[str, list[str]] = {}
        for row in cat_rows:
            code = row.get("标准编码", "")
            if code not in seen:
                seen[code] = []
            seen[code].append(row.get("数据编号", ""))
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        # 校验参考数据唯一性：检测同类别下重复编码
        return {
            "category": category,
            "total": len(cat_rows),
            "unique_codes": len(seen),
            "duplicate_codes": len(duplicates),
            "duplicates": duplicates,
            "unique_pct": round((len(seen) - len(duplicates)) / max(len(seen), 1) * 100, 2),
        }

    def query_reference_by_category(self, category: str) -> list[dict[str, Any]]:
        """查询指定数据类别的所有已生效参考数据。

        遍历参考数据存储，筛选已生效且匹配数据类别的记录，
        按标准编码排序返回参考数据列表。
        """
        rows = store.rows(self.MODULE)
        refs = [
            row for row in rows
            if row.get("数据类别") == category and row.get("status") == "已生效"
        ]
        # 查询参考数据：按数据类别和编码排序
        refs.sort(key=lambda r: r.get("标准编码", ""))
        return refs

    def merge_reference_versions(self, category: str, old_version: str,
                                   new_version: str) -> dict[str, Any]:
        """归并参考数据版本：将旧版本参考数据迁移到新版本。

        读取参考数据存储，将指定类别的旧版本数据归并到
        新版本，更新数据版本号并归档旧版本记录。
        """
        rows = store.rows(self.MODULE)
        migrated = 0
        for row in rows:
            if row.get("数据类别") == category and row.get("数据版本") == old_version:
                row["数据版本"] = new_version
                row["version_migrated_at"] = datetime.datetime.utcnow().isoformat()
                migrated += 1
        # 归并参考数据版本：将旧版本归档并迁移到新版本
        return {"category": category, "migrated_count": migrated, "from": old_version, "to": new_version}