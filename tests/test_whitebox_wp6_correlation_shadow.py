"""WP6 白盒测试：相关性治理、Shadow 观测与审批流程。

覆盖：
- WP6-01 相关矩阵和聚类（spearman/pearson/rank、高相关对、层次聚类、代表因子）
- WP6-02 残差增量评估（正交化、残差 IC、valuable/marginal/redundant 判定）
- WP6-03 Shadow 每日观测（幂等、未来日期拒绝、有效性判定、有效天数统计）
- WP6-04 衰减和数据健康告警（IC 衰减、覆盖突降、常数化、缺失异常）
- WP6-05 人工审批流程（申请门禁、批准激活、驳回、自动隔离）
- WP6-06 相关性治理综合流程（run_correlation_governance 集成）
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.models.factor import Factor
from app.models.factor_evaluation import EvaluationRun, ShadowObservation, TransitionAudit
from app.models.factor_model import FactorVersion
from app.schemas.factor_library import FactorTransitionRequest
from app.services.factors.factor_correlation import (
    DEFAULT_CLUSTER_THRESHOLD,
    EXTREME_CORRELATION_THRESHOLD,
    HIGH_CORRELATION_THRESHOLD,
    MIN_RESIDUAL_IC_TSTAT,
    MIN_RESIDUAL_ICIR,
    ClusterReport,
    CorrelationGovernanceReport,
    CorrelationMatrix,
    FactorCluster,
    ResidualIncrementResult,
    cluster_factors,
    compute_correlation_matrix,
    compute_residual_ic,
    run_correlation_governance,
)
from app.services.factors.factor_lifecycle import execute_transition
from app.services.factors.factor_shadow import (
    BLOCKED,
    CONSTANT_FACTOR_IC_STD,
    COVERAGE_DROP_THRESHOLD,
    DEGRADED,
    HEALTHY,
    IC_DECAY_THRESHOLD,
    MIN_COMPLETENESS_RATIO,
    MIN_VALID_SHADOW_DAYS,
    ActivationRequest,
    ActivationResult,
    HealthAlert,
    ShadowHealthReport,
    ShadowObservationInput,
    ShadowObservationResult,
    approve_activation,
    auto_quarantine,
    count_consecutive_valid_shadow_days,
    count_valid_shadow_days,
    get_shadow_observations,
    is_shadow_observation_complete,
    record_shadow_observation,
    reject_activation,
    request_activation,
    run_shadow_health_check,
)


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════


def _make_factor_and_version(
    db_session,
    factor_code="test_factor_wp6_001",
    lifecycle_status="shadow",
):
    """创建测试因子和版本（默认 shadow 状态）。"""
    factor = Factor(
        code=factor_code,
        name=f"WP6 Test Factor {factor_code}",
        category="test",
        direction="higher_better",
        status="active",
        source_type="daily_bars",
        frequency="daily",
        default_missing_policy="exclude",
        lifecycle_status=lifecycle_status,
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
        execution_plan_hash="wp6test" + factor_code[-3:],
        formula_ast_json="{}",
        data_dependencies_json="{}",
        created_via="manual",
    )
    db_session.add(version)
    db_session.flush()
    return factor, version


def _ensure_evidence_run(db_session, factor_version_id: int, run_id: str = "eval-1-abc-20260801") -> None:
    """为 wp6 审批流程需要的 evidence_run_id 外键插入占位评估运行记录（不存在时）。"""
    existing = db_session.get(EvaluationRun, run_id)
    if existing is not None:
        return
    run = EvaluationRun(
        id=run_id,
        factor_version_id=factor_version_id,
        data_cutoff_at=datetime(2026, 8, 1),
        config_json="{}",
        metrics_json="{}",
        gate_result="passed",
    )
    db_session.add(run)
    db_session.flush()


def _make_panel_df(
    n_days=40,
    n_symbols=30,
    n_factors=3,
    start_date=date(2026, 5, 1),
    seed=42,
    correlations=None,
):
    """生成测试用面板 DataFrame（MultiIndex: trade_date, symbol）。

    correlations: 可选 dict {(i, j): corr} 指定因子间相关性。
    """
    rng = np.random.RandomState(seed)
    dates = [start_date + timedelta(days=i) for i in range(n_days)]
    symbols = [f"S{i:04d}" for i in range(n_symbols)]

    # 生成独立基础因子
    base = rng.randn(n_days, n_symbols, n_factors)

    # 应用相关性
    if correlations:
        for (i, j), corr in correlations.items():
            if i < n_factors and j < n_factors and i != j:
                # 使因子 j = corr * 因子 i + sqrt(1-corr^2) * 噪声
                base[:, :, j] = (
                    corr * base[:, :, i]
                    + np.sqrt(max(0.0, 1.0 - corr * corr)) * base[:, :, j]
                )

    multi_index = pd.MultiIndex.from_product(
        [dates, symbols], names=["trade_date", "symbol"]
    )
    data = {
        f"f{k}": base[:, :, k].flatten() for k in range(n_factors)
    }
    return pd.DataFrame(data, index=multi_index)


def _make_target_series(factor_df, factor_col="f0", ic=0.3, seed=99):
    """生成与指定因子有 IC 的目标收益序列。"""
    rng = np.random.RandomState(seed)
    factor_values = factor_df[factor_col].values
    noise = rng.randn(len(factor_values))
    target = ic * factor_values + (1.0 - ic * ic) ** 0.5 * noise * 0.5
    return pd.Series(target, index=factor_df.index, name="target")


def _record_n_valid_observations(
    db_session,
    factor_id,
    factor_version_id,
    n_days,
    start_date=None,
    ic_value=0.05,
    coverage=0.95,
    completeness=0.95,
    vary_ic=True,
):
    """记录 n_days 个有效 Shadow 观测。

    vary_ic: 添加小变化避免触发 constant_factor 告警（IC 标准差 > 1e-6）。
    """
    if start_date is None:
        start_date = date.today() - timedelta(days=n_days + 5)
    for i in range(n_days):
        td = start_date + timedelta(days=i)
        # 添加小变化避免常数化告警
        actual_ic = ic_value + 0.002 * (i % 5 - 2) if vary_ic else ic_value
        inp = ShadowObservationInput(
            factor_id=factor_id,
            factor_version_id=factor_version_id,
            trade_date=td.isoformat(),
            observed_symbols=100,
            expected_symbols=100,
            completeness_ratio=completeness,
            ic_value=actual_ic,
            coverage=coverage,
            turnover=0.1,
            metrics={"test": True},
        )
        record_shadow_observation(db_session, inp=inp)
    db_session.flush()


# ══════════════════════════════════════════════════════════
# WP6-01: 相关矩阵和聚类
# ══════════════════════════════════════════════════════════


class TestCorrelationMatrix:
    """相关矩阵计算测试。"""

    def test_empty_dataframe_returns_empty_matrix(self):
        """空 DataFrame 返回空矩阵。"""
        empty_df = pd.DataFrame()
        result = compute_correlation_matrix(empty_df, method="spearman")
        assert result.matrix.empty
        assert result.method == "spearman"
        assert result.n_dates == 0
        assert result.n_symbols_avg == 0.0
        assert result.factor_codes == []

    def test_spearman_method_on_independent_factors(self):
        """spearman 方法计算独立因子的相关矩阵（应接近 0）。"""
        df = _make_panel_df(n_factors=3, seed=11)
        result = compute_correlation_matrix(df, method="spearman")
        assert result.method == "spearman"
        assert result.n_dates == 40
        assert result.n_symbols_avg == 30.0
        assert set(result.factor_codes) == {"f0", "f1", "f2"}
        # 独立因子间相关系数应接近 0
        off_diag = [
            result.matrix.loc["f0", "f1"],
            result.matrix.loc["f0", "f2"],
            result.matrix.loc["f1", "f2"],
        ]
        for v in off_diag:
            assert abs(v) < 0.3

    def test_pearson_method(self):
        """pearson 方法。"""
        df = _make_panel_df(n_factors=2, seed=22)
        result = compute_correlation_matrix(df, method="pearson")
        assert result.method == "pearson"
        # 对角线为 1
        assert abs(result.matrix.loc["f0", "f0"] - 1.0) < 1e-9

    def test_rank_method_equivalent_to_spearman(self):
        """rank 方法应与 spearman 接近（截面 rank 后 pearson）。"""
        df = _make_panel_df(n_factors=2, seed=33, correlations={(0, 1): 0.8})
        result_rank = compute_correlation_matrix(df, method="rank")
        result_spearman = compute_correlation_matrix(df, method="spearman")
        # 高相关时两种方法应都检测到
        assert abs(result_rank.matrix.loc["f0", "f1"]) > 0.5
        assert abs(result_spearman.matrix.loc["f0", "f1"]) > 0.5

    def test_high_correlation_detected(self):
        """高相关因子对被检测到。"""
        df = _make_panel_df(
            n_factors=3, seed=44, correlations={(0, 1): 0.85, (0, 2): 0.2}
        )
        result = compute_correlation_matrix(df, method="spearman")
        pairs = result.high_correlation_pairs(threshold=HIGH_CORRELATION_THRESHOLD)
        # 应检测到 (f0, f1) 高相关对
        assert len(pairs) >= 1
        codes_in_pairs = {p[0] for p in pairs} | {p[1] for p in pairs}
        assert "f0" in codes_in_pairs and "f1" in codes_in_pairs
        # 按绝对值降序
        for i in range(len(pairs) - 1):
            assert abs(pairs[i][2]) >= abs(pairs[i + 1][2])

    def test_high_correlation_pairs_excludes_self(self):
        """高相关对排除自相关。"""
        df = _make_panel_df(n_factors=2, seed=55)
        result = compute_correlation_matrix(df, method="spearman")
        pairs = result.high_correlation_pairs(threshold=0.0)
        # 不应有 (f0, f0) 这种自相关对
        for a, b, _ in pairs:
            assert a != b

    def test_get_method_returns_correlation(self):
        """get 方法返回指定因子对的相关系数。"""
        df = _make_panel_df(n_factors=2, seed=66)
        result = compute_correlation_matrix(df, method="spearman")
        v = result.get("f0", "f1")
        assert v is not None
        assert -1.0 <= v <= 1.0

    def test_get_method_unknown_code_returns_none(self):
        """get 方法对未知 code 返回 None。"""
        df = _make_panel_df(n_factors=2, seed=77)
        result = compute_correlation_matrix(df, method="spearman")
        assert result.get("f0", "unknown") is None
        assert result.get("unknown", "f0") is None

    def test_single_index_dataframe(self):
        """单索引 DataFrame（无 MultiIndex）也能计算。"""
        rng = np.random.RandomState(88)
        df = pd.DataFrame(
            {
                "f0": rng.randn(100),
                "f1": rng.randn(100),
            }
        )
        result = compute_correlation_matrix(df, method="pearson")
        assert result.n_dates == 1
        assert result.n_symbols_avg == 100.0
        assert set(result.factor_codes) == {"f0", "f1"}

    def test_to_dict_serializable(self):
        """to_dict 返回可序列化字典。"""
        df = _make_panel_df(n_factors=2, seed=99)
        result = compute_correlation_matrix(df, method="spearman")
        d = result.to_dict()
        assert "matrix" in d
        assert "method" in d
        assert "high_correlation_pairs" in d
        # 可 JSON 序列化（matrix 内部是 dict）
        json.dumps(d, default=str)


class TestClusterFactors:
    """层次聚类测试。"""

    def test_empty_matrix_returns_empty_report(self):
        """空矩阵返回空聚类报告。"""
        empty_corr = CorrelationMatrix(
            matrix=pd.DataFrame(),
            method="spearman",
            n_dates=0,
            n_symbols_avg=0.0,
            factor_codes=[],
        )
        report = cluster_factors(empty_corr, threshold=0.6)
        assert report.clusters == []
        assert report.n_factors == 0
        assert report.n_clusters == 0
        assert report.orphans == []

    def test_single_factor_becomes_orphan(self):
        """单因子成为 orphan（未聚入簇）。"""
        df = _make_panel_df(n_factors=1, seed=11)
        corr = compute_correlation_matrix(df, method="spearman")
        report = cluster_factors(corr, threshold=0.6)
        assert report.n_factors == 1
        assert report.n_clusters == 0
        assert "f0" in report.orphans

    def test_high_correlation_factors_merged_into_cluster(self):
        """高相关因子被合并到同一簇。"""
        df = _make_panel_df(
            n_factors=3,
            seed=22,
            correlations={(0, 1): 0.85, (0, 2): 0.1, (1, 2): 0.1},
        )
        corr = compute_correlation_matrix(df, method="spearman")
        report = cluster_factors(corr, threshold=0.6)
        assert report.n_clusters >= 1
        # f0 和 f1 应在同一簇
        cluster_with_f0 = next(c for c in report.clusters if "f0" in c.members)
        assert "f1" in cluster_with_f0.members
        # f2 应是 orphan（与 f0/f1 都低相关）
        assert "f2" in report.orphans

    def test_cluster_intra_max_correlation(self):
        """簇内最大相关系数正确计算。"""
        df = _make_panel_df(
            n_factors=2, seed=33, correlations={(0, 1): 0.9}
        )
        corr = compute_correlation_matrix(df, method="spearman")
        report = cluster_factors(corr, threshold=0.6)
        assert report.n_clusters == 1
        cluster = report.clusters[0]
        assert cluster.intra_max_correlation > 0.6

    def test_representative_factor_selected_by_score(self):
        """代表因子根据 representative_scores 选择（分数最高者）。"""
        df = _make_panel_df(
            n_factors=2, seed=44, correlations={(0, 1): 0.85}
        )
        corr = compute_correlation_matrix(df, method="spearman")
        # f1 分数更高
        scores = {"f0": 0.5, "f1": 0.8}
        report = cluster_factors(corr, threshold=0.6, representative_scores=scores)
        assert report.n_clusters == 1
        assert report.clusters[0].representative == "f1"

    def test_representative_default_first_member(self):
        """无 representative_scores 时取首个成员（按字母序）。"""
        df = _make_panel_df(
            n_factors=2, seed=55, correlations={(0, 1): 0.85}
        )
        corr = compute_correlation_matrix(df, method="spearman")
        report = cluster_factors(corr, threshold=0.6, representative_scores=None)
        assert report.n_clusters == 1
        # members 排序后第一个
        assert report.clusters[0].representative == sorted(report.clusters[0].members)[0]

    def test_low_threshold_merges_more(self):
        """低阈值合并更多因子。"""
        df = _make_panel_df(
            n_factors=3,
            seed=66,
            correlations={(0, 1): 0.5, (1, 2): 0.5, (0, 2): 0.3},
        )
        corr = compute_correlation_matrix(df, method="spearman")
        # 高阈值：3 个独立
        report_high = cluster_factors(corr, threshold=0.8)
        assert report_high.n_clusters == 0
        assert len(report_high.orphans) == 3
        # 低阈值：可能合并
        report_low = cluster_factors(corr, threshold=0.4)
        assert report_low.n_clusters >= 1

    def test_to_dict_serializable(self):
        """to_dict 可序列化。"""
        df = _make_panel_df(
            n_factors=2, seed=77, correlations={(0, 1): 0.85}
        )
        corr = compute_correlation_matrix(df, method="spearman")
        report = cluster_factors(corr, threshold=0.6)
        d = report.to_dict()
        json.dumps(d, default=str)


# ══════════════════════════════════════════════════════════
# WP6-02: 残差增量评估
# ══════════════════════════════════════════════════════════


class TestResidualIncrement:
    """残差增量评估测试。"""

    def test_empty_references_uses_candidate_directly(self):
        """空引用时直接用候选因子计算 IC（无正交化）。"""
        df = _make_panel_df(n_factors=1, seed=11)
        candidate = df["f0"]
        target = _make_target_series(df, factor_col="f0", ic=0.5)
        references = pd.DataFrame(index=df.index)

        result = compute_residual_ic(
            candidate=candidate,
            references=references,
            target=target,
            candidate_code="f0",
        )
        assert result.candidate_code == "f0"
        assert result.reference_codes == []
        # 原始 IC 与残差 IC 应接近（无正交化）
        assert abs(result.original_ic_mean) > 0.1
        assert abs(result.residual_ic_mean - result.original_ic_mean) < 0.2

    def test_redundant_factor_has_near_zero_residual_ic(self):
        """冗余因子（与引用高度共线）残差 IC 接近 0。"""
        df = _make_panel_df(
            n_factors=2, seed=22, correlations={(0, 1): 0.95}
        )
        # f1 是 f0 的近似线性变换，target 与 f0 相关
        candidate = df["f1"]
        references = df[["f0"]]
        target = _make_target_series(df, factor_col="f0", ic=0.4)

        result = compute_residual_ic(
            candidate=candidate,
            references=references,
            target=target,
            candidate_code="f1",
        )
        # 残差 IC 应显著低于原始 IC
        assert abs(result.residual_ic_mean) < abs(result.original_ic_mean)
        # verdict 应为 redundant 或 marginal
        assert result.verdict in ("redundant", "marginal")

    def test_valuable_factor_has_significant_residual_ic(self):
        """独立增量因子残差 IC 显著。"""
        rng = np.random.RandomState(33)
        n_days, n_symbols = 40, 30
        dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(n_days)]
        symbols = [f"S{i:04d}" for i in range(n_symbols)]
        multi_index = pd.MultiIndex.from_product(
            [dates, symbols], names=["trade_date", "symbol"]
        )

        # 已有因子 f0 与 target 弱相关
        f0 = rng.randn(n_days * n_symbols)
        target = 0.1 * f0 + 0.4 * rng.randn(n_days * n_symbols)
        # 候选因子 f1 与 f0 独立，但与 target 强相关
        f1 = rng.randn(n_days * n_symbols)
        target = target + 0.3 * f1  # f1 有独立增量

        df = pd.DataFrame({"f0": f0, "f1": f1}, index=multi_index)
        target_series = pd.Series(target, index=multi_index, name="target")

        result = compute_residual_ic(
            candidate=df["f1"],
            references=df[["f0"]],
            target=target_series,
            candidate_code="f1",
        )
        # f1 应有独立增量价值
        assert result.verdict in ("valuable", "marginal")
        assert result.has_incremental_value is True
        assert abs(result.residual_ic_mean) > 0.05

    def test_insufficient_samples_returns_redundant(self):
        """样本不足返回 redundant。"""
        # 9 天 × 3 标的 = 27 个样本（< 30）
        dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(9)]
        symbols = ["S0001", "S0002", "S0003"]
        multi_index = pd.MultiIndex.from_product(
            [dates, symbols], names=["trade_date", "symbol"]
        )
        rng = np.random.RandomState(44)
        df = pd.DataFrame(
            {
                "f0": rng.randn(27),
                "f1": rng.randn(27),
            },
            index=multi_index,
        )
        target = pd.Series(rng.randn(27), index=multi_index, name="target")

        result = compute_residual_ic(
            candidate=df["f1"],
            references=df[["f0"]],
            target=target,
            candidate_code="f1",
        )
        assert result.verdict == "redundant"
        assert "insufficient_samples" in result.reasons
        assert result.residual_ic_mean == 0.0

    def test_provided_original_ic_used(self):
        """显式提供 original_ic_mean/original_icir 时不现场计算。"""
        df = _make_panel_df(n_factors=2, seed=55, correlations={(0, 1): 0.5})
        target = _make_target_series(df, factor_col="f0", ic=0.3)

        result = compute_residual_ic(
            candidate=df["f1"],
            references=df[["f0"]],
            target=target,
            candidate_code="f1",
            original_ic_mean=0.2,
            original_icir=0.5,
        )
        assert result.original_ic_mean == 0.2
        assert result.original_icir == 0.5

    def test_incremental_ratio_computed(self):
        """增量比例正确计算。"""
        df = _make_panel_df(n_factors=2, seed=66, correlations={(0, 1): 0.3})
        target = _make_target_series(df, factor_col="f0", ic=0.4)

        result = compute_residual_ic(
            candidate=df["f1"],
            references=df[["f0"]],
            target=target,
            candidate_code="f1",
        )
        # incremental_ratio = residual_ic_mean / original_ic_mean
        if abs(result.original_ic_mean) > 1e-10:
            expected_ratio = result.residual_ic_mean / result.original_ic_mean
            assert abs(result.incremental_ic_ratio - expected_ratio) < 1e-6

    def test_to_dict_serializable(self):
        """to_dict 可序列化。"""
        df = _make_panel_df(n_factors=2, seed=77)
        target = _make_target_series(df, factor_col="f0", ic=0.3)
        result = compute_residual_ic(
            candidate=df["f1"],
            references=df[["f0"]],
            target=target,
            candidate_code="f1",
        )
        d = result.to_dict()
        json.dumps(d, default=str)

    def test_single_index_candidate(self):
        """单索引（非 MultiIndex）候选因子也能计算。"""
        rng = np.random.RandomState(88)
        n = 200
        f0 = rng.randn(n)
        f1 = 0.8 * f0 + 0.2 * rng.randn(n)  # f1 与 f0 高相关
        target = 0.3 * f0 + 0.1 * f1 + 0.3 * rng.randn(n)

        df = pd.DataFrame({"f0": f0, "f1": f1})
        target_series = pd.Series(target, name="target")

        result = compute_residual_ic(
            candidate=df["f1"],
            references=df[["f0"]],
            target=target_series,
            candidate_code="f1",
        )
        # 单截面时 ICIR 为 0
        assert result.residual_icir == 0.0
        assert result.candidate_code == "f1"


# ══════════════════════════════════════════════════════════
# WP6-06: 相关性治理综合流程
# ══════════════════════════════════════════════════════════


class TestCorrelationGovernance:
    """相关性治理综合流程测试。"""

    def test_full_governance_run(self):
        """完整相关性治理流程。"""
        df = _make_panel_df(
            n_factors=3,
            seed=11,
            correlations={(0, 1): 0.85, (0, 2): 0.1, (1, 2): 0.1},
        )
        target = _make_target_series(df, factor_col="f0", ic=0.3)

        report = run_correlation_governance(
            df,
            target,
            method="spearman",
            cluster_threshold=0.6,
            representative_scores={"f0": 0.8, "f1": 0.5, "f2": 0.6},
        )
        assert isinstance(report, CorrelationGovernanceReport)
        assert len(report.correlation.factor_codes) == 3
        assert report.clusters.n_factors == 3
        # f0 和 f1 高相关，应至少有一对高相关
        assert len(report.high_correlation_pairs) >= 1
        # 推荐应覆盖所有因子
        assert set(report.recommendations.keys()) == {"f0", "f1", "f2"}
        # 汇总字符串非空
        assert "相关性治理完成" in report.summary

    def test_recommendations_default_keep(self):
        """无高相关的因子默认推荐 keep。"""
        df = _make_panel_df(n_factors=2, seed=22)  # 独立因子
        target = _make_target_series(df, factor_col="f0", ic=0.3)

        report = run_correlation_governance(df, target, method="spearman")
        # 无高相关对时，所有因子默认 keep
        assert all(v == "keep" for v in report.recommendations.values())

    def test_redundant_candidate_recommended_reject(self):
        """冗余候选被推荐 reject。"""
        # f1 与 f0 极高相关（接近共线）
        df = _make_panel_df(
            n_factors=2, seed=33, correlations={(0, 1): 0.98}
        )
        target = _make_target_series(df, factor_col="f0", ic=0.4)

        report = run_correlation_governance(
            df,
            target,
            method="spearman",
            cluster_threshold=0.6,
            representative_scores={"f0": 0.8, "f1": 0.3},  # f1 分数低，作为候选
        )
        # f1 应被推荐 reject 或 investigate
        assert report.recommendations["f1"] in ("reject", "investigate")

    def test_to_dict_serializable(self):
        """to_dict 可序列化。"""
        df = _make_panel_df(
            n_factors=2, seed=44, correlations={(0, 1): 0.7}
        )
        target = _make_target_series(df, factor_col="f0", ic=0.3)
        report = run_correlation_governance(df, target, method="spearman")
        d = report.to_dict()
        json.dumps(d, default=str)


# ══════════════════════════════════════════════════════════
# WP6-03: Shadow 每日观测
# ══════════════════════════════════════════════════════════


class TestShadowObservation:
    """Shadow 每日观测测试。"""

    def test_record_observation_creates_new(self, db_session):
        """记录新观测。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=date.today().isoformat(),
            observed_symbols=100,
            expected_symbols=100,
            completeness_ratio=0.95,
            ic_value=0.05,
            coverage=0.95,
            turnover=0.1,
            metrics={"test": True},
        )
        result = record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        assert result.is_new is True
        assert result.observation.id is not None
        assert result.observation.trade_date == date.today().isoformat()
        assert result.observation.is_valid_day is True
        assert result.observation.health_status == HEALTHY

    def test_record_observation_idempotent(self, db_session):
        """同因子版本同交易日幂等返回既有记录。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        td = date.today().isoformat()
        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=td,
            observed_symbols=100,
            expected_symbols=100,
            completeness_ratio=0.95,
            ic_value=0.05,
        )
        result1 = record_shadow_observation(db_session, inp=inp)
        db_session.flush()
        result2 = record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        assert result1.is_new is True
        assert result2.is_new is False
        assert result1.observation.id == result2.observation.id

    def test_future_trade_date_rejected(self, db_session):
        """未来日期不允许记录。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        future_date = (date.today() + timedelta(days=5)).isoformat()
        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=future_date,
        )
        with pytest.raises(ValueError, match="future_trade_date_not_allowed"):
            record_shadow_observation(db_session, inp=inp)

    def test_invalid_trade_date_rejected(self, db_session):
        """非法日期格式拒绝。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date="not-a-date",
        )
        with pytest.raises(ValueError, match="invalid_trade_date"):
            record_shadow_observation(db_session, inp=inp)

    def test_low_completeness_marks_invalid(self, db_session):
        """完整率 < 90% 标记为无效观察日。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=date.today().isoformat(),
            observed_symbols=80,
            expected_symbols=100,
            completeness_ratio=0.80,  # 低于 0.90
            coverage=0.80,
        )
        result = record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        assert result.is_valid_day is False
        assert result.invalid_reason == "incomplete_coverage"
        assert result.observation.is_valid_day is False

    def test_zero_observed_symbols_marks_invalid(self, db_session):
        """observed_symbols=0 标记为无效（无数据）。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=date.today().isoformat(),
            observed_symbols=0,
            completeness_ratio=0.0,
        )
        result = record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        assert result.is_valid_day is False
        assert result.invalid_reason == "no_data"

    def test_zero_coverage_marks_invalid(self, db_session):
        """coverage=0 标记为无效。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=date.today().isoformat(),
            observed_symbols=100,
            coverage=0.0,
        )
        result = record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        assert result.is_valid_day is False
        assert result.invalid_reason == "no_data"

    def test_count_valid_shadow_days(self, db_session):
        """统计有效观察天数。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 记录 5 个有效 + 2 个无效
        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=5, ic_value=0.05
        )
        # 无效日
        for i in range(2):
            td = date.today() - timedelta(days=10 + i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                observed_symbols=50,
                expected_symbols=100,
                completeness_ratio=0.50,  # 无效
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        valid_days = count_valid_shadow_days(
            db_session, factor_version_id=version.id
        )
        assert valid_days == 5

    def test_count_consecutive_valid_shadow_days(self, db_session):
        """统计连续有效观察天数。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        base = date.today() - timedelta(days=10)
        # 前 3 天有效
        for i in range(3):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05,
            )
            record_shadow_observation(db_session, inp=inp)
        # 第 4 天无效
        td_invalid = base + timedelta(days=3)
        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=td_invalid.isoformat(),
            completeness_ratio=0.50,
        )
        record_shadow_observation(db_session, inp=inp)
        # 第 5-7 天有效（最近）
        for i in range(5, 8):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05,
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        consecutive = count_consecutive_valid_shadow_days(
            db_session, factor_version_id=version.id
        )
        # 最近 3 天连续有效
        assert consecutive == 3

    def test_is_shadow_observation_complete_below_threshold(self, db_session):
        """有效天数不足 20 时 is_complete=False。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=10
        )
        db_session.commit()

        is_complete, valid_days, reason = is_shadow_observation_complete(
            db_session, factor_version_id=version.id
        )
        assert is_complete is False
        assert valid_days == 10
        assert "insufficient_valid_days" in reason

    def test_is_shadow_observation_complete_meets_threshold(self, db_session):
        """有效天数 >= 20 时 is_complete=True。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=25
        )
        db_session.commit()

        is_complete, valid_days, reason = is_shadow_observation_complete(
            db_session, factor_version_id=version.id
        )
        assert is_complete is True
        assert valid_days == 25
        assert reason is None

    def test_get_shadow_observations_ordered(self, db_session):
        """查询观测历史按交易日升序。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        base = date.today() - timedelta(days=5)
        for i in range(5):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        obs_list = get_shadow_observations(
            db_session, factor_version_id=version.id
        )
        assert len(obs_list) == 5
        # 升序
        for i in range(len(obs_list) - 1):
            assert obs_list[i].trade_date <= obs_list[i + 1].trade_date

    def test_get_shadow_observations_valid_only_filter(self, db_session):
        """valid_only 过滤只返回有效日。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        base = date.today() - timedelta(days=3)
        # 2 个有效
        for i in range(2):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        # 1 个无效
        td = base + timedelta(days=2)
        inp = ShadowObservationInput(
            factor_id=factor.id,
            factor_version_id=version.id,
            trade_date=td.isoformat(),
            completeness_ratio=0.50,
        )
        record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        all_obs = get_shadow_observations(
            db_session, factor_version_id=version.id
        )
        valid_obs = get_shadow_observations(
            db_session, factor_version_id=version.id, valid_only=True
        )
        assert len(all_obs) == 3
        assert len(valid_obs) == 2
        assert all(o.is_valid_day for o in valid_obs)


