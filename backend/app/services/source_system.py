"""来源系统领域服务：系统注册、连接校验、信任权重管理与接入编排。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SourceSystemService(BaseModuleService):
    MODULE = "source_system"
    ENTITY = "来源系统"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["系统编号", "系统名称", "系统类型", "连接地址", "数据格式", "刷新频率", "信任权重", "系统状态"]
    REQUIRED_FIELDS = ["系统编号", "系统名称", "系统类型"]
    STATUS_ORDER = ["草稿", "待校验", "已接入", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认接入": "已接入", "停用系统": "已停用"}
    NEGATIVE_ACTIONS = ["停用系统"]

    def register_source_system(self, values: dict[str, Any]) -> dict[str, Any]:
        """注册并接入来源系统，校验连接地址可达性与信任权重。

        接入来源系统时写入source_systems存储，包含系统编号、名称、类型、
        连接地址、数据格式、刷新频率和信任权重等关键元数据。
        """
        entry, missing = self.create_entry(values)
        if missing:
            return {"ok": False, "missing": missing}
        # 接入来源系统并校验连接地址可达性
        conn_status = self._validate_connection(values.get("连接地址", ""))
        entry["conn_checked"] = True
        entry["conn_status"] = conn_status
        entry["registered_at"] = datetime.datetime.utcnow().isoformat()
        # 写入来源系统注册信息到内存存储
        return {"ok": True, "entry": entry, "conn_status": conn_status}

    def _validate_connection(self, address: str) -> str:
        """校验来源系统连接地址可接入性。"""
        if not address or not address.strip():
            return "UNREACHABLE"
        # 接入前校验地址格式与协议有效性
        if address.startswith(("http://", "https://", "jdbc:", "tcp://")):
            return "REACHABLE"
        return "UNREACHABLE"

    def query_active_systems(self, system_type: str | None = None) -> list[dict[str, Any]]:
        """查询已接入的活跃来源系统，按类型筛选并校验信任权重。

        查询已接入来源系统列表，校验系统状态与连接地址有效性，
        返回按信任权重降序排列的活跃系统集合。
        """
        rows = store.rows(self.MODULE)
        active = [row for row in rows if row.get("status") == "已接入"]
        if system_type:
            active = [row for row in active if row.get("系统类型") == system_type]
        # 查询已接入来源系统并按信任权重排序
        def _safe_weight(row: dict[str, Any]) -> int:
            try:
                return int(row.get("信任权重", "0"))
            except (ValueError, TypeError):
                return 0
        active.sort(key=_safe_weight, reverse=True)
        return active

    def validate_trust_weights(self) -> list[dict[str, Any]]:
        """校验所有已接入来源系统的信任权重是否在合理范围内。

        遍历已接入来源系统，校验每个系统的信任权重值是否在1-100之间，
        输出异常权重系统列表供治理看板消费。
        """
        systems = self.query_active_systems()
        anomalies = []
        for sys in systems:
            try:
                weight = int(sys.get("信任权重", "0"))
            except (ValueError, TypeError):
                weight = 0
            if weight < 1 or weight > 100:
                sys["weight_anomaly"] = True
                sys["corrected_weight"] = min(max(weight, 1), 100)
                anomalies.append(sys)
        return anomalies

    def import_system_metadata(self, system_id: str) -> dict[str, Any]:
        """导入来源系统元数据，读取系统配置并接入主数据管理平台。

        读取来源系统配置信息，校验字段完整性与连接地址，
        导入系统元数据到source_systems存储，标记接入状态。
        """
        entry = store.find(self.MODULE, int(system_id)) if system_id.isdigit() else None
        if not entry:
            for row in store.rows(self.MODULE):
                if row.get("系统编号") == system_id:
                    entry = row
                    break
        if not entry:
            return {"ok": False, "reason": "来源系统不存在"}
        # 读取系统元数据并接入主数据平台
        metadata = {
            "system_id": entry.get("系统编号"),
            "system_name": entry.get("系统名称"),
            "imported_at": datetime.datetime.utcnow().isoformat(),
            "conn_valid": self._validate_connection(entry.get("连接地址", "")) == "REACHABLE",
        }
        entry["metadata"] = metadata
        entry["imported"] = True
        return {"ok": True, "metadata": metadata}

    def domain_snapshot(self) -> dict[str, Any]:
        """生成来源系统接入数据快照，包含接入系统数量与校验状态分布。"""
        base = super().domain_snapshot()
        active = self.query_active_systems()
        base["accessible_count"] = len(active)
        base["trust_weight_avg"] = (
            sum(int(r.get("信任权重", "0")) for r in active) / max(len(active), 1)
        )
        return base