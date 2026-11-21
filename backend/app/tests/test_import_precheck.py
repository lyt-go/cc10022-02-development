"""入库分片预检业务链路测试。

覆盖需求点：
1. 门店数据切分后逐片预检，给出每条数据的门店编码转换值或拒绝理由；
2. 补跑因失败中断的分片时，已完成记录不重复计入；
3. 预检阶段不向门店目标表写入记录，只有确认入库才写；
4. 批次导入管理把逐片预检结果汇成可确认的导入清单；
5. 两个分片先后完成且中途切换入库规则版本时，清单暴露版本漂移并阻止确认；
6. N-10 在某片没映射出门店编码时，预检文件能定位原数据行与规则版本；
7. 修订源数据后复查，通过后确认入库并去重写入目标表。

运行：backend/.venv/bin/python -m app.tests.test_import_precheck
"""
from __future__ import annotations

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.data_import_rule import DataImportRuleService, STORE_CODE_TARGET_FIELD
from app.services.import_batch import ImportBatchService, target_module_of
from app.services.base import BaseModuleService
from app.seed import SEED_ROWS
from app.store import store


def _create_active_rule(
    rule_service: DataImportRuleService,
    *,
    source: str,
    model: str,
    version: str,
    code_mapping: str,
) -> dict:
    """登记并置为「已生效」的入库规则（含字段映射与编码映射）。"""
    entry, missing = rule_service.create_entry({
        "规则编号": f"RULE-{version}",
        "来源系统": source,
        "实体模型": model,
        "字段映射": f"store_ext_code->{STORE_CODE_TARGET_FIELD},store_name->门店名称",
        "校验规则": "门店编码必填",
        "出错策略": "拒绝入库",
        "分片大小": "3",
        "规则版本": version,
        "编码映射": code_mapping,
    })
    assert not missing, f"规则登记缺字段：{missing}"
    entry["status"] = "已生效"
    entry["pending"] = False
    return entry


def _create_batch(batch_service: ImportBatchService, *, source: str, model: str, total: int) -> dict:
    entry, missing = batch_service.create_entry({
        "批次编号": f"BATCH-PRE-{source}",
        "来源系统": source,
        "实体模型": model,
        "总记录数": str(total),
        "成功数": "0",
        "失败数": "0",
        "重复数": "0",
        "批次状态": "待处理",
    })
    assert not missing
    return entry


