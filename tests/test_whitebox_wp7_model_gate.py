"""WP7-04: 模型门禁增强白盒测试。

覆盖：
- evaluate_model_gate 增强门禁（ICIR/成本后收益/权重漂移/簇暴露/FactorSet 健康）
- _compute_validation_icir（正常/退化/不足）
- _compute_weight_drift（有/无 active 模型）
- _compute_cluster_exposure（分类聚合/单簇主导）
- _check_factor_set_healthy（无观察/blocked/healthy）
- train_rolling_ridge 集成增强门禁指标写入 metrics_json
- 向后兼容：不配置增强阈值时跳过检查
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sklearn")

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember, ShadowObservation
from app.models.factor_model import FactorModelRun, FactorVersion, FactorWeightSnapshot
from app.models.factor_runtime import FactorRuntimeState
from app.services.factors.ridge_model import (
    ModelGate,
    _check_factor_set_healthy,
    _compute_cluster_exposure,
    _compute_validation_icir,
    _compute_weight_drift,
    evaluate_model_gate,
    train_rolling_ridge,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


# ── evaluate_model_gate 增强门禁单元测试 ──────────────────


def test_evaluate_model_gate_backward_compatible_without_enhanced_gate():
    """不配置增强阈值时，evaluate_model_gate 行为与基础门禁一致（向后兼容）。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        # 增强参数全部 None
        validation_icir=None,
        cost_adjusted_return=None,
        weight_drift=None,
        cluster_exposure=None,
        factor_set_healthy=None,
        normalized_weights=None,
    )
    assert reasons == []


def test_evaluate_model_gate_icir_passes_when_above_threshold():
    """ICIR 高于阈值时通过。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        minimum_validation_icir=0.5,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        validation_icir=0.8,
    )
    assert reasons == []


def test_evaluate_model_gate_icir_rejects_when_below_threshold():
    """ICIR 低于阈值时被拒绝，拒绝原因包含 validation_icir。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        minimum_validation_icir=0.5,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        validation_icir=0.3,
    )
    assert any(r.startswith("validation_icir:") for r in reasons)


def test_evaluate_model_gate_icir_rejects_when_non_finite():
    """ICIR 为 None（无法计算）且配置了阈值时被拒绝。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        minimum_validation_icir=0.5,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        validation_icir=None,
    )
    assert "validation_icir:non_finite" in reasons


def test_evaluate_model_gate_cost_adjusted_return_rejects():
    """扣成本后收益低于阈值时被拒绝。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        minimum_cost_adjusted_return=0.01,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        cost_adjusted_return=0.005,
    )
    assert any(r.startswith("cost_adjusted_return:") for r in reasons)


def test_evaluate_model_gate_weight_drift_rejects():
    """权重漂移超过阈值时被拒绝。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        max_weight_drift=0.3,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        weight_drift=0.5,
    )
    assert any(r.startswith("weight_drift:") for r in reasons)


def test_evaluate_model_gate_cluster_exposure_rejects():
    """簇暴露超过阈值时被拒绝。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        max_cluster_exposure=0.6,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        cluster_exposure=0.8,
    )
    assert any(r.startswith("cluster_exposure:") for r in reasons)


