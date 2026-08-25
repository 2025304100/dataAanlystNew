"""WP8-03: 正式验收门禁白盒测试。

对齐 docs/专业因子库开发计划.md §12.3 和 spec.md「迁移切换与回退」。

覆盖：
- dual_read_compare 核心对比逻辑（_compare_factor / DualReadReport）
- rollback_drill 回滚演练（execute_rollback_drill / verify_rollback_read_path）
- Active 异常不改写历史 Score
- manual 和上一 FactorSet 均可回退
- FactorSet frozen 后不可修改成员
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.whitebox

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.models.factor_runtime import (
    FactorModelAuditLog,
    FactorRuntimeState,
    FactorSystemConfig,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.config import update_factor_system_config
from app.services.factors.dual_read_compare import (
    DualReadReport,
    FactorComparisonEntry,
    _compare_factor,
)
from app.services.factors.rollback_drill import (
    execute_rollback_drill,
    verify_rollback_read_path,
)
from app.services.factors.runtime import (
    activate_factor_model,
    fallback_factor_model,
    get_factor_runtime_snapshot,
)


# ── 辅助函数 ──────────────────────────────────────────────


def _ensure_runtime(db_session, *, weight_mode: str = "manual"):
    """确保 FactorRuntimeState 单例存在。"""
    state = db_session.get(FactorRuntimeState, 1)
    if state is None:
        state = FactorRuntimeState(
            id=1,
            weight_mode=weight_mode,
            active_model_run_id=None,
            updated_by="test",
            fallback_reason=None,
            version=1,
        )
        db_session.add(state)
    else:
        state.weight_mode = weight_mode
        state.active_model_run_id = None
        state.fallback_reason = None
    db_session.flush()


def _ensure_model_run(db_session, run_id: str, model_type: str = "ridge") -> None:
    """为 wp8 active_model_run_id 外键插入占位 FactorModelRun 记录（不存在时）。"""
    existing = db_session.get(FactorModelRun, run_id)
    if existing is not None:
        return
    run = FactorModelRun(
        id=run_id,
        model_type=model_type,
        asset_type="index",
        target_code="000300",
        train_start_date=date(2025, 1, 1),
        train_end_date=date(2025, 12, 31),
        validation_start_date=date(2026, 1, 1),
        validation_end_date=date(2026, 6, 30),
        data_cutoff_at=datetime(2026, 7, 1),
        feature_versions_json="{}",
        hyperparameters_json='{"factor_set_id": "legacy-system-v1"}',
        metrics_json='{"validation_ic": 0.05}',
        sample_count=1000,
        symbol_count=300,
        trade_date_count=250,
        status="validated",
        rejection_reason=None,
        artifact_path=None,
    )
    db_session.add(run)
    db_session.flush()


def _ensure_config(db_session, *, feature_enabled: bool = True):
    """确保 FactorSystemConfig 单例存在。"""
    config = db_session.get(FactorSystemConfig, 1)
    if config is None:
        config = FactorSystemConfig(
            id=1,
            feature_enabled=int(feature_enabled),
            warehouse_path="/tmp/test.duckdb",
            updated_by="test",
        )
        db_session.add(config)
    else:
        config.feature_enabled = int(feature_enabled)
    db_session.flush()


def _seed_symbol(db_session, code: str = "000001") -> Symbol:
    symbol = Symbol(
        symbol=code,
        name=f"Test {code}",
        asset_type="stock",
        market="SH",
    )
    db_session.add(symbol)
    db_session.flush()
    return symbol


def _seed_score(
    db_session,
    symbol_id: int,
    trade_date: date,
    *,
    quality_score: float = 75.0,
    factor_set_id: str | None = "legacy-system-v1",
    factor_member_versions_json: str | None = None,
    calc_batch_id: str = "score-batch-001",
) -> Score:
    """创建一条历史 Score 记录。"""
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=quality_score,
        quality_grade="B",
        timing_score=60.0,
        stage="initial",
        action="watch",
        priority_score=70.0,
        weight_mode="ridge",
        factor_model_run_id="model-historical-001",
        factor_set_id=factor_set_id,
        factor_member_versions_json=factor_member_versions_json
        or '{"ep_ttm": {"version": 1, "role": "feature"}}',
        calc_batch_id=calc_batch_id,
    )
    db_session.add(score)
    db_session.flush()
    return score


# ── dual_read_compare: _compare_factor 单元测试 ────────────


class TestCompareFactor:
    """_compare_factor 核心对比逻辑测试。"""

    def _make_df(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(rows)

    def test_identical_frames_within_tolerance(self):
        """相同数据完全一致。"""
        legacy = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
            {"symbol": "000002", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": -0.3, "raw_value": 0.8},
        ])
        fs = legacy.copy()

        entry = _compare_factor(legacy, fs, "ep_ttm", tolerance=1e-6)
        assert entry.row_count_match is True
        assert entry.missing_match is True
        assert entry.compared_pairs == 2
        assert entry.max_abs_diff is not None
        assert entry.max_abs_diff <= 1e-6
        assert entry.within_tolerance is True

    def test_different_row_count_mismatch(self):
        """行数不一致。"""
        legacy = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
            {"symbol": "000002", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": -0.3, "raw_value": 0.8},
        ])
        fs = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
        ])

        entry = _compare_factor(legacy, fs, "ep_ttm", tolerance=1e-6)
        assert entry.row_count_match is False
        assert entry.legacy_rows == 2
        assert entry.factor_set_rows == 1

    def test_value_exceeds_tolerance(self):
        """归一化值超出容差。"""
        legacy = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
        ])
        fs = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.6, "raw_value": 1.3},
        ])

        entry = _compare_factor(legacy, fs, "ep_ttm", tolerance=1e-6)
        assert entry.row_count_match is True
        assert entry.compared_pairs == 1
        assert entry.max_abs_diff is not None
        assert entry.max_abs_diff == pytest.approx(0.1, abs=1e-8)
        assert entry.within_tolerance is False

    def test_missing_values_mismatch(self):
        """缺失值数量不一致。"""
        legacy = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
            {"symbol": "000002", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": None, "raw_value": None},
        ])
        fs = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
            {"symbol": "000002", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.3, "raw_value": 0.9},
        ])

        entry = _compare_factor(legacy, fs, "ep_ttm", tolerance=1e-6)
        assert entry.missing_match is False
        assert entry.legacy_missing == 1
        assert entry.factor_set_missing == 0

    def test_empty_frames_returns_zero(self):
        """空 DataFrame 对比。"""
        legacy = self._make_df([
            {"symbol": "000001", "trade_date": date(2026, 7, 24), "factor_code": "ep_ttm",
             "normalized_value": 0.5, "raw_value": 1.2},
        ])
        empty = pd.DataFrame(columns=["symbol", "trade_date", "factor_code", "normalized_value", "raw_value"])

        entry = _compare_factor(legacy, empty, "ep_ttm", tolerance=1e-6)
        assert entry.legacy_rows == 1
        assert entry.factor_set_rows == 0
        assert entry.row_count_match is False
        assert entry.compared_pairs == 0


# ── DualReadReport 结构测试 ───────────────────────────────


class TestDualReadReport:
    """DualReadReport 结构化报告测试。"""

    def test_report_to_dict_contains_all_fields(self):
        """报告 to_dict 包含所有必要字段。"""
        report = DualReadReport(
            trade_date=date(2026, 7, 24),
            factor_set_id="legacy-system-v1",
            tolerance=1e-6,
            legacy_batch_id="batch-legacy",
            factor_set_batch_id="batch-fs",
            legacy_rows_written=100,
            factor_set_total_members=8,
            factor_set_success_count=8,
            factor_set_failed_count=0,
            legacy_factor_codes=["ep_ttm", "turnover_z20"],
            factor_set_factor_codes=["ep_ttm", "turnover_z20"],
            factor_codes_only_in_legacy=[],
            factor_codes_only_in_factor_set=[],
            entries=[
                FactorComparisonEntry(
                    factor_code="ep_ttm",
                    legacy_rows=50,
                    factor_set_rows=50,
                    row_count_match=True,
                    legacy_missing=0,
                    factor_set_missing=0,
                    missing_match=True,
                    compared_pairs=50,
                    max_abs_diff=0.0,
                    mean_abs_diff=0.0,
                    within_tolerance=True,
                ),
            ],
            overall_consistent=True,
        )

        d = report.to_dict()
        assert d["trade_date"] == "2026-07-24"
        assert d["factor_set_id"] == "legacy-system-v1"
        assert d["tolerance"] == 1e-6
        assert d["legacy_rows_written"] == 100
        assert d["factor_set_success_count"] == 8
        assert d["overall_consistent"] is True
        assert len(d["entries"]) == 1
        assert d["entries"][0]["factor_code"] == "ep_ttm"

    def test_report_inconsistent_when_factor_codes_differ(self):
        """因子代码集合不一致时 overall_consistent=False。"""
        report = DualReadReport(
            trade_date=None,
            factor_set_id="test",
            tolerance=1e-6,
            legacy_batch_id="b1",
            factor_set_batch_id="b2",
            legacy_rows_written=10,
            factor_set_total_members=5,
            factor_set_success_count=5,
            factor_set_failed_count=0,
            legacy_factor_codes=["ep_ttm", "turnover_z20"],
            factor_set_factor_codes=["ep_ttm"],
            factor_codes_only_in_legacy=["turnover_z20"],
            factor_codes_only_in_factor_set=[],
            entries=[],
            overall_consistent=False,
        )
        assert report.overall_consistent is False
        assert "turnover_z20" in report.factor_codes_only_in_legacy


# ── rollback_drill: 回滚演练测试 ──────────────────────────


class TestRollbackDrill:
    """execute_rollback_drill 回滚演练测试。"""

    def test_rollback_from_ridge_to_manual(self, db_session):
        """从 ridge 模式回滚到 manual。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)

        # 模拟有 active_model_run_id
        state = db_session.get(FactorRuntimeState, 1)
        _ensure_model_run(db_session, "model-test-001")
        state.active_model_run_id = "model-test-001"
        db_session.flush()

        report = execute_rollback_drill(
            db_session,
            actor="test_user",
            reason="test rollback from ridge",
        )
        db_session.commit()

        assert report.overall_success is True
        assert len(report.steps) == 4
        assert report.final_weight_mode == "manual"
        assert report.final_active_model_run_id is None
        assert report.final_feature_enabled is False
        assert report.final_fallback_reason == "test rollback from ridge"

    def test_rollback_already_manual(self, db_session):
        """已经是 manual 模式时回滚仍成功。"""
        _ensure_runtime(db_session, weight_mode="manual")
        _ensure_config(db_session, feature_enabled=False)

        report = execute_rollback_drill(
            db_session,
            actor="test_user",
            reason="already manual",
        )
        db_session.commit()

        assert report.overall_success is True
        assert report.final_weight_mode == "manual"
        assert report.final_active_model_run_id is None
        assert report.final_feature_enabled is False

    def test_rollback_preserves_audit_logs(self, db_session):
        """回滚不删除审计日志。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)

        # 预先写入一条审计日志（先补 model_run FK 种子）
        _ensure_model_run(db_session, "model-pre-001")
        db_session.add(
            FactorModelAuditLog(
                action="activate",
                model_run_id="model-pre-001",
                previous_mode="manual",
                new_mode="ridge",
                actor="pre_test",
                note="pre-rollback activation",
            )
        )
        db_session.flush()

        report = execute_rollback_drill(
            db_session,
            actor="test_user",
            reason="preserve audit test",
        )
        db_session.commit()

        assert report.overall_success is True
        # 至少保留 pre_test 的 1 条 + 回滚新增的 1 条
        assert report.audit_logs_preserved >= 1

    def test_verify_rollback_read_path_after_rollback(self, db_session):
        """回滚后读取路径验证。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)
        state = db_session.get(FactorRuntimeState, 1)
        _ensure_model_run(db_session, "model-verify-001")
        state.active_model_run_id = "model-verify-001"
        db_session.flush()

        execute_rollback_drill(db_session, actor="test", reason="verify path")
        db_session.commit()

        result = verify_rollback_read_path(db_session)
        assert result["weight_mode"] == "manual"
        assert result["active_model_run_id"] is None
        assert result["feature_enabled"] is False
        assert result["read_path"] == "definitions.py + factor_engine"
        assert result["uses_ridge_weights"] is False
        assert result["is_rolled_back"] is True

    def test_verify_rollback_read_path_not_rolled_back(self, db_session):
        """未回滚时读取路径验证返回 False。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)
        state = db_session.get(FactorRuntimeState, 1)
        _ensure_model_run(db_session, "model-active-001")
        state.active_model_run_id = "model-active-001"
        db_session.flush()

        result = verify_rollback_read_path(db_session)
        assert result["weight_mode"] == "ridge"
        assert result["is_rolled_back"] is False
        assert result["uses_ridge_weights"] is True


# ── Active 异常不改写历史 Score 测试 ─────────────────────


class TestActiveExceptionPreservesHistory:
    """Active 因子异常时，旧 FactorSet 和历史 Score 不被改写。"""

    def test_quarantined_factor_does_not_modify_historical_score(self, db_session):
        """因子被隔离后，已写入的历史 Score 记录不变。"""
        _ensure_runtime(db_session, weight_mode="manual")
        _ensure_config(db_session, feature_enabled=True)

        symbol = _seed_symbol(db_session)
        trade_date = date(2026, 7, 24)

        # 写入一条历史 Score（ridge 模式，含 factor_set_id）
        original_score = _seed_score(
            db_session,
            symbol_id=symbol.id,
            trade_date=trade_date,
            quality_score=82.5,
            factor_set_id="legacy-system-v1",
        )
        db_session.flush()

        original_quality = original_score.quality_score
        original_factor_set_id = original_score.factor_set_id
        original_versions_json = original_score.factor_member_versions_json

        # 模拟因子异常：直接修改因子状态（不影响已写入的 Score）
        factor = Factor(
            code="ep_ttm",
            name="EP TTm",
            category="fundamental",
            direction="higher_better",
            status="quarantined",
            is_active=0,
            origin="system",
            lifecycle_status="quarantined",
        )
        db_session.add(factor)
        db_session.flush()

        # 验证历史 Score 未被修改
        db_session.expire_all()
        unchanged_score = db_session.get(Score, original_score.id)
        assert unchanged_score is not None
        assert unchanged_score.quality_score == original_quality
        assert unchanged_score.factor_set_id == original_factor_set_id
        assert unchanged_score.factor_member_versions_json == original_versions_json

    def test_frozen_factor_set_remains_immutable(self, db_session):
        """frozen FactorSet 成员不可修改。"""
        _ensure_runtime(db_session)
        _ensure_config(db_session)

        # 创建 frozen FactorSet
        factor = Factor(
            code="ep_ttm",
            name="EP TTM",
            category="fundamental",
            direction="higher_better",
            status="active",
            is_active=1,
            origin="system",
            lifecycle_status="active",
        )
        db_session.add(factor)
        db_session.flush()

        version = FactorVersion(
            factor_id=factor.id,
            version=1,
            formula_expr="#ep_ttm",
            params_json="{}",
            direction="higher_better",
            is_latest=1,
            validation_status="active",
        )
        db_session.add(version)
        db_session.flush()

        factor_set = FactorSet(
            id="fs-immutable-test",
            name="Immutable Test Set",
            status="frozen",
            content_hash="hash_immutable",
            frozen_at=datetime.now(timezone.utc).replace(tzinfo=None),
            created_by="test",
        )
        db_session.add(factor_set)
        db_session.flush()

        member = FactorSetMember(
            factor_set_id="fs-immutable-test",
            factor_id=factor.id,
            factor_version_id=version.id,
            factor_code="ep_ttm",
            factor_version=1,
            role="feature",
            weight_constraint="free",
            display_order=0,
            missing_policy="exclude",
        )
        db_session.add(member)
        db_session.flush()

        original_hash = factor_set.content_hash
        original_frozen_at = factor_set.frozen_at
        original_member_count = db_session.query(FactorSetMember).filter_by(
            factor_set_id="fs-immutable-test"
        ).count()

        # 验证 frozen FactorSet 不可修改成员（通过服务层约束）
        from app.services.factors.factor_set_service import add_member, FactorSetError

        with pytest.raises(FactorSetError) as exc_info:
            add_member(
                db_session,
                factor_set_id="fs-immutable-test",
                payload=type(
                    "FakePayload",
                    (),
                    {
                        "factor_id": factor.id + 100,
                        "factor_version_id": version.id + 100,
                        "role": "feature",
                        "weight_constraint": "free",
                        "display_order": 1,
                        "missing_policy": "exclude",
                    },
                )(),
            )

        assert "set_not_mutable" in str(exc_info.value.code) or "not_mutable" in str(exc_info.value.code)

        # 验证 FactorSet 本身未被修改
        db_session.expire_all()
        unchanged_set = db_session.get(FactorSet, "fs-immutable-test")
        assert unchanged_set.status == "frozen"
        assert unchanged_set.content_hash == original_hash
        assert unchanged_set.frozen_at == original_frozen_at

        unchanged_member_count = db_session.query(FactorSetMember).filter_by(
            factor_set_id="fs-immutable-test"
        ).count()
        assert unchanged_member_count == original_member_count


# ── manual 和 FactorSet 均可回退测试 ─────────────────────


class TestRollbackCapability:
    """manual 和上一 FactorSet 均可回退。"""

    def test_ridge_to_manual_rollback(self, db_session):
        """从 ridge 回退到 manual。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)
        state = db_session.get(FactorRuntimeState, 1)
        _ensure_model_run(db_session, "model-ridge-001")
        state.active_model_run_id = "model-ridge-001"
        db_session.flush()

        snapshot = fallback_factor_model(
            db_session,
            actor="test",
            reason="ridge to manual rollback",
        )
        db_session.commit()

        assert snapshot.weight_mode == "manual"
        assert snapshot.active_model_run_id is None
        assert snapshot.fallback_reason == "ridge to manual rollback"

        # 验证审计日志
        audit = db_session.query(FactorModelAuditLog).filter_by(
            action="fallback"
        ).first()
        assert audit is not None
        assert audit.previous_mode == "ridge"
        assert audit.new_mode == "manual"
        assert audit.previous_model_run_id == "model-ridge-001"

    def test_manual_mode_does_not_require_rollback(self, db_session):
        """manual 模式本身不需要回退，但仍可调用 fallback。"""
        _ensure_runtime(db_session, weight_mode="manual")
        _ensure_config(db_session, feature_enabled=False)

        # manual 模式下 fallback 仍可执行（记录审计）
        snapshot = fallback_factor_model(
            db_session,
            actor="test",
            reason="manual mode redundant fallback",
        )
        db_session.commit()

        assert snapshot.weight_mode == "manual"
        assert snapshot.active_model_run_id is None

    def test_ridge_fallback_then_reactivate(self, db_session):
        """回退后可重新激活（回退不是终态）。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)
        state = db_session.get(FactorRuntimeState, 1)
        _ensure_model_run(db_session, "model-cycle-001")
        state.active_model_run_id = "model-cycle-001"
        db_session.flush()

        # 1. 回退到 manual
        fallback_snapshot = fallback_factor_model(
            db_session,
            actor="test",
            reason="cycle test fallback",
        )
        db_session.commit()
        assert fallback_snapshot.weight_mode == "manual"

        # 2. 重新激活到 ridge（需要 validated 模型）
        # 创建一个 validated 模型记录
        from app.models.factor_model import FactorModelRun
        model = FactorModelRun(
            id="model-cycle-002",
            model_type="ridge",
            asset_type="index",
            target_code="000300",
            train_start_date=date(2025, 1, 1),
            train_end_date=date(2025, 12, 31),
            validation_start_date=date(2026, 1, 1),
            validation_end_date=date(2026, 6, 30),
            data_cutoff_at=datetime(2026, 7, 1),
            feature_versions_json="{}",
            hyperparameters_json='{"factor_set_id": "legacy-system-v1"}',
            metrics_json='{"validation_ic": 0.05}',
            sample_count=1000,
            symbol_count=300,
            trade_date_count=250,
            status="validated",
            rejection_reason=None,
            artifact_path=None,
        )
        db_session.add(model)
        db_session.flush()

        # 3. 重新激活
        reactivate_snapshot = activate_factor_model(
            db_session,
            model_run_id="model-cycle-002",
            mode="ridge",
            actor="test",
            note="reactivate after fallback",
        )
        db_session.commit()

        assert reactivate_snapshot.weight_mode == "ridge"
        assert reactivate_snapshot.active_model_run_id == "model-cycle-002"
        assert reactivate_snapshot.fallback_reason is None

        # 4. 验证审计日志记录了完整周期
        audits = db_session.query(FactorModelAuditLog).order_by(
            FactorModelAuditLog.id
        ).all()
        actions = [a.action for a in audits]
        assert "fallback" in actions
        assert "activate" in actions


# ── feature_enabled 关闭前必须先回退到 manual 测试 ────────


class TestFeatureEnabledGuard:
    """feature_enabled 关闭前的安全门禁。"""

    def test_disable_feature_while_ridge_active_raises(self, db_session):
        """ridge 模式下关闭 feature_enabled 应报错。"""
        _ensure_runtime(db_session, weight_mode="ridge")
        _ensure_config(db_session, feature_enabled=True)
        state = db_session.get(FactorRuntimeState, 1)
        _ensure_model_run(db_session, "model-guard-001")
        state.active_model_run_id = "model-guard-001"
        db_session.flush()

        with pytest.raises(ValueError, match="Fallback to manual"):
            update_factor_system_config(
                db_session,
                feature_enabled=False,
                actor="test",
            )

    def test_disable_feature_after_manual_ok(self, db_session):
        """manual 模式下关闭 feature_enabled 成功。"""
        _ensure_runtime(db_session, weight_mode="manual")
        _ensure_config(db_session, feature_enabled=True)

        snapshot = update_factor_system_config(
            db_session,
            feature_enabled=False,
            actor="test",
        )
        db_session.commit()

        assert snapshot.feature_enabled == 0