# ══════════════════════════════════════════════════════════
# WP6-04: 衰减和数据健康告警
# ══════════════════════════════════════════════════════════


class TestShadowHealthCheck:
    """Shadow 健康检查测试。"""

    def test_no_observations_returns_blocked(self, db_session):
        """无观测记录返回 blocked。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id
        )
        assert report.health_status == BLOCKED
        assert report.n_total_days == 0
        assert report.n_valid_days == 0
        assert any(a.alert_type == "no_observations" for a in report.alerts)
        assert report.should_quarantine is False

    def test_healthy_factor_no_alerts(self, db_session):
        """健康因子无告警。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 记录 10 个稳定的有效观测
        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=10, ic_value=0.05
        )
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id
        )
        assert report.health_status == HEALTHY
        assert len(report.alerts) == 0
        assert report.should_quarantine is False
        assert report.n_valid_days == 10

    def test_ic_decay_detected(self, db_session):
        """IC 衰减被检测到。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 历史 10 天高 IC（带小变化避免常数化）
        base = date.today() - timedelta(days=15)
        for i in range(10):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.10 + 0.002 * (i % 5 - 2),  # 高 IC，带变化
                coverage=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        # 最近 5 天低 IC（衰减到 0.02，仅为历史的 20%，带小变化）
        base2 = base + timedelta(days=10)
        for i in range(5):
            td = base2 + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.02 + 0.001 * (i % 3 - 1),  # 衰减，带变化
                coverage=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id, window_days=5
        )
        assert report.health_status == DEGRADED
        assert any(a.alert_type == "ic_decay" for a in report.alerts)

    def test_coverage_drop_detected(self, db_session):
        """覆盖突降被检测到。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 历史 10 天高覆盖（IC 带变化避免常数化）
        base = date.today() - timedelta(days=15)
        for i in range(10):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05 + 0.002 * (i % 5 - 2),  # 带变化
                coverage=0.95,  # 高覆盖
            )
            record_shadow_observation(db_session, inp=inp)
        # 最近 5 天覆盖突降（IC 带变化）
        base2 = base + timedelta(days=10)
        for i in range(5):
            td = base2 + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05 + 0.002 * (i % 5 - 2),  # 带变化
                coverage=0.50,  # 突降
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id, window_days=5
        )
        assert report.health_status == DEGRADED
        assert any(a.alert_type == "coverage_drop" for a in report.alerts)

    def test_constant_factor_quarantined(self, db_session):
        """常数化因子触发 critical 并建议隔离。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 近 5 天 IC 完全相同（标准差为 0）
        base = date.today() - timedelta(days=5)
        for i in range(5):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05,  # 完全相同的 IC
                coverage=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id, window_days=5
        )
        assert report.should_quarantine is True
        assert any(a.alert_type == "constant_factor" for a in report.alerts)
        # 常数化是 critical
        assert report.health_status in (BLOCKED, DEGRADED)

    def test_missing_anomaly_detected(self, db_session):
        """缺失异常（近 5 日有 >= 3 个无效日）触发 critical。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 历史 5 天有效
        base = date.today() - timedelta(days=10)
        for i in range(5):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05,
            )
            record_shadow_observation(db_session, inp=inp)
        # 最近 5 天 3 个无效
        base2 = base + timedelta(days=5)
        for i in range(5):
            td = base2 + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.50 if i < 3 else 0.95,  # 前 3 个无效
                ic_value=0.05,
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id, window_days=5
        )
        assert report.should_quarantine is True
        assert any(a.alert_type == "missing_anomaly" for a in report.alerts)

    def test_to_dict_serializable(self, db_session):
        """to_dict 可序列化。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=5
        )
        db_session.commit()

        report = run_shadow_health_check(
            db_session, factor_version_id=version.id
        )
        d = report.to_dict()
        json.dumps(d, default=str)


# ══════════════════════════════════════════════════════════
# WP6-05: 人工审批流程
# ══════════════════════════════════════════════════════════


class TestActivationApproval:
    """人工审批流程测试。"""

    def test_request_activation_not_in_shadow(self, db_session):
        """非 shadow 状态申请激活失败。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="testing"
        )
        db_session.commit()

        req = ActivationRequest(
            factor_id=factor.id,
            factor_version_id=version.id,
            evidence_run_id=None,
            observation_start="",
            observation_end="",
            valid_days=0,
            actor="user_a",
            reason="test",
        )
        result = request_activation(db_session, req=req)
        assert result.success is False
        assert "not_in_shadow" in (result.error or "")

    def test_request_activation_insufficient_days(self, db_session):
        """有效天数不足 20 申请失败。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=10
        )
        db_session.commit()

        req = ActivationRequest(
            factor_id=factor.id,
            factor_version_id=version.id,
            evidence_run_id="eval-1-abc-20260801",
            observation_start="",
            observation_end="",
            valid_days=10,
            actor="user_a",
            reason="test",
        )
        result = request_activation(db_session, req=req)
        assert result.success is False
        assert "insufficient_valid_days" in (result.error or "")
        assert result.valid_days == 10

    def test_request_activation_factor_not_found(self, db_session):
        """因子不存在申请失败。"""
        req = ActivationRequest(
            factor_id=99999,
            factor_version_id=99999,
            evidence_run_id=None,
            observation_start="",
            observation_end="",
            valid_days=0,
            actor="user_a",
            reason="test",
        )
        result = request_activation(db_session, req=req)
        assert result.success is False
        assert result.error == "factor_not_found"

    def test_request_activation_meets_requirements(self, db_session):
        """满足门禁的申请成功。"""
        factor, version = _make_factor_and_version(db_session)
        _ensure_evidence_run(db_session, version.id)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=25
        )
        db_session.commit()

        req = ActivationRequest(
            factor_id=factor.id,
            factor_version_id=version.id,
            evidence_run_id="eval-1-abc-20260801",
            observation_start="",
            observation_end="",
            valid_days=25,
            actor="user_a",
            reason="meets requirements",
        )
        result = request_activation(db_session, req=req)
        assert result.success is True
        assert result.from_status == "shadow"
        assert result.to_status == "active"
        assert result.valid_days == 25

    def test_approve_activation_executes_transition(self, db_session):
        """批准激活执行状态迁移 shadow → active。"""
        factor, version = _make_factor_and_version(db_session)
        _ensure_evidence_run(db_session, version.id)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=25
        )
        db_session.commit()

        result = approve_activation(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            approver="approver_a",
            reason="approved",
            evidence_run_id="eval-1-abc-20260801",
        )
        # execute_transition 内部会 commit
        assert result.success is True
        assert result.from_status == "shadow"
        assert result.to_status == "active"
        assert result.audit_id is not None

        # 验证状态已迁移
        db_session.expire_all()
        refreshed_factor = db_session.get(Factor, factor.id)
        assert refreshed_factor.lifecycle_status == "active"

        # 验证审计记录
        audits = db_session.query(TransitionAudit).filter_by(
            factor_id=factor.id
        ).all()
        assert any(a.to_status == "active" for a in audits)

    def test_approve_activation_blocked_by_insufficient_days(self, db_session):
        """天数不足时批准失败。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=10
        )
        db_session.commit()

        result = approve_activation(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            approver="approver_a",
            reason="approved",
        )
        assert result.success is False
        assert "insufficient_valid_days" in (result.error or "")

    def test_approve_activation_blocked_by_health(self, db_session):
        """健康检查失败时批准被阻止。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        # 25 个有效观测，但最近 5 天常数化
        base = date.today() - timedelta(days=30)
        for i in range(20):
            td = base + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05 + 0.001 * (i % 5),  # 历史 IC 有变化
                coverage=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        # 最近 5 天常数化
        base2 = base + timedelta(days=20)
        for i in range(5):
            td = base2 + timedelta(days=i)
            inp = ShadowObservationInput(
                factor_id=factor.id,
                factor_version_id=version.id,
                trade_date=td.isoformat(),
                completeness_ratio=0.95,
                ic_value=0.05,  # 完全相同
                coverage=0.95,
            )
            record_shadow_observation(db_session, inp=inp)
        db_session.commit()

        result = approve_activation(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            approver="approver_a",
            reason="approved",
        )
        assert result.success is False
        assert "health_check_failed" in (result.error or "")

    def test_reject_activation_from_shadow(self, db_session):
        """从 shadow 状态驳回（shadow → rejected）。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        result = reject_activation(
            db_session,
            factor_id=factor.id,
            reviewer="reviewer_a",
            reason="not good enough",
        )
        # execute_transition 内部 commit
        assert result.success is True
        assert result.to_status == "rejected"
        assert result.audit_id is not None

    def test_auto_quarantine_from_shadow(self, db_session):
        """数据硬错误自动隔离 shadow → quarantined。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        result = auto_quarantine(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            reason="data corruption detected",
        )
        # execute_transition 内部 commit
        assert result.success is True
        assert result.from_status == "shadow"
        assert result.to_status == "quarantined"
        assert result.actor == "system"
        assert result.audit_id is not None

    def test_auto_quarantine_from_active(self, db_session):
        """active 状态也可自动隔离。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="active"
        )
        db_session.commit()

        result = auto_quarantine(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            reason="active factor degraded",
        )
        assert result.success is True
        assert result.from_status == "active"
        assert result.to_status == "quarantined"

    def test_auto_quarantine_blocked_from_draft(self, db_session):
        """draft 状态不允许隔离。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="draft"
        )
        db_session.commit()

        result = auto_quarantine(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            reason="try quarantine draft",
        )
        assert result.success is False
        assert "transition_forbidden" in (result.error or "") or "invalid" in (result.error or "")

    def test_approve_activation_idempotent_with_request_id(self, db_session):
        """带 request_id 的批准幂等。"""
        factor, version = _make_factor_and_version(db_session)
        _ensure_evidence_run(db_session, version.id)
        db_session.commit()

        _record_n_valid_observations(
            db_session, factor.id, version.id, n_days=25
        )
        db_session.commit()

        request_id = "activation-req-001"
        result1 = approve_activation(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            approver="approver_a",
            reason="approved",
            evidence_run_id="eval-1-abc-20260801",
            request_id=request_id,
        )
        assert result1.success is True

        # 第二次因状态已变为 active，门禁检查会失败（not_in_shadow）
        # 这是预期行为：request_id 幂等主要在 execute_transition 层
        result2 = approve_activation(
            db_session,
            factor_id=factor.id,
            factor_version_id=version.id,
            approver="approver_a",
            reason="approved again",
            request_id=request_id,
        )
        # 状态已迁移，第二次应失败
        assert result2.success is False


# ══════════════════════════════════════════════════════════
# WP6 状态机扩展测试
# ══════════════════════════════════════════════════════════


class TestLifecycleStateMachineWP6:
    """WP6 生命周期状态机扩展测试。"""

    def test_testing_to_shadow_transition(self, db_session):
        """testing → shadow 迁移。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="testing"
        )
        db_session.commit()

        req = FactorTransitionRequest(
            action="start_shadow",
            actor="user_a",
            reason="start shadow observation",
        )
        result = execute_transition(db_session, factor_id=factor.id, request=req)
        assert result.success is True
        assert result.to_status == "shadow"

    def test_shadow_to_active_via_activate(self, db_session):
        """shadow → active 迁移。"""
        factor, version = _make_factor_and_version(db_session)
        db_session.commit()

        req = FactorTransitionRequest(
            action="activate",
            actor="user_a",
            reason="approved",
        )
        result = execute_transition(db_session, factor_id=factor.id, request=req)
        assert result.success is True
        assert result.to_status == "active"

    def test_active_to_quarantined(self, db_session):
        """active → quarantined 迁移。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="active"
        )
        db_session.commit()

        req = FactorTransitionRequest(
            action="quarantine",
            actor="system",
            reason="data error",
        )
        result = execute_transition(db_session, factor_id=factor.id, request=req)
        assert result.success is True
        assert result.to_status == "quarantined"

    def test_quarantined_to_shadow_recovery(self, db_session):
        """quarantined → shadow 恢复观察。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="quarantined"
        )
        db_session.commit()

        req = FactorTransitionRequest(
            action="recover_shadow",
            actor="user_a",
            reason="data issue resolved",
        )
        result = execute_transition(db_session, factor_id=factor.id, request=req)
        assert result.success is True
        assert result.to_status == "shadow"

    def test_start_shadow_blocked_from_draft(self, db_session):
        """draft 状态不能直接进入 shadow。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="draft"
        )
        db_session.commit()

        req = FactorTransitionRequest(
            action="start_shadow",
            actor="user_a",
            reason="try skip testing",
        )
        with pytest.raises(ValueError, match="transition_forbidden"):
            execute_transition(db_session, factor_id=factor.id, request=req)

    def test_activate_blocked_from_testing(self, db_session):
        """testing 状态不能直接激活（必须先经过 shadow）。"""
        factor, version = _make_factor_and_version(
            db_session, lifecycle_status="testing"
        )
        db_session.commit()

        req = FactorTransitionRequest(
            action="activate",
            actor="user_a",
            reason="try skip shadow",
        )
        with pytest.raises(ValueError, match="transition_forbidden"):
            execute_transition(db_session, factor_id=factor.id, request=req)


# ══════════════════════════════════════════════════════════
# 常量与门禁阈值测试
# ══════════════════════════════════════════════════════════


class TestWP6Constants:
    """WP6 常量与门禁阈值测试。"""

    def test_min_valid_shadow_days_is_20(self):
        """最小有效观察天数为 20。"""
        assert MIN_VALID_SHADOW_DAYS == 20

    def test_min_completeness_ratio_is_90_percent(self):
        """最小完整率为 90%。"""
        assert MIN_COMPLETENESS_RATIO == 0.90

    def test_high_correlation_threshold(self):
        """高相关阈值。"""
        assert HIGH_CORRELATION_THRESHOLD == 0.70

    def test_extreme_correlation_threshold(self):
        """极高相关阈值。"""
        assert EXTREME_CORRELATION_THRESHOLD == 0.95

    def test_default_cluster_threshold(self):
        """默认聚类阈值。"""
        assert DEFAULT_CLUSTER_THRESHOLD == 0.60

    def test_min_residual_icir(self):
        """残差 ICIR 最小门禁。"""
        assert MIN_RESIDUAL_ICIR == 0.15

    def test_min_residual_ic_tstat(self):
        """残差 IC t 统计量阈值。"""
        assert MIN_RESIDUAL_IC_TSTAT == 1.96

    def test_ic_decay_threshold(self):
        """IC 衰减阈值。"""
        assert IC_DECAY_THRESHOLD == 0.50

    def test_coverage_drop_threshold(self):
        """覆盖突降阈值。"""
        assert COVERAGE_DROP_THRESHOLD == 0.70

    def test_constant_factor_ic_std(self):
        """常数化检测阈值。"""
        assert CONSTANT_FACTOR_IC_STD == 1e-6

    def test_health_status_enums(self):
        """健康状态枚举值。"""
        assert HEALTHY == "healthy"
        assert DEGRADED == "degraded"
        assert BLOCKED == "blocked"
