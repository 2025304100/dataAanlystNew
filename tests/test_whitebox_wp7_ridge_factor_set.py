"""WP7-03: Ridge 样本接线白盒测试。

覆盖：
- _load_features_from_factor_set 动态特征加载
- train_rolling_ridge 使用 factor_set_id 动态特征
- train_rolling_ridge 不传 factor_set_id 向后兼容
- evaluate_model_gate 动态 expected_feature_count
- scoring_bridge 从 model.weights 派生 feature_codes
- FactorSet 非 frozen 状态拒绝训练
- FactorSet 无 feature 成员拒绝训练
- 模型 identity 含 factor_set_id 实现幂等
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sklearn")

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion
from app.services.factors.ridge_model import (
    FEATURE_CODES,
    ModelGate,
    _load_features_from_factor_set,
    evaluate_model_gate,
    train_rolling_ridge,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


# ── 测试数据种子 ──────────────────────────────────────────


def _seed_factors_and_versions(db_session) -> dict[str, tuple[int, int]]:
    """创建 Factor 和 FactorVersion 记录，返回 {code: (factor_id, version_id)}。"""
    codes = ["ep_ttm", "negative_pb", "roe_yoy_growth", "main_inflow_5d_ratio", "turnover_z20"]
    mapping: dict[str, tuple[int, int]] = {}
    for code in codes:
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
    db_session.flush()
    return mapping


def _create_frozen_factor_set(
    db_session,
    *,
    factor_mapping: dict[str, tuple[int, int]],
    set_id: str = "fs-test-ridge-001",
    name: str = "Test Ridge Feature Set",
) -> FactorSet:
    """创建并冻结 FactorSet，成员为 factor_mapping 中的全部因子。"""
    factor_set = FactorSet(
        id=set_id,
        name=name,
        description="Test factor set for WP7-03",
        status="draft",
        content_hash=None,
        frozen_at=None,
        created_by="test",
    )
    db_session.add(factor_set)
    db_session.flush()

    for idx, (code, (factor_id, version_id)) in enumerate(factor_mapping.items()):
        member = FactorSetMember(
            factor_set_id=set_id,
            factor_id=factor_id,
            factor_version_id=version_id,
            factor_code=code,
            factor_version=1,
            role="feature",
            weight_constraint="free",
            display_order=idx,
            missing_policy="exclude",
        )
        db_session.add(member)

    db_session.flush()

    # 冻结
    factor_set.status = "frozen"
    factor_set.frozen_at = datetime.utcnow()
    factor_set.content_hash = "test_hash_001"
    db_session.flush()
    return factor_set


def _seed_training_data(
    warehouse: FactorWarehouse,
    *,
    trade_dates: int = 80,
    symbols: int = 10,
):
    """在 DuckDB 中种子因子值和目标值数据。"""
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
                        "calc_batch_id": "factor-train",
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
                    "calc_batch_id": "target-train",
                    "created_at": created,
                }
            )
    warehouse.upsert_records("factor_values", factor_rows)
    warehouse.upsert_records("factor_targets", target_rows)
    return start


# ── _load_features_from_factor_set 测试 ────────────────────


def test_load_features_from_factor_set_returns_dynamic_features(db_session):
    """_load_features_from_factor_set 从 frozen FactorSet 读取特征列表。"""
    mapping = _seed_factors_and_versions(db_session)
    _create_frozen_factor_set(db_session, factor_mapping=mapping)

    feature_codes, feature_versions, missing_policies = (
        _load_features_from_factor_set(db_session, "fs-test-ridge-001")
    )

    assert feature_codes == (
        "ep_ttm",
        "negative_pb",
        "roe_yoy_growth",
        "main_inflow_5d_ratio",
        "turnover_z20",
    )
    assert all(v == 1 for v in feature_versions.values())
    assert all(p == "exclude" for p in missing_policies.values())


def test_load_features_rejects_non_frozen_factor_set(db_session):
    """非 frozen 状态的 FactorSet 拒绝加载特征。"""
    mapping = _seed_factors_and_versions(db_session)

    factor_set = FactorSet(
        id="fs-draft-001",
        name="Draft Set",
        status="draft",
        created_by="test",
    )
    db_session.add(factor_set)
    db_session.flush()

    # 添加一个成员
    code = "ep_ttm"
    factor_id, version_id = mapping[code]
    db_session.add(
        FactorSetMember(
            factor_set_id="fs-draft-001",
            factor_id=factor_id,
            factor_version_id=version_id,
            factor_code=code,
            factor_version=1,
            role="feature",
            display_order=0,
            missing_policy="exclude",
        )
    )
    db_session.flush()

    with pytest.raises(ValueError, match="factor_set_not_frozen"):
        _load_features_from_factor_set(db_session, "fs-draft-001")


def test_load_features_rejects_not_found(db_session):
    """不存在的 FactorSet 抛出 factor_set_not_found。"""
    with pytest.raises(ValueError, match="factor_set_not_found"):
        _load_features_from_factor_set(db_session, "fs-nonexistent-999")


def test_load_features_rejects_empty_feature_set(db_session):
    """只有 target/regime 成员的 FactorSet 拒绝加载（无 feature）。"""
    mapping = _seed_factors_and_versions(db_session)

    factor_set = FactorSet(
        id="fs-no-features-001",
        name="No Features Set",
        status="frozen",
        frozen_at=datetime.utcnow(),
        content_hash="test_hash_no_feat",
        created_by="test",
    )
    db_session.add(factor_set)
    db_session.flush()

    # 添加一个 target 角色成员（不是 feature）
    code = "ep_ttm"
    factor_id, version_id = mapping[code]
    db_session.add(
        FactorSetMember(
            factor_set_id="fs-no-features-001",
            factor_id=factor_id,
            factor_version_id=version_id,
            factor_code=code,
            factor_version=1,
            role="target",
            display_order=0,
            missing_policy="exclude",
        )
    )
    db_session.flush()

    with pytest.raises(ValueError, match="factor_set_no_features"):
        _load_features_from_factor_set(db_session, "fs-no-features-001")


def test_load_features_skips_non_feature_roles(db_session):
    """target/regime 角色成员被跳过，只返回 feature 角色。"""
    mapping = _seed_factors_and_versions(db_session)

    factor_set = FactorSet(
        id="fs-mixed-roles-001",
        name="Mixed Roles Set",
        status="frozen",
        frozen_at=datetime.utcnow(),
        content_hash="test_hash_mixed",
        created_by="test",
    )
    db_session.add(factor_set)
    db_session.flush()

    # ep_ttm 作为 feature
    fid, vid = mapping["ep_ttm"]
    db_session.add(FactorSetMember(
        factor_set_id="fs-mixed-roles-001", factor_id=fid, factor_version_id=vid,
        factor_code="ep_ttm", factor_version=1, role="feature", display_order=0,
        missing_policy="exclude",
    ))
    # negative_pb 作为 target
    fid, vid = mapping["negative_pb"]
    db_session.add(FactorSetMember(
        factor_set_id="fs-mixed-roles-001", factor_id=fid, factor_version_id=vid,
        factor_code="negative_pb", factor_version=1, role="target", display_order=1,
        missing_policy="exclude",
    ))
    db_session.flush()

    feature_codes, _, _ = _load_features_from_factor_set(
        db_session, "fs-mixed-roles-001"
    )
    assert feature_codes == ("ep_ttm",)


# ── train_rolling_ridge with factor_set_id 测试 ────────────


def test_train_rolling_ridge_with_factor_set_id_uses_dynamic_features(
    db_session, tmp_path
):
    """train_rolling_ridge 使用 factor_set_id 时从 FactorSet 读取动态特征。"""
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse)
    mapping = _seed_factors_and_versions(db_session)
    _create_frozen_factor_set(db_session, factor_mapping=mapping)

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
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        factor_set_id="fs-test-ridge-001",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    assert result.status == "validated"
    assert result.validation_ic is not None
    assert result.validation_ic > 0.9
    # 系数符号与训练数据生成逻辑一致
    assert result.coefficients["ep_ttm"] > 0
    assert result.coefficients["negative_pb"] < 0
    assert result.coefficients["roe_yoy_growth"] > 0
    assert result.coefficients["main_inflow_5d_ratio"] > 0
    assert result.coefficients["turnover_z20"] < 0
    assert sum(abs(v) for v in result.normalized_weights.values()) == pytest.approx(1)

    # 模型记录含 factor_set_id
    model = db_session.get(FactorModelRun, result.model_run_id)
    assert model is not None
    import json
    hyperparams = json.loads(model.hyperparameters_json)
    assert hyperparams["factor_set_id"] == "fs-test-ridge-001"


def test_train_rolling_ridge_with_factor_set_id_is_idempotent(
    db_session, tmp_path
):
    """同 factor_set_id + 同参数的模型训练幂等。"""
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse)
    mapping = _seed_factors_and_versions(db_session)
    _create_frozen_factor_set(db_session, factor_mapping=mapping)

    gate = ModelGate(
        minimum_samples=500,
        minimum_symbols=5,
        minimum_trade_dates=60,
        minimum_validation_dates=10,
        minimum_validation_ic=0.5,
    )

    first = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        factor_set_id="fs-test-ridge-001",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    second = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        factor_set_id="fs-test-ridge-001",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )

    assert second.reused is True
    assert second.model_run_id == first.model_run_id


def test_train_rolling_ridge_factor_set_id_produces_different_run_than_legacy(
    db_session, tmp_path
):
    """factor_set_id 路径和 legacy 路径产生不同的模型 run_id（identity 不同）。"""
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse)
    mapping = _seed_factors_and_versions(db_session)
    _create_frozen_factor_set(db_session, factor_mapping=mapping)

    gate = ModelGate(
        minimum_samples=500,
        minimum_symbols=5,
        minimum_trade_dates=60,
        minimum_validation_dates=10,
        minimum_validation_ic=0.5,
    )

    # 带 factor_set_id
    with_set = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        factor_set_id="fs-test-ridge-001",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    # 不带 factor_set_id（legacy 回退）
    without_set = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    # 不同的 run_id（identity 含 factor_set_id）
    assert with_set.model_run_id != without_set.model_run_id
    # 两者都验证通过
    assert with_set.status == "validated"
    assert without_set.status == "validated"


def test_train_rolling_ridge_with_non_frozen_factor_set_raises(db_session, tmp_path):
    """FactorSet 非 frozen 状态时 train_rolling_ridge 抛错。"""
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    _seed_training_data(warehouse, trade_dates=10, symbols=3)
    mapping = _seed_factors_and_versions(db_session)

    # 创建 draft 状态的 FactorSet
    db_session.add(FactorSet(
        id="fs-draft-train-001",
        name="Draft Train Set",
        status="draft",
        created_by="test",
    ))
    db_session.flush()

    with pytest.raises(ValueError, match="factor_set_not_frozen"):
        train_rolling_ridge(
            db_session,
            warehouse,
            factor_calc_batch_id="factor-train",
            target_calc_batch_id="target-train",
            factor_set_id="fs-draft-train-001",
        )


# ── 向后兼容测试 ──────────────────────────────────────────


def test_train_rolling_ridge_without_factor_set_id_uses_static_features(
    db_session, tmp_path
):
    """不传 factor_set_id 时回退到静态 FEATURE_CODES（legacy 路径）。"""
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse)

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
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        data_cutoff_date=start + timedelta(days=90),
        window_days=70,
        validation_days=15,
        gate=gate,
    )
    db_session.commit()

    assert result.status == "validated"
    # 系数字典的 key 集合与静态 FEATURE_CODES 一致
    assert set(result.coefficients.keys()) == set(FEATURE_CODES)

    # hyperparameters 中 factor_set_id 为 None
    model = db_session.get(FactorModelRun, result.model_run_id)
    import json
    hyperparams = json.loads(model.hyperparameters_json)
    assert hyperparams["factor_set_id"] is None


# ── evaluate_model_gate 动态特征数测试 ─────────────────────


def test_evaluate_model_gate_with_dynamic_feature_count():
    """evaluate_model_gate 使用 expected_feature_count 而非静态 FEATURE_CODES 长度。"""
    gate = ModelGate(
        minimum_samples=1,
        minimum_symbols=1,
        minimum_trade_dates=1,
        minimum_validation_dates=1,
        minimum_validation_ic=0,
    )

    # 3 个系数，expected_feature_count=3 → 通过
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=10,
        trade_date_count=30,
        validation_date_count=10,
        validation_ic=0.1,
        coefficients=[1.0, 2.0, 3.0],
        gate=gate,
        expected_feature_count=3,
    )
    assert "coefficients:wrong_size" not in reasons

    # 3 个系数，expected_feature_count=5 → wrong_size
    reasons_wrong = evaluate_model_gate(
        sample_count=100,
        symbol_count=10,
        trade_date_count=30,
        validation_date_count=10,
        validation_ic=0.1,
        coefficients=[1.0, 2.0, 3.0],
        gate=gate,
        expected_feature_count=5,
    )
    assert "coefficients:wrong_size" in reasons_wrong


def test_evaluate_model_gate_backward_compat_without_expected_count():
    """不传 expected_feature_count 时回退到静态 FEATURE_CODES 长度。"""
    gate = ModelGate(
        minimum_samples=1,
        minimum_symbols=1,
        minimum_trade_dates=1,
        minimum_validation_dates=1,
        minimum_validation_ic=0,
    )

    # 系数数量与 FEATURE_CODES 一致 → 通过
    reasons = evaluate_model_gate(
        sample_count=100,
        symbol_count=10,
        trade_date_count=30,
        validation_date_count=10,
        validation_ic=0.1,
        coefficients=[1.0] * len(FEATURE_CODES),
        gate=gate,
    )
    assert "coefficients:wrong_size" not in reasons

    # 系数数量不一致 → wrong_size
    reasons_wrong = evaluate_model_gate(
        sample_count=100,
        symbol_count=10,
        trade_date_count=30,
        validation_date_count=10,
        validation_ic=0.1,
        coefficients=[1.0, 2.0, 3.0],
        gate=gate,
    )
    assert "coefficients:wrong_size" in reasons_wrong


# ── scoring_bridge 动态特征测试 ────────────────────────────


def test_scoring_bridge_uses_dynamic_features_from_model_weights(
    db_session, tmp_path
):
    """scoring_bridge 从 model.weights 派生 feature_codes，不依赖静态 FEATURE_CODES。"""
    from app.models.score import Score
    from app.models.symbol import Symbol
    from app.services.factors.scoring_bridge import materialize_factor_scores

    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = _seed_training_data(warehouse, trade_dates=10, symbols=3)
    mapping = _seed_factors_and_versions(db_session)
    _create_frozen_factor_set(db_session, factor_mapping=mapping)

    # 训练模型（使用 factor_set_id）
    gate = ModelGate(
        minimum_samples=1,
        minimum_symbols=1,
        minimum_trade_dates=1,
        minimum_validation_dates=1,
        minimum_validation_ic=-1,
    )
    trade_date = start + timedelta(days=5)

    model_result = train_rolling_ridge(
        db_session,
        warehouse,
        factor_calc_batch_id="factor-train",
        target_calc_batch_id="target-train",
        factor_set_id="fs-test-ridge-001",
        data_cutoff_date=start + timedelta(days=9),
        window_days=8,
        validation_days=2,
        gate=gate,
        model_run_id="test-scoring-bridge-001",
    )
    db_session.commit()
    assert model_result.status == "validated"

    # 创建 Symbol 和 manual Score
    for idx in range(1, 4):
        symbol = Symbol(
            symbol=f"{idx:06d}",
            name=f"Test {idx}",
            asset_type="stock",
            market="CN",
            is_active=1,
        )
        db_session.add(symbol)
        db_session.flush()
        db_session.add(Score(
            symbol_id=symbol.id,
            trade_date=trade_date,
            quality_score=60,
            quality_grade="B",
            timing_score=40,
            stage="start",
            action="open",
            priority_score=float(idx * 10),
            scoring_asset_type="stock",
            scoring_config_snapshot_json='{"final_weights": {"quality": 0.4, "timing": 0.5, "news": 0.1}}',
            dimension_scores_json='{"manual": 60}',
            factor_scores_json='{}',
            weight_mode="manual",
            calc_batch_id="manual-001",
        ))
    db_session.flush()

    # 在 DuckDB 中为 trade_date 添加因子值
    factor_rows = []
    for day in range(5, 6):  # 只为 trade_date 那天添加
        signal_date = start + timedelta(days=day)
        for symbol_index in range(3):
            symbol = f"{symbol_index + 1:06d}"
            for code in FEATURE_CODES:
                value = float((symbol_index + day) % 7 - 3) / 2
                factor_rows.append({
                    "symbol": symbol,
                    "trade_date": signal_date,
                    "factor_code": code,
                    "factor_version": 1,
                    "raw_value": value,
                    "winsorized_value": value,
                    "normalized_value": value,
                    "is_imputed": False,
                    "imputation_method": None,
                    "eligible": True,
                    "data_cutoff_at": datetime(2026, 1, 1),
                    "calc_batch_id": "factor-score",
                    "created_at": datetime(2026, 1, 1),
                })
    warehouse.upsert_records("factor_values", factor_rows)

    # 激活模型
    model = db_session.get(FactorModelRun, "test-scoring-bridge-001")
    model.status = "validated"
    db_session.flush()

    # 调用 materialize_factor_scores（shadow 模式）
    result = materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=trade_date,
        factor_calc_batch_id="factor-score",
        model_run_id="test-scoring-bridge-001",
        mode="shadow",
    )
    db_session.commit()

    assert result.mode == "shadow"
    assert result.materialized_count > 0

    # 验证 Score 记录中包含动态特征解释
    scores = db_session.query(Score).filter(
        Score.calc_batch_id == result.calc_batch_id
    ).all()
    assert len(scores) > 0
    import json
    detail = json.loads(scores[0].factor_scores_json)
    dynamic = detail.get("_dynamic_model", {})
    # factors 字典的 key 集合应该来自 model.weights（等于 FEATURE_CODES）
    assert set(dynamic.get("factors", {}).keys()) == set(FEATURE_CODES)