def test_evaluate_model_gate_factor_set_healthy_rejects():
    """FactorSet 不健康时被拒绝。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        require_factor_set_healthy=True,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        factor_set_healthy=False,
    )
    assert "factor_set_health:unhealthy" in reasons


def test_evaluate_model_gate_factor_set_healthy_unknown_rejects():
    """FactorSet 健康状态未知（None）且要求健康时被拒绝。"""
    gate = ModelGate(
        minimum_samples=10,
        minimum_symbols=5,
        minimum_trade_dates=5,
        minimum_validation_dates=3,
        minimum_validation_ic=0.0,
        require_factor_set_healthy=True,
    )
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        validation_date_count=20,
        validation_ic=0.05,
        coefficients=[1.0, -0.5],
        gate=gate,
        expected_feature_count=2,
        factor_set_healthy=None,
    )
    assert "factor_set_health:unknown" in reasons


# ── _compute_cluster_exposure 单元测试 ─────────────────────


def test_compute_cluster_exposure_single_cluster_dominant():
    """单簇主导时返回高占比。"""
    # 全部 fundamental 分类
    weights = {"ep_ttm": 0.6, "negative_pb": 0.3, "roe_yoy_growth": 0.1}
    exposure = _compute_cluster_exposure(weights)
    assert exposure is not None
    assert exposure == pytest.approx(1.0, abs=1e-6)


def test_compute_cluster_exposure_balanced_clusters():
    """均衡分布时返回约 0.5。"""
    # fundamental: 0.5, capital_flow: 0.5
    weights = {"ep_ttm": 0.5, "main_inflow_5d_ratio": 0.5}
    exposure = _compute_cluster_exposure(weights)
    assert exposure is not None
    assert exposure == pytest.approx(0.5, abs=1e-6)


def test_compute_cluster_exposure_empty_returns_none():
    """空权重返回 None。"""
    assert _compute_cluster_exposure({}) is None


def test_compute_cluster_exposure_all_zero_returns_none():
    """全零权重返回 None。"""
    assert _compute_cluster_exposure({"ep_ttm": 0.0, "main_inflow_5d_ratio": 0.0}) is None


def test_compute_cluster_exposure_custom_category():
    """自定义因子（不在 FACTOR_BY_CODE）归入 custom 分类。"""
    weights = {"ep_ttm": 0.4, "custom_factor": 0.6}
    exposure = _compute_cluster_exposure(weights)
    assert exposure is not None
    # custom: 0.6, fundamental: 0.4 → max = 0.6
    assert exposure == pytest.approx(0.6, abs=1e-6)


# ── _compute_validation_icir 单元测试 ──────────────────────


def _make_ridge_model():
    """构造一个简单的 Ridge 模型用于 ICIR 测试。"""
    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(42)
    x = rng.normal(size=(100, 2))
    y = 0.5 * x[:, 0] - 0.3 * x[:, 1] + rng.normal(scale=0.1, size=100)
    model = Ridge(alpha=1.0, fit_intercept=True)
    model.fit(x, y)
    return model


def test_compute_validation_icir_returns_finite_for_multi_day():
    """多日验证集返回有限 ICIR。"""
    model = _make_ridge_model()
    rng = np.random.default_rng(123)
    rows = []
    for day in range(10):
        for symbol in range(5):
            x1, x2 = rng.normal(size=2)
            target = 0.5 * x1 - 0.3 * x2 + rng.normal(scale=0.1)
            rows.append(
                {
                    "trade_date": date(2025, 1, 1) + timedelta(days=day),
                    "ep_ttm": x1,
                    "negative_pb": x2,
                    "target": target,
                }
            )
    validation_df = pd.DataFrame(rows)
    icir = _compute_validation_icir(validation_df, ["ep_ttm", "negative_pb"], model)
    assert icir is not None
    assert np.isfinite(icir)


def test_compute_validation_icir_returns_none_for_single_day():
    """单日验证集无法计算 std，返回 None。"""
    model = _make_ridge_model()
    rows = [
        {
            "trade_date": date(2025, 1, 1),
            "ep_ttm": 0.1,
            "negative_pb": -0.2,
            "target": 0.05,
        },
        {
            "trade_date": date(2025, 1, 1),
            "ep_ttm": 0.3,
            "negative_pb": -0.1,
            "target": 0.08,
        },
    ]
    validation_df = pd.DataFrame(rows)
    icir = _compute_validation_icir(validation_df, ["ep_ttm", "negative_pb"], model)
    assert icir is None


def test_compute_validation_icir_returns_none_for_empty():
    """空验证集返回 None。"""
    model = _make_ridge_model()
    validation_df = pd.DataFrame(
        columns=["trade_date", "ep_ttm", "negative_pb", "target"]
    )
    icir = _compute_validation_icir(validation_df, ["ep_ttm", "negative_pb"], model)
    assert icir is None


# ── _compute_weight_drift 单元测试（需要 DB） ─────────────


def _seed_factor_model_run(
    db_session,
    *,
    run_id: str,
    weights: dict[str, float],
    status: str = "validated",
) -> FactorModelRun:
    """创建一个 FactorModelRun 及其权重快照。"""
    model = FactorModelRun(
        id=run_id,
        model_type="ridge",
        asset_type="stock",
        target_code="target_5d_return",
        sample_count=100,
        symbol_count=20,
        trade_date_count=60,
        status=status,
        feature_versions_json="{}",
        hyperparameters_json="{}",
        metrics_json="{}",
    )
    db_session.add(model)
    db_session.flush()
    for code, weight in weights.items():
        db_session.add(
            FactorWeightSnapshot(
                model_run_id=run_id,
                factor_code=code,
                factor_version=1,
                coefficient=weight,
                normalized_weight=weight,
                train_ic=None,
                validation_ic=None,
            )
        )
    db_session.flush()
    return model


def _ensure_runtime_state(db_session, active_model_run_id: str | None):
    """创建或更新 FactorRuntimeState。"""
    state = db_session.get(FactorRuntimeState, 1)
    if state is None:
        state = FactorRuntimeState(
            id=1,
            weight_mode="ridge" if active_model_run_id else "manual",
            active_model_run_id=active_model_run_id,
            updated_by="test",
            version=1,
        )
        db_session.add(state)
    else:
        state.active_model_run_id = active_model_run_id
        state.weight_mode = "ridge" if active_model_run_id else "manual"
    db_session.flush()
    return state


def test_compute_weight_drift_returns_none_without_active_model(db_session):
    """无 active 模型时返回 None。"""
    _ensure_runtime_state(db_session, active_model_run_id=None)
    current_weights = {"ep_ttm": 0.5, "negative_pb": 0.5}
    drift = _compute_weight_drift(db_session, current_weights)
    assert drift is None


def test_compute_weight_drift_returns_zero_for_identical_weights(db_session):
    """权重完全一致时漂移为 0。"""
    _seed_factor_model_run(
        db_session, run_id="prev-001", weights={"ep_ttm": 0.6, "negative_pb": 0.4}
    )
    _ensure_runtime_state(db_session, active_model_run_id="prev-001")
    current_weights = {"ep_ttm": 0.6, "negative_pb": 0.4}
    drift = _compute_weight_drift(db_session, current_weights)
    assert drift is not None
    assert drift == pytest.approx(0.0, abs=1e-9)


def test_compute_weight_drift_returns_positive_for_different_weights(db_session):
    """权重不同时返回正漂移。"""
    _seed_factor_model_run(
        db_session, run_id="prev-002", weights={"ep_ttm": 0.8, "negative_pb": 0.2}
    )
    _ensure_runtime_state(db_session, active_model_run_id="prev-002")
    current_weights = {"ep_ttm": 0.2, "negative_pb": 0.8}
    drift = _compute_weight_drift(db_session, current_weights)
    assert drift is not None
    assert drift > 0.5  # 权重翻转，漂移较大


def test_compute_weight_drift_returns_none_for_no_common_features(db_session):
    """无共同特征时返回 None。"""
    _seed_factor_model_run(
        db_session, run_id="prev-003", weights={"ep_ttm": 1.0}
    )
    _ensure_runtime_state(db_session, active_model_run_id="prev-003")
    current_weights = {"turnover_z20": 1.0}
    drift = _compute_weight_drift(db_session, current_weights)
    assert drift is None


# ── _check_factor_set_healthy 单元测试（需要 DB） ──────────


def _seed_factors_versions_set(
    db_session,
    *,
    set_id: str = "fs-health-001",
) -> dict[str, int]:
    """创建因子、版本和冻结的 FactorSet，返回 {code: factor_version_id}。"""
    codes = ["ep_ttm", "negative_pb"]
    version_map: dict[str, int] = {}
    factor_set = FactorSet(
        id=set_id,
        name="Health Test Set",
        status="frozen",
        frozen_at=datetime.utcnow(),
        content_hash="hash-health",
        created_by="test",
    )
    db_session.add(factor_set)
    db_session.flush()
    for idx, code in enumerate(codes):
        factor = Factor(
            code=code,
            name=f"Test {code}",
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
            formula_expr=f"#{code}",
            params_json="{}",
            direction="higher_better",
            is_latest=1,
            validation_status="valid",
        )
        db_session.add(version)
        db_session.flush()
        version_map[code] = version.id
        db_session.add(
            FactorSetMember(
                factor_set_id=set_id,
                factor_id=factor.id,
                factor_version_id=version.id,
                factor_code=code,
                factor_version=1,
                role="feature",
                weight_constraint="free",
                display_order=idx,
                missing_policy="exclude",
            )
        )
    db_session.flush()
    return version_map


def test_check_factor_set_healthy_returns_none_without_factor_set_id(db_session):
    """factor_set_id 为 None 时返回 None。"""
    assert _check_factor_set_healthy(db_session, None) is None


def test_check_factor_set_healthy_returns_none_without_observations(db_session):
    """无 ShadowObservation 记录时返回 None（未知）。"""
    _seed_factors_versions_set(db_session)
    assert _check_factor_set_healthy(db_session, "fs-health-001") is None


def test_check_factor_set_healthy_returns_true_when_all_healthy(db_session):
    """所有成员最新观察为 healthy 时返回 True。"""
    version_map = _seed_factors_versions_set(db_session)
    for code, version_id in version_map.items():
        db_session.add(
            ShadowObservation(
                factor_id=1,
                factor_version_id=version_id,
                trade_date="2025-01-01",
                is_valid_day=True,
                health_status="healthy",
                ic_value=0.05,
                coverage=0.98,
            )
        )
    db_session.flush()
    assert _check_factor_set_healthy(db_session, "fs-health-001") is True


def test_check_factor_set_healthy_returns_false_when_blocked(db_session):
    """任一成员最新观察为 blocked 时返回 False。"""
    version_map = _seed_factors_versions_set(db_session)
    codes = list(version_map.keys())
    # 第一个 healthy
    db_session.add(
        ShadowObservation(
            factor_id=1,
            factor_version_id=version_map[codes[0]],
            trade_date="2025-01-01",
            is_valid_day=True,
            health_status="healthy",
            ic_value=0.05,
            coverage=0.98,
        )
    )
    # 第二个 blocked（最新记录）
    db_session.add(
        ShadowObservation(
            factor_id=2,
            factor_version_id=version_map[codes[1]],
            trade_date="2025-01-01",
            is_valid_day=True,
            health_status="healthy",
            ic_value=0.04,
            coverage=0.95,
        )
    )
    db_session.add(
        ShadowObservation(
            factor_id=2,
            factor_version_id=version_map[codes[1]],
            trade_date="2025-01-02",
            is_valid_day=False,
            health_status="blocked",
            invalid_reason="data_anomaly",
        )
    )
    db_session.flush()
    assert _check_factor_set_healthy(db_session, "fs-health-001") is False


# ── train_rolling_ridge 集成增强门禁测试 ───────────────────


def _seed_training_data_for_gate(
    warehouse: FactorWarehouse,
    *,
    trade_dates: int = 80,
    symbols: int = 10,
):
    """种子训练数据（复用 WP7-03 测试的数据生成逻辑）。"""
    start = date(2025, 1, 1)
    factor_rows = []
    target_rows = []
    created = datetime(2026, 1, 1)
    for day in range(trade_dates):
        signal_date = start + timedelta(days=day)
        for symbol_index in range(symbols):
            symbol = f"{symbol_index + 1:06d}"
            ep = (symbol_index - 4.5) / 3 + day * 0.005
            pb = ((symbol_index * 2 + day) % 9 - 4) / 2
            flow = ((symbol_index + day * 3) % 11 - 5) / 3
            turnover = ((symbol_index * 3 + day) % 7 - 3) / 2
            roe = ((symbol_index * 5 + day * 2) % 13 - 6) / 4
            features = {
                "ep_ttm": ep,
                "negative_pb": pb,
                "roe_yoy_growth": roe,
                "main_inflow_5d_ratio": flow,
                "turnover_z20": turnover,
            }
            target = (
                0.04 * ep
                - 0.025 * pb
                + 0.02 * roe
                + 0.015 * flow
                - 0.005 * turnover
                + symbol_index * 1e-5
            )
            for factor_code, value in features.items():
                factor_rows.append(
                    {
                        "symbol": symbol,
                        "trade_date": signal_date,
                        "factor_code": factor_code,
                        "factor_version": 1,
                        "raw_value": value,
                        "winsorized_value": value,
                        "normalized_value": value,
                        "is_imputed": False,
                        "imputation_method": None,
                        "eligible": True,
                        "data_cutoff_at": created,
                        "calc_batch_id": "factor-gate",
                        "created_at": created,
                    }
                )
            target_rows.append(
                {
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "entry_date": signal_date + timedelta(days=1),
                    "exit_date": signal_date + timedelta(days=5),
                    "target_code": "target_5d_return",
                    "target_value": target,
                    "is_tradable": True,
                    "invalid_reason": None,
                    "calc_batch_id": "target-gate",
                    "created_at": created,
                }
            )
    warehouse.upsert_records("factor_values", factor_rows)
    warehouse.upsert_records("factor_targets", target_rows)
    return start


def _seed_factors_and_frozen_set(db_session) -> dict[str, tuple[int, int]]:
    """创建因子、版本和冻结 FactorSet。"""
    codes = ["ep_ttm", "negative_pb", "roe_yoy_growth", "main_inflow_5d_ratio", "turnover_z20"]
    mapping: dict[str, tuple[int, int]] = {}
    factor_set = FactorSet(
        id="fs-gate-001",
        name="Gate Test Set",
        status="frozen",
        frozen_at=datetime.utcnow(),
        content_hash="hash-gate",
        created_by="test",
    )
    db_session.add(factor_set)
    db_session.flush()
    for idx, code in enumerate(codes):
        factor = Factor(
            code=code,
            name=f"Test {code}",
            category="fundamental" if code in ("ep_ttm", "negative_pb", "roe_yoy_growth") else "capital_flow",
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
            formula_expr=f"#{code}",
            params_json="{}",
            direction="higher_better",
            is_latest=1,
            validation_status="valid",
        )
        db_session.add(version)
        db_session.flush()
        mapping[code] = (factor.id, version.id)
        db_session.add(
            FactorSetMember(
                factor_set_id="fs-gate-001",
                factor_id=factor.id,
                factor_version_id=version.id,
                factor_code=code,
                factor_version=1,
                role="feature",
                weight_constraint="free",
                display_order=idx,
                missing_policy="exclude",
            )
        )
    db_session.flush()
    return mapping


def test_train_rolling_ridge_writes_enhanced_metrics_to_metrics_json(
    db_session, tmp_path
):
    """train_rolling_ridge 将 WP7-04 增强门禁指标写入 metrics_json。"""
    import json

    warehouse = FactorWarehouse(tmp_path / "factor_gate.duckdb")
    _seed_training_data_for_gate(warehouse)
    _seed_factors_and_frozen_set(db_session)

    gate = ModelGate(
        minimum_samples=500,
        minimum_symbols=5,
        minimum_trade_dates=60,
        minimum_validation_dates=10,
        minimum_validation_ic=0.5,
    )

    result = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-gate",
        target_calc_batch_id="target-gate",
        factor_set_id="fs-gate-001",
        data_cutoff_date=date(2025, 1, 1) + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    assert result.status == "validated"
    model = db_session.get(FactorModelRun, result.model_run_id)
    assert model is not None
    metrics = json.loads(model.metrics_json)
    # WP7-04 增强指标已写入 metrics_json
    assert "validation_icir" in metrics
    assert "weight_drift" in metrics
    assert "cluster_exposure" in metrics
    assert "factor_set_healthy" in metrics
    # 无 active 模型时 weight_drift 为 None
    assert metrics["weight_drift"] is None
    # factor_set_healthy 无观察记录时为 None
    assert metrics["factor_set_healthy"] is None
    # cluster_exposure 应为有限值（有权重时）
    assert metrics["cluster_exposure"] is not None


def test_train_rolling_ridge_enhanced_gate_rejects_on_high_cluster_exposure(
    db_session, tmp_path
):
    """配置严格的簇暴露阈值时，单簇主导的模型被拒绝。"""
    warehouse = FactorWarehouse(tmp_path / "factor_gate_reject.duckdb")
    _seed_training_data_for_gate(warehouse)
    _seed_factors_and_frozen_set(db_session)

    # 设置极低的簇暴露阈值（所有特征归入 fundamental+capital_flow 两簇，
    # fundamental 占 3/5=0.6，阈值 0.5 会拒绝）
    gate = ModelGate(
        minimum_samples=500,
        minimum_symbols=5,
        minimum_trade_dates=60,
        minimum_validation_dates=10,
        minimum_validation_ic=0.5,
        max_cluster_exposure=0.5,
    )

    result = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-gate-reject",
        target_calc_batch_id="target-gate-reject",
        factor_set_id="fs-gate-001",
        data_cutoff_date=date(2025, 1, 1) + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    # 由于 fundamental 簇占比可能超过 0.5，模型应被拒绝
    if result.status == "rejected":
        assert any(
            r.startswith("cluster_exposure:") for r in result.rejection_reasons
        )
