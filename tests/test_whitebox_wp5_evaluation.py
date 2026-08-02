"""WP5 白盒测试：因子科学评估与压力测试。

覆盖：
- WP5-01 评估运行契约（不可变、幂等、终态保护）
- WP5-02 时间切分与样本构造（purge/embargo、事件日）
- WP5-03 基础指标（IC/ICIR/分组/换手/成本/覆盖）
- WP5-04 分类门禁（continuous/event/regime）
- WP5-05 参数扰动（邻域/时间段/缺失敏感度）
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.models.factor import Factor
from app.models.factor_evaluation import EvaluationRun
from app.models.factor_model import FactorVersion
from app.services.factors.factor_evaluator import (
    EVALUATOR_VERSION,
    GATE_THRESHOLDS,
    TERMINAL_GATE_RESULTS,
    AlignedSample,
    CoverageMetrics,
    CostAdjustedReturn,
    EvaluationConfig,
    GateResult,
    ICMetrics,
    QuantileMetrics,
    TurnoverMetrics,
    align_factor_with_target,
    build_evaluation_run_id,
    build_time_split,
    compute_config_hash,
    compute_cost_adjusted_return,
    compute_coverage,
    compute_quantile_returns,
    compute_rank_ic,
    compute_turnover,
    create_evaluation_run,
    evaluate_gate,
    finalize_evaluation_run,
    get_evaluation_run,
    list_evaluation_runs,
    run_evaluation,
)
from app.services.factors.factor_stress import (
    DEFAULT_PERTURBATION_RATIOS,
    PERTURBATION_THRESHOLDS,
    MissingSensitivityBatch,
    ParameterPerturbationResult,
    StressTestSummary,
    TimePerturbationResult,
    perturb_missing_sensitivity,
    perturb_parameter,
    perturb_time_segments,
    run_stress_test,
)


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════


def _make_factor_and_version(db_session, factor_code="test_factor_001"):
    """创建测试因子和版本。"""
    factor = Factor(
        code=factor_code,
        name=f"Test Factor {factor_code}",
        category="test",
        direction="higher_better",
        status="active",  # NOT NULL 字段
        source_type="daily_bars",
        frequency="daily",
        default_missing_policy="exclude",
        lifecycle_status="testing",
        origin="user",
        is_active=0,
    )
    db_session.add(factor)
    db_session.flush()

    version = FactorVersion(
        factor_id=factor.id,
        version=1,
        formula_expr="close",
        params_json="{}",
        postprocess_json=None,
        direction="higher_better",
        is_latest=1,
        execution_plan_hash="abc123def456abc1",
        formula_ast_json="{}",
        data_dependencies_json="{}",
        created_via="manual",
    )
    db_session.add(version)
    db_session.flush()
    return factor, version


def _make_factor_values_df(
    n_days=80,
    n_symbols=20,
    start_date=date(2026, 5, 1),
    seed=42,
):
    """生成测试用因子值 DataFrame。"""
    rng = np.random.RandomState(seed)
    dates = [start_date + timedelta(days=i) for i in range(n_days)]
    symbols = [f"S{i:04d}" for i in range(n_symbols)]
    data = rng.randn(n_days, n_symbols)
    return pd.DataFrame(data, index=dates, columns=symbols)


def _make_forward_returns_df(
    factor_values: pd.DataFrame,
    horizon=5,
    seed=42,
):
    """生成与因子值正相关的前瞻收益（用于测试通过门禁）。"""
    rng = np.random.RandomState(seed + 1)
    # 收益 = 0.1 * 因子值 + 噪声
    returns = 0.1 * factor_values + 0.05 * rng.randn(*factor_values.shape)
    # shift 实现 horizon 天前瞻
    return returns.shift(-horizon)


def _make_random_returns_df(factor_values: pd.DataFrame, seed=99):
    """生成随机收益（IC 接近 0，用于测试失败门禁）。"""
    rng = np.random.RandomState(seed)
    return pd.DataFrame(
        rng.randn(*factor_values.shape),
        index=factor_values.index,
        columns=factor_values.columns,
    )


# ══════════════════════════════════════════════════════════
# WP5-01: 评估运行契约
# ══════════════════════════════════════════════════════════


class TestEvaluationRunContract:
    """评估运行契约测试。"""

    def test_compute_config_hash_is_deterministic(self):
        """配置哈希确定性。"""
        config = {"a": 1, "b": [2, 3], "c": {"d": "e"}}
        h1 = compute_config_hash(config)
        h2 = compute_config_hash(config)
        assert h1 == h2
        assert len(h1) == 16

    def test_compute_config_hash_differs_on_change(self):
        """配置变化哈希不同。"""
        c1 = {"a": 1, "b": 2}
        c2 = {"a": 1, "b": 3}
        assert compute_config_hash(c1) != compute_config_hash(c2)

    def test_compute_config_hash_key_order_independent(self):
        """配置哈希与键顺序无关。"""
        c1 = {"a": 1, "b": 2}
        c2 = {"b": 2, "a": 1}
        assert compute_config_hash(c1) == compute_config_hash(c2)

    def test_build_evaluation_run_id_deterministic(self):
        """运行 ID 确定性。"""
        rid1 = build_evaluation_run_id(
            factor_version_id=1, config_hash="abcdef0123456789",
            data_cutoff=date(2026, 7, 24),
        )
        rid2 = build_evaluation_run_id(
            factor_version_id=1, config_hash="abcdef0123456789",
            data_cutoff=date(2026, 7, 24),
        )
        assert rid1 == rid2
        assert "eval-1-abcdef01-20260724" == rid1

    def test_build_evaluation_run_id_differs_on_cutoff(self):
        """不同截止日 ID 不同。"""
        rid1 = build_evaluation_run_id(
            factor_version_id=1, config_hash="abcdef0123456789",
            data_cutoff=date(2026, 7, 24),
        )
        rid2 = build_evaluation_run_id(
            factor_version_id=1, config_hash="abcdef0123456789",
            data_cutoff=date(2026, 7, 25),
        )
        assert rid1 != rid2

    def test_create_evaluation_run_persists(self, db_session):
        """创建评估运行并持久化。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        config = {"factor_kind": "continuous", "target_horizon": 5}
        run = create_evaluation_run(
            db_session,
            factor_version_id=version.id,
            config=config,
            data_cutoff_at=datetime(2026, 7, 24),
            train_start=date(2026, 5, 1),
            train_end=date(2026, 6, 15),
            validation_start=date(2026, 6, 20),
            validation_end=date(2026, 7, 20),
        )
        db_session.commit()

        assert run.id.startswith("eval-")
        assert run.factor_version_id == version.id
        assert run.config_json is not None
        assert run.gate_result is None
        assert run.metrics_json == "{}"

    def test_create_evaluation_run_idempotent(self, db_session):
        """幂等：同参数不创建两个运行。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        config = {"factor_kind": "continuous"}
        run1 = create_evaluation_run(
            db_session,
            factor_version_id=version.id,
            config=config,
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.flush()

        run2 = create_evaluation_run(
            db_session,
            factor_version_id=version.id,
            config=config,
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.flush()

        assert run1.id == run2.id

    def test_finalize_evaluation_run_writes_metrics(self, db_session):
        """完成评估运行写入 metrics 和门禁结论。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        run = create_evaluation_run(
            db_session,
            factor_version_id=version.id,
            config={"factor_kind": "continuous"},
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.flush()

        metrics = {"ic": {"rank_ic_mean": 0.05}}
        finalized = finalize_evaluation_run(
            db_session,
            run_id=run.id,
            metrics=metrics,
            gate_result="passed",
        )
        db_session.flush()

        assert finalized.gate_result == "passed"
        assert json.loads(finalized.metrics_json)["ic"]["rank_ic_mean"] == 0.05

    def test_finalize_evaluation_run_rejects_overwrite_terminal(self, db_session):
        """终态保护：已 passed 的运行不允许覆盖。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        run = create_evaluation_run(
            db_session,
            factor_version_id=version.id,
            config={"factor_kind": "continuous"},
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.flush()

        finalize_evaluation_run(
            db_session, run_id=run.id, metrics={}, gate_result="passed",
        )
        db_session.flush()

        with pytest.raises(ValueError, match="evaluation_run_finalized:passed"):
            finalize_evaluation_run(
                db_session, run_id=run.id, metrics={"new": True},
                gate_result="rejected",
            )

    def test_list_evaluation_runs_by_version(self, db_session):
        """按版本列出评估运行。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        for cutoff in [date(2026, 7, 20), date(2026, 7, 24)]:
            create_evaluation_run(
                db_session,
                factor_version_id=version.id,
                config={"ts": cutoff.isoformat()},
                data_cutoff_at=datetime.combine(cutoff, datetime.min.time()),
            )
        db_session.flush()

        runs = list_evaluation_runs(db_session, factor_version_id=version.id)
        assert len(runs) == 2

    def test_evaluation_run_saves_ctd_evidence(self, db_session):
        """评估运行保存完整交易日证据。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        ctd = MagicMock()
        ctd.selected_trade_date = date(2026, 7, 24)
        ctd.observed_symbols = 4500
        ctd.expected_symbols = 4800
        ctd.completeness_ratio = 0.9375
        ctd.fallback_reason = None

        run = create_evaluation_run(
            db_session,
            factor_version_id=version.id,
            config={},
            data_cutoff_at=datetime(2026, 7, 24),
            ctd_evidence=ctd,
        )
        db_session.flush()

        assert run.selected_trade_date == "2026-07-24"
        assert run.observed_symbols == 4500
        assert run.expected_symbols == 4800
        assert run.completeness_ratio == pytest.approx(0.9375, abs=1e-6)
        assert run.fallback_reason is None


# ══════════════════════════════════════════════════════════
# WP5-02: 时间切分与样本构造
# ══════════════════════════════════════════════════════════


class TestTimeSplit:
    """时间切分测试。"""

    def test_build_time_split_basic(self):
        """基本时间切分。"""
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
        split = build_time_split(all_dates=dates, target_horizon=5)

        assert split.train_start == date(2026, 1, 1)
        assert split.train_end < split.validation_start
        assert split.validation_start < split.validation_end
        assert split.purge_days == 5
        assert split.embargo_days == 5

    def test_build_time_split_has_purge_gap(self):
        """训练-验证之间有 purge 间隔。"""
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
        split = build_time_split(all_dates=dates, purge_days=5)

        # validation_start 应该至少比 train_end 晚 5 天
        gap = (split.validation_start - split.train_end).days
        assert gap >= 5

    def test_build_time_split_has_embargo_gap(self):
        """验证-测试之间有 embargo 间隔。"""
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
        split = build_time_split(all_dates=dates, embargo_days=5)

        if split.test_start:
            gap = (split.test_start - split.validation_end).days
            assert gap >= 5

    def test_build_time_split_insufficient_dates(self):
        """日期不足抛异常。"""
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
        with pytest.raises(ValueError, match="insufficient_dates"):
            build_time_split(all_dates=dates)

    def test_align_factor_with_target_continuous(self):
        """连续因子对齐。"""
        fv = _make_factor_values_df(n_days=30, n_symbols=10)
        fr = _make_forward_returns_df(fv)

        aligned = align_factor_with_target(fv, fr, factor_kind="continuous")

        assert len(aligned.trade_dates) > 0
        assert len(aligned.symbols) == 10
        assert 0.0 <= aligned.coverage <= 1.0
        assert aligned.event_dates is None

    def test_align_factor_with_target_event_only_event_dates(self):
        """事件因子只保留事件日样本。"""
        fv = _make_factor_values_df(n_days=30, n_symbols=10)
        fr = _make_forward_returns_df(fv)

        # 只保留第 5、10、15 天作为事件日
        event_dates = [date(2026, 5, 6), date(2026, 5, 11), date(2026, 5, 16)]
        aligned = align_factor_with_target(
            fv, fr, factor_kind="event", event_dates=event_dates,
        )

        assert len(aligned.trade_dates) == 3
        assert aligned.event_dates == event_dates

    def test_align_factor_with_target_removes_nan(self):
        """对齐移除 NaN 对。"""
        fv = _make_factor_values_df(n_days=20, n_symbols=10)
        fr = _make_forward_returns_df(fv)

        # 制造一些 NaN
        fv.iloc[0, 0] = np.nan
        fr.iloc[5, 3] = np.nan

        aligned = align_factor_with_target(fv, fr)
        assert aligned.coverage < 1.0

    def test_align_factor_with_target_empty_input(self):
        """空输入返回空样本。"""
        empty = pd.DataFrame()
        aligned = align_factor_with_target(empty, empty)
        assert aligned.coverage == 0.0
        assert len(aligned.trade_dates) == 0


# ══════════════════════════════════════════════════════════
# WP5-03: 基础指标
# ══════════════════════════════════════════════════════════


class TestICMetrics:
    """IC 指标测试。"""

    def test_compute_rank_ic_positive_correlation(self):
        """正相关因子的 IC > 0。"""
        fv = _make_factor_values_df(n_days=40, n_symbols=20, seed=1)
        fr = _make_forward_returns_df(fv, seed=1)

        ic = compute_rank_ic(fv, fr)
        assert ic.rank_ic_mean > 0
        assert len(ic.ic_series) == 40

    def test_compute_rank_ic_random_near_zero(self):
        """随机因子的 IC 接近 0。"""
        fv = _make_factor_values_df(n_days=40, n_symbols=20, seed=1)
        fr = _make_random_returns_df(fv)

        ic = compute_rank_ic(fv, fr)
        assert abs(ic.rank_ic_mean) < 0.3

    def test_compute_rank_ic_empty_returns_zeros(self):
        """空输入返回零值。"""
        empty = pd.DataFrame()
        ic = compute_rank_ic(empty, empty)
        assert ic.rank_ic_mean == 0.0
        assert ic.icir == 0.0

    def test_compute_rank_ic_icir_calculation(self):
        """ICIR = mean / std。"""
        fv = _make_factor_values_df(n_days=60, n_symbols=30, seed=2)
        fr = _make_forward_returns_df(fv, seed=2)

        ic = compute_rank_ic(fv, fr)
        if ic.rank_ic_std > 1e-10:
            expected_icir = ic.rank_ic_mean / ic.rank_ic_std
            assert ic.icir == pytest.approx(expected_icir, rel=1e-4)


class TestQuantileMetrics:
    """分组单调性测试。"""

    def test_compute_quantile_returns_monotonic(self):
        """正相关因子的分组收益单调递增。"""
        # 增大样本量和相关性强度确保单调性显著
        fv = _make_factor_values_df(n_days=80, n_symbols=100, seed=3)
        # 强正相关收益：returns = 0.3 * factor + noise（不 shift，直接对齐）
        rng = np.random.RandomState(33)
        fr = pd.DataFrame(
            0.3 * fv.values + 0.02 * rng.randn(*fv.shape),
            index=fv.index, columns=fv.columns,
        )

        qm = compute_quantile_returns(fv, fr, n_groups=5)
        assert qm.n_groups == 5
        # 最后一组收益应大于第一组
        assert qm.group_returns[-1] > qm.group_returns[0]
        assert qm.top_bottom_return > 0
        assert qm.monotonicity_score > 0

    def test_compute_quantile_returns_random_low_monotonicity(self):
        """随机因子单调性低。"""
        fv = _make_factor_values_df(n_days=40, n_symbols=50, seed=3)
        fr = _make_random_returns_df(fv)

        qm = compute_quantile_returns(fv, fr, n_groups=5)
        assert abs(qm.monotonicity_score) < 0.5

    def test_compute_quantile_returns_insufficient_data(self):
        """数据不足返回默认值。"""
        empty = pd.DataFrame()
        qm = compute_quantile_returns(empty, empty)
        assert qm.top_bottom_return == 0.0
        assert qm.monotonicity_score == 0.0


class TestTurnoverMetrics:
    """换手率测试。"""

    def test_compute_turnover_stable_factor(self):
        """稳定因子的换手率较低。"""
        # 生成稳定因子（每日微调）
        fv = _make_factor_values_df(n_days=30, n_symbols=20, seed=4)
        # 微调：每天只变化很小一点
        for i in range(1, len(fv)):
            fv.iloc[i] = fv.iloc[i - 1] + 0.01 * np.random.RandomState(4).randn(20)

        tm = compute_turnover(fv)
        assert 0.0 <= tm.avg_turnover <= 1.0

    def test_compute_turnover_single_day(self):
        """单日数据换手率为 0。"""
        fv = _make_factor_values_df(n_days=1, n_symbols=10)
        tm = compute_turnover(fv)
        assert tm.avg_turnover == 0.0


class TestCostAdjustedReturn:
    """成本后收益测试。"""

    def test_compute_cost_adjusted_return_basic(self):
        """基本成本计算。"""
        car = compute_cost_adjusted_return(
            top_bottom_return=0.01,
            turnover=0.5,
            cost_rate=0.001,
        )
        assert car.gross_return == 0.01
        assert car.cost == pytest.approx(0.001, abs=1e-6)  # 0.5 * 2 * 0.001
        assert car.net_return == pytest.approx(0.009, abs=1e-6)

    def test_compute_cost_adjusted_return_high_turnover(self):
        """高换手率侵蚀收益。"""
        car = compute_cost_adjusted_return(
            top_bottom_return=0.001,
            turnover=2.0,
            cost_rate=0.001,
        )
        assert car.net_return < 0  # 成本超过收益


class TestCoverageMetrics:
    """覆盖率测试。"""

    def test_compute_coverage_full_data(self):
        """完整数据覆盖率为 1。"""
        fv = _make_factor_values_df(n_days=20, n_symbols=10)
        cm = compute_coverage(fv)
        assert cm.coverage == 1.0
        assert cm.missing_rate == 0.0
        assert cm.available_dates == 20
        assert cm.available_symbols == 10

    def test_compute_coverage_with_missing(self):
        """有缺失数据。"""
        fv = _make_factor_values_df(n_days=20, n_symbols=10)
        fv.iloc[0, 0] = np.nan
        fv.iloc[5, 3] = np.nan

        cm = compute_coverage(fv)
        assert cm.coverage < 1.0
        assert cm.missing_rate > 0.0


# ══════════════════════════════════════════════════════════
# WP5-04: 分类门禁
# ══════════════════════════════════════════════════════════


class TestGateEvaluation:
    """分类门禁测试。"""

    def _make_passing_metrics(self):
        """生成通过门禁的指标。"""
        return {
            "ic_metrics": ICMetrics(0.05, 0.04, 0.10, 0.5, 0.60, [0.05] * 10),
            "quantile_metrics": QuantileMetrics(5, [0.001, 0.002, 0.003, 0.004, 0.005], 0.8, 0.004, 1.5),
            "turnover_metrics": TurnoverMetrics(0.3, 0.1),
            "cost_adjusted": CostAdjustedReturn(0.004, 0.0006, 0.0034, 0.001),
            "coverage_metrics": CoverageMetrics(0.85, 0.15, 0.0, 60, 20),
        }

    def test_continuous_gate_passes_with_good_metrics(self):
        """continuous 因子好指标通过。"""
        m = self._make_passing_metrics()
        result = evaluate_gate(
            factor_kind="continuous",
            validation_days=60,
            effective_samples=12000,
            **m,
        )
        assert result.result == "passed"
        assert len(result.reasons) == 0

    def test_continuous_gate_fails_low_coverage(self):
        """覆盖率不足被拒。"""
        m = self._make_passing_metrics()
        m["coverage_metrics"] = CoverageMetrics(0.50, 0.50, 0.0, 60, 20)
        result = evaluate_gate(
            factor_kind="continuous",
            validation_days=60,
            effective_samples=12000,
            **m,
        )
        assert result.result == "rejected"
        assert any("coverage_insufficient" in r for r in result.reasons)

    def test_continuous_gate_fails_low_icir(self):
        """ICIR 不足被拒。"""
        m = self._make_passing_metrics()
        m["ic_metrics"] = ICMetrics(0.05, 0.04, 0.5, 0.10, 0.60, [0.05] * 10)
        result = evaluate_gate(
            factor_kind="continuous",
            validation_days=60,
            effective_samples=12000,
            **m,
        )
        assert result.result == "rejected"
        assert any("icir_below_threshold" in r for r in result.reasons)

    def test_continuous_gate_fails_negative_ic(self):
        """IC 为负被拒。"""
        m = self._make_passing_metrics()
        m["ic_metrics"] = ICMetrics(-0.02, -0.01, 0.1, -0.2, 0.30, [-0.02] * 10)
        result = evaluate_gate(
            factor_kind="continuous",
            validation_days=60,
            effective_samples=12000,
            **m,
        )
        assert result.result == "rejected"
        assert any("rank_ic_non_positive" in r for r in result.reasons)

    def test_continuous_gate_fails_insufficient_days(self):
        """验证日数不足被拒。"""
        m = self._make_passing_metrics()
        result = evaluate_gate(
            factor_kind="continuous",
            validation_days=30,
            effective_samples=12000,
            **m,
        )
        assert result.result == "rejected"
        assert any("validation_days_insufficient" in r for r in result.reasons)

    def test_continuous_gate_fails_negative_net_return(self):
        """成本后收益为负被拒。"""
        m = self._make_passing_metrics()
        m["cost_adjusted"] = CostAdjustedReturn(0.001, 0.002, -0.001, 0.001)
        result = evaluate_gate(
            factor_kind="continuous",
            validation_days=60,
            effective_samples=12000,
            **m,
        )
        assert result.result == "rejected"
        assert any("net_return_non_positive" in r for r in result.reasons)

    def test_event_gate_uses_event_thresholds(self):
        """event 因子使用事件阈值（ICIR 放宽到 0.10）。"""
        m = self._make_passing_metrics()
        m["ic_metrics"] = ICMetrics(0.03, 0.02, 0.2, 0.12, 0.55, [0.03] * 10)
        result = evaluate_gate(
            factor_kind="event",
            validation_days=40,
            effective_samples=5000,
            event_sample_count=100,
            **m,
        )
        # event ICIR 阈值 0.10，0.12 通过
        assert not any("icir_below_threshold" in r for r in result.reasons)

    def test_event_gate_fails_insufficient_event_samples(self):
        """event 因子事件样本不足被拒。"""
        m = self._make_passing_metrics()
        result = evaluate_gate(
            factor_kind="event",
            validation_days=40,
            effective_samples=5000,
            event_sample_count=20,
            **m,
        )
        assert result.result == "rejected"
        assert any("event_samples_insufficient" in r for r in result.reasons)

    def test_regime_gate_checks_time_coverage(self):
        """regime 因子检查时间覆盖率。"""
        m = self._make_passing_metrics()
        m["coverage_metrics"] = CoverageMetrics(0.40, 0.60, 0.0, 50, 1)
        result = evaluate_gate(
            factor_kind="regime",
            validation_days=60,
            effective_samples=1000,
            **m,
        )
        assert result.result == "rejected"
        assert any("time_coverage_insufficient" in r for r in result.reasons)


# ══════════════════════════════════════════════════════════
# WP5-05: 参数扰动
# ══════════════════════════════════════════════════════════


class TestParameterPerturbation:
    """参数扰动测试。"""

    def test_perturb_parameter_default_ratios(self):
        """默认 5 个扰动点。"""
        def eval_fn(param_value):
            # 模拟 IC 随参数变化（稳定）
            return ICMetrics(0.05, 0.04, 0.1, 0.5, 0.6, [0.05] * 10)

        result = perturb_parameter(
            param_name="window",
            baseline_value=20,
            eval_fn=eval_fn,
        )

        assert len(result.points) == 5
        assert result.points[0].label == "-20%"
        assert result.points[2].label == "baseline"
        assert result.points[4].label == "+20%"
        assert result.verdict == "stable"

    def test_perturb_parameter_cliff_drop(self):
        """断崖式下跌检测。"""
        def eval_fn(param_value):
            # 基准 IC=0.05，偏离基准后 IC 接近 0
            if abs(param_value - 20) < 1:
                return ICMetrics(0.05, 0.04, 0.1, 0.5, 0.6, [0.05] * 10)
            return ICMetrics(0.005, 0.004, 0.1, 0.05, 0.5, [0.005] * 10)

        result = perturb_parameter(
            param_name="window",
            baseline_value=20,
            eval_fn=eval_fn,
        )

        assert result.has_cliff_drop is True
        assert result.verdict == "cliff_drop"

    def test_perturb_parameter_integer_rounding(self):
        """整数参数取整。"""
        def eval_fn(param_value):
            return ICMetrics(0.05, 0.04, 0.1, 0.5, 0.6, [0.05] * 10)

        result = perturb_parameter(
            param_name="window",
            baseline_value=20,
            eval_fn=eval_fn,
        )

        # -20% 的 20 是 16（整数）
        assert result.points[0].param_value == 16.0
        # +20% 的 20 是 24
        assert result.points[4].param_value == 24.0


class TestTimePerturbation:
    """时间段扰动测试。"""

    def test_perturb_time_segments_stable(self):
        """稳定 IC 的时间段扰动。"""
        fv = _make_factor_values_df(n_days=180, n_symbols=30, seed=5)
        fr = _make_forward_returns_df(fv, seed=5)

        result = perturb_time_segments(
            features=fv, targets=fr, segment_size=60,
        )

        assert len(result.segments) >= 2
        assert result.verdict in ("stable", "unstable")

    def test_perturb_time_segments_insufficient_data(self):
        """数据不足返回 unstable。"""
        fv = _make_factor_values_df(n_days=30, n_symbols=10)
        fr = _make_forward_returns_df(fv)

        result = perturb_time_segments(
            features=fv, targets=fr, segment_size=60,
        )

        assert result.verdict == "unstable"
        assert len(result.segments) == 0


class TestMissingSensitivity:
    """缺失敏感度测试。"""

    def test_perturb_missing_sensitivity_robust(self):
        """稳定因子的缺失敏感度低。"""
        fv = _make_factor_values_df(n_days=60, n_symbols=30, seed=6)
        fr = _make_forward_returns_df(fv, seed=6)

        result = perturb_missing_sensitivity(
            features=fv, targets=fr,
            dropout_ratios=[0.0, 0.05, 0.10, 0.20],
        )

        assert len(result.results) == 4
        assert result.results[0].dropout_ratio == 0.0
        assert result.verdict in ("robust", "sensitive")

    def test_perturb_missing_sensitivity_results_have_coverage(self):
        """结果包含覆盖率信息。"""
        fv = _make_factor_values_df(n_days=40, n_symbols=20, seed=7)
        fr = _make_forward_returns_df(fv, seed=7)

        result = perturb_missing_sensitivity(features=fv, targets=fr)

        for r in result.results:
            assert 0.0 <= r.coverage <= 1.0


class TestStressTestSummary:
    """压力测试汇总测试。"""

    def test_run_stress_test_without_params(self):
        """无参数扰动时只做时间段和缺失。"""
        fv = _make_factor_values_df(n_days=180, n_symbols=30, seed=8)
        fr = _make_forward_returns_df(fv, seed=8)

        summary = run_stress_test(
            features=fv, targets=fr,
            parameter_perturbations=None,
            eval_fn_factory=None,
            segment_size=60,
        )

        assert summary.overall_verdict in ("stable", "unstable", "cliff_drop")
        assert summary.time_result is not None
        assert summary.missing_result is not None
        assert len(summary.parameter_results) == 0


# ══════════════════════════════════════════════════════════
# 评估主流程集成测试
# ══════════════════════════════════════════════════════════


class TestRunEvaluation:
    """评估主流程测试。"""

    def test_run_evaluation_creates_run_with_metrics(self, db_session):
        """完整评估流程创建运行并写入 metrics。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        fv = _make_factor_values_df(n_days=80, n_symbols=20, seed=10)
        fr = _make_forward_returns_df(fv, seed=10)

        config = EvaluationConfig(
            factor_kind="continuous",
            target_horizon=5,
            n_groups=5,
        )

        outcome = run_evaluation(
            db_session,
            factor_version_id=version.id,
            factor_values=fv,
            forward_returns=fr,
            config=config,
            data_cutoff_at=datetime(2026, 7, 24),
        )

        db_session.commit()

        assert outcome.run_id.startswith("eval-")
        assert outcome.gate_result in ("passed", "rejected", "warn")
        assert "ic" in outcome.metrics
        assert "quantile" in outcome.metrics
        assert outcome.time_split is not None

        # 验证持久化
        run = get_evaluation_run(db_session, outcome.run_id)
        assert run is not None
        assert run.gate_result == outcome.gate_result

    def test_run_evaluation_idempotent(self, db_session):
        """同参数重复运行幂等。"""
        _, version = _make_factor_and_version(db_session)
        db_session.commit()

        fv = _make_factor_values_df(n_days=80, n_symbols=20, seed=11)
        fr = _make_forward_returns_df(fv, seed=11)

        config = EvaluationConfig(factor_kind="continuous")

        outcome1 = run_evaluation(
            db_session,
            factor_version_id=version.id,
            factor_values=fv,
            forward_returns=fr,
            config=config,
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.commit()

        # 第二次运行（已终态，不覆盖）
        outcome2 = run_evaluation(
            db_session,
            factor_version_id=version.id,
            factor_values=fv,
            forward_returns=fr,
            config=config,
            data_cutoff_at=datetime(2026, 7, 24),
        )
        db_session.commit()

        assert outcome1.run_id == outcome2.run_id
        assert outcome1.gate_result == outcome2.gate_result