class TestImportPrecheck:
    def setup_method(self) -> None:
        # 每个用例使用从种子数据重建的全新内存仓库，避免跨用例污染
        # （上一个用例确认入库产生的动态目标表不会残留）。
        store._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self.rule_service = DataImportRuleService()
        self.batch_service = ImportBatchService()
        self.source = "POS-HQ"
        self.model = "门店主数据"

    # ── 用例1：逐片预检产出转换值/拒绝理由，且预检不写目标表 ──
    def test_precheck_produces_conversion_and_reject_without_writes(self):
        rule = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v1", code_mapping="N-1:SD001,N-2:SD002",
        )
        batch = _create_batch(self.batch_service, source=self.source, model=self.model, total=3)
        raw_rows = [
            {"store_ext_code": "N-1", "store_name": "一号门店"},
            {"store_ext_code": "N-2", "store_name": "二号门店"},
            {"store_ext_code": "N-10", "store_name": "十号门店"},  # 未映射
        ]
        shards = self.batch_service.split_large_batch(batch["id"], shard_size=2, raw_rows=raw_rows)
        assert len(shards) == 2

        assert store.table_size(target_module_of(batch)) == 0, "切分后、预检前不应存在目标表数据"

        r1 = self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"])
        assert r1["ok"] and r1["processed_total"] == 2
        assert r1["target_writes"] == 0, "预检阶段不得向门店目标表写入记录"

        r2 = self.batch_service.precheck_shard(batch["id"], 2, rule_id=rule["id"])
        assert r2["rejected"] == 1
        assert store.table_size(target_module_of(batch)) == 0, "全部预检完成后目标表仍应为空"

        checklist = self.batch_service.get_precheck_checklist(batch["id"])
        assert checklist["total_rows"] == 3
        assert checklist["accepted"] == 2 and checklist["rejected"] == 1
        accepted = [i for i in checklist["items"] if i["accepted"]]
        assert {i["store_code_value"] for i in accepted} == {"SD001", "SD002"}
        rejected = [i for i in checklist["items"] if not i["accepted"]][0]
        assert "N-10" in rejected["reject_reason"]
        assert rejected["rule_version"] == "v1"
        assert rejected["source_ref"].endswith("源第1行") and rejected["shard_index"] == 2
        # 存在被拒记录，清单不允许确认。
        assert checklist["confirmable"] is False
        assert any("被拒" in b for b in checklist["blockers"])

    # ── 用例2：分片中断后补跑，已完成记录不重复计入 ──
    def test_interrupted_shard_rerun_is_idempotent(self):
        rule = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v1", code_mapping="N-1:SD001,N-2:SD002,N-3:SD003,N-4:SD004",
        )
        batch = _create_batch(self.batch_service, source=self.source, model=self.model, total=4)
        raw_rows = [
            {"store_ext_code": "N-1", "store_name": "一店"},
            {"store_ext_code": "N-2", "store_name": "二店"},
            {"store_ext_code": "N-3", "store_name": "三店"},
            {"store_ext_code": "N-4", "store_name": "四店"},
        ]
        self.batch_service.split_large_batch(batch["id"], shard_size=4, raw_rows=raw_rows)

        first = self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"], fail_after=2)
        assert first["interrupted"] is True
        assert first["processed_this_run"] == 2 and first["processed_total"] == 2

        # 模拟失败中断后的补跑：前 2 条已完成，应被跳过且不重复计入。
        second = self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"])
        assert second["interrupted"] is False
        assert second["skipped_already_done"] == 2
        assert second["processed_this_run"] == 2
        assert second["processed_total"] == 4

        shard = self.batch_service._find_shard(self.batch_service.get_entry(batch["id"]), 1)
        assert len(shard["precheck_results"]) == 4, "补跑不得产生重复预检记录"
        assert len(shard["processed_keys"]) == 4
        assert second["target_writes"] == 0

    # ── 用例3：预检文件能定位 N-10 的原数据行与规则版本 ──
    def test_export_report_locates_original_row_and_version(self):
        rule = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v7", code_mapping="N-1:SD001",
        )
        batch = _create_batch(self.batch_service, source=self.source, model=self.model, total=1)
        self.batch_service.split_large_batch(
            batch["id"], shard_size=1,
            raw_rows=[{"store_ext_code": "N-10", "store_name": "十店"}],
        )
        self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"])

        report = self.batch_service.export_precheck_report(batch["id"])
        assert report["ok"] and report["rejected"] == 1
        content = report["content"]
        assert "拒绝" in content and "N-10" in content
        assert "v7" in content, "预检文件需带规则版本"
        assert "源第1行" in content, "预检文件需能定位原数据行"
        assert report["filename"].endswith(".csv")

    # ── 用例4：分片跨版本完成 → 版本漂移，清单阻止确认 ──
    def test_version_drift_between_shards_blocks_confirmation(self):
        rule_v1 = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v1", code_mapping="N-1:SD001,N-2:SD002",
        )
        batch = _create_batch(self.batch_service, source=self.source, model=self.model, total=4)
        raw_rows = [
            {"store_ext_code": "N-1", "store_name": "一店"},
            {"store_ext_code": "N-2", "store_name": "二店"},
            {"store_ext_code": "N-1", "store_name": "一店副本"},
            {"store_ext_code": "N-2", "store_name": "二店副本"},
        ]
        self.batch_service.split_large_batch(batch["id"], shard_size=2, raw_rows=raw_rows)

        # 分片1 先按 v1 完成。
        self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule_v1["id"])

        # 规则切换：v1 停用，v2 生效，外部编码 N-1 的目标编码随之改变。
        rule_v1["status"] = "已停用"
        rule_v2 = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v2", code_mapping="N-1:SD901,N-2:SD902",
        )
        # 分片2 后完成，命中 v2。
        r2 = self.batch_service.precheck_shard(batch["id"], 2, rule_id=rule_v2["id"])
        assert r2["rule_version"] == "v2"

        checklist = self.batch_service.get_precheck_checklist(batch["id"])
        assert checklist["version_drift"] is True
        assert checklist["rule_versions"] == ["v1", "v2"]
        assert checklist["confirmable"] is False
        assert any("版本漂移" in b for b in checklist["blockers"])

        # 即使全部记录本身通过预检，版本漂移也必须阻止写库。
        confirm = self.batch_service.confirm_import(batch["id"])
        assert confirm["ok"] is False
        assert store.table_size(target_module_of(batch)) == 0

    # ── 用例5：修订复查后确认入库，仅此时写目标表并去重 ──
    def test_recheck_then_confirm_writes_target_once(self):
        rule = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v1", code_mapping="N-1:SD001,N-2:SD002",
        )
        batch = _create_batch(self.batch_service, source=self.source, model=self.model, total=2)
        raw_rows = [
            {"store_ext_code": "N-10", "store_name": "待修订门店"},  # 首次被拒
            {"store_ext_code": "N-2", "store_name": "二店"},
        ]
        self.batch_service.split_large_batch(batch["id"], shard_size=2, raw_rows=raw_rows)
        first = self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"])
        assert first["rejected"] == 1

        # 导入人修订源数据（N-10 -> N-1）后复查整片。
        stored = self.batch_service.get_entry(batch["id"])
        stored["shards"][0]["raw_rows"][0]["store_ext_code"] = "N-1"
        recheck = self.batch_service.recheck_shard(batch["id"], 1, rule_id=rule["id"])
        assert recheck["accepted"] == 2 and recheck["rejected"] == 0

        checklist = self.batch_service.get_precheck_checklist(batch["id"])
        assert checklist["confirmable"] is True and checklist["blockers"] == []

        # 确认前目标表为空；确认后才写入。
        assert store.table_size(target_module_of(batch)) == 0
        confirm = self.batch_service.confirm_import(batch["id"])
        assert confirm["ok"] is True and confirm["written"] == 2
        target_rows = store.rows(target_module_of(batch))
        assert len(target_rows) == 2
        assert {r[STORE_CODE_TARGET_FIELD] for r in target_rows} == {"SD001", "SD002"}

        # 已确认的批次预检被锁定，不能再预检。
        again = self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"])
        assert again["ok"] is False

        # 重复确认受保护（清单已锁定为已入库）。
        re_confirm = self.batch_service.confirm_import(batch["id"])
        assert re_confirm["ok"] is False
        assert len(store.rows(target_module_of(batch))) == 2, "目标表不得被重复写入"

    # ── 用例6：确认入库按门店编码去重 ──
    def test_confirm_deduplicates_by_store_code(self):
        rule = _create_active_rule(
            self.rule_service, source=self.source, model=self.model,
            version="v1", code_mapping="N-1:SD001",
        )
        batch = _create_batch(self.batch_service, source=self.source, model=self.model, total=2)
        raw_rows = [
            {"store_ext_code": "N-1", "store_name": "一店A"},
            {"store_ext_code": "N-1", "store_name": "一店B"},  # 同一标准编码
        ]
        self.batch_service.split_large_batch(batch["id"], shard_size=2, raw_rows=raw_rows)
        self.batch_service.precheck_shard(batch["id"], 1, rule_id=rule["id"])
        confirm = self.batch_service.confirm_import(batch["id"])
        assert confirm["ok"] is True
        assert confirm["written"] == 1 and confirm["duplicated"] == 1
        assert len(store.rows(target_module_of(batch))) == 1

    # ── 用例7：无生效规则时预检失败且不写库 ──
    def test_precheck_without_active_rule(self):
        batch = _create_batch(self.batch_service, source="NO-RULE-SRC", model="门店主数据", total=1)
        self.batch_service.split_large_batch(
            batch["id"], shard_size=1, raw_rows=[{"store_ext_code": "X-1"}]
        )
        result = self.batch_service.precheck_shard(batch["id"], 1)
        assert result["ok"] is False
        assert store.table_size(target_module_of(batch)) == 0


# 让 BaseModuleService 仅用于类型可读性，避免未使用告警时也可被静态检查引用。
_ = BaseModuleService


if __name__ == "__main__":
    runner = TestImportPrecheck()
    tests = [
        ("逐片预检转换值/拒绝理由且不写目标表", runner.test_precheck_produces_conversion_and_reject_without_writes),
        ("中断分片补跑幂等不重复计入", runner.test_interrupted_shard_rerun_is_idempotent),
        ("预检文件定位N-10原数据行与版本", runner.test_export_report_locates_original_row_and_version),
        ("分片跨规则版本产生漂移并阻止确认", runner.test_version_drift_between_shards_blocks_confirmation),
        ("修订复查后确认入库且只写一次", runner.test_recheck_then_confirm_writes_target_once),
        ("确认入库按门店编码去重", runner.test_confirm_deduplicates_by_store_code),
        ("无生效规则预检失败不写库", runner.test_precheck_without_active_rule),
    ]
    passed = failed = 0
    for name, test_fn in tests:
        runner.setup_method()
        try:
            test_fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {name} -> {exc}")
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"结果: {passed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条分片预检测试失败！"
