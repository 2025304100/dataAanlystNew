"""WP0-01/02/03: 基线冻结白盒测试。

覆盖：
- 8 系统因子基线快照（content_hash 确定性、DB 一致性检测）
- 24 个 API 端点契约（字段验证、缺失字段检测）
- 计算结果对账样本（空 DB 和有数据场景）

对齐 docs/专业因子库开发计划.md §WP0 基线冻结与开发护栏。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models.factor import Factor, FactorValue
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.baseline_freeze import (
    API_CONTRACTS,
    ApiContractSnapshot,
    BaselineFreezeReport,
    FactorBaselineSnapshot,
    ReconciliationSample,
    build_reconciliation_sample,
    compute_factor_content_hash,
    freeze_api_contract,
    freeze_factor_baseline,
    generate_baseline_freeze_report,
    validate_api_response_fields,
)
from app.services.factors.definitions import seed_factor_definitions

pytestmark = pytest.mark.whitebox


EXPECTED_FACTOR_CODES = {
    "ep_ttm",
    "negative_pb",
    "roe_yoy_growth",
    "main_inflow_5d_ratio",
    "lhb_institution_net_ratio",
    "turnover_z20",
    "hot_rank_attention",
    "tail_accumulation_proxy",
}


# ── 1. compute_factor_content_hash ───────────────────────


def test_content_hash_is_deterministic():
    """相同输入应产生相同的 content_hash（SHA256[:16]）。"""
    kwargs = {
        "formula": "1 / pe_ttm, pe_ttm > 0",
        "params": {"valuation_max_age_days": 7, "positive_pe_only": True},
        "source_mapping": {"table": "raw_valuation_snapshots", "field": "pe_ttm"},
        "direction": "higher_better",
    }
    h1 = compute_factor_content_hash(**kwargs)
    h2 = compute_factor_content_hash(**kwargs)
    assert h1 == h2
    assert len(h1) == 16


def test_content_hash_differs_on_formula_change():
    """公式不同 → content_hash 不同。"""
    base = {
        "formula": "1 / pe_ttm, pe_ttm > 0",
        "params": {"valuation_max_age_days": 7},
        "source_mapping": {"table": "raw_valuation_snapshots"},
        "direction": "higher_better",
    }
    h1 = compute_factor_content_hash(**base)
    h2 = compute_factor_content_hash(
        formula="1 / pe_ttm",
        params=base["params"],
        source_mapping=base["source_mapping"],
        direction=base["direction"],
    )
    assert h1 != h2


def test_content_hash_differs_on_direction_change():
    """方向不同 → content_hash 不同。"""
    base = {
        "formula": "(turnover - mean_20d) / stddev_pop_20d",
        "params": {"window": 20, "minimum_periods": 20},
        "source_mapping": {"table": "raw_daily_bars", "field": "turnover_rate"},
        "direction": "nonlinear",
    }
    h1 = compute_factor_content_hash(**base)
    h2 = compute_factor_content_hash(
        formula=base["formula"],
        params=base["params"],
        source_mapping=base["source_mapping"],
        direction="higher_better",
    )
    assert h1 != h2


# ── 2. freeze_factor_baseline ────────────────────────────


def test_freeze_factor_baseline_has_8_factors(db_session):
    """seed_factor_definitions 后冻结基线 → 8 个因子且 code 正确。"""
    seed_factor_definitions(db_session)
    db_session.commit()

    snapshot = freeze_factor_baseline(db_session)
    assert isinstance(snapshot, FactorBaselineSnapshot)
    assert snapshot.factor_count == 8
    assert len(snapshot.factors) == 8
    assert {f.code for f in snapshot.factors} == EXPECTED_FACTOR_CODES


def test_freeze_factor_baseline_db_consistent_after_seed(db_session):
    """seed 后 db_consistent=True, inconsistencies=[]。"""
    seed_factor_definitions(db_session)
    db_session.commit()

    snapshot = freeze_factor_baseline(db_session)
    assert snapshot.db_consistent is True
    assert snapshot.inconsistencies == []


def test_freeze_factor_baseline_detects_missing_factor(db_session):
    """空 DB（未 seed）→ db_consistent=False, 8 个 missing_factor_in_db。"""
    snapshot = freeze_factor_baseline(db_session)
    assert snapshot.db_consistent is False
    missing_msgs = [
        s for s in snapshot.inconsistencies if s.startswith("missing_factor_in_db:")
    ]
    assert len(missing_msgs) == 8
    for code in EXPECTED_FACTOR_CODES:
        assert f"missing_factor_in_db:{code}" in snapshot.inconsistencies


def test_freeze_factor_baseline_has_content_hash(db_session):
    """每个 factor entry 有非空 16 字符 content_hash。"""
    seed_factor_definitions(db_session)
    db_session.commit()

    snapshot = freeze_factor_baseline(db_session)
    for entry in snapshot.factors:
        assert entry.content_hash
        assert len(entry.content_hash) == 16


# ── 3. API contracts ─────────────────────────────────────


def test_api_contracts_has_24_endpoints():
    """API_CONTRACTS 共 24 个端点。"""
    assert len(API_CONTRACTS) == 24


def test_freeze_api_contract_returns_snapshot():
    """freeze_api_contract() 返回 ApiContractSnapshot, 24 端点, 非空 manual_mode_fields。"""
    snapshot = freeze_api_contract()
    assert isinstance(snapshot, ApiContractSnapshot)
    assert snapshot.endpoint_count == 24
    assert len(snapshot.contracts) == 24
    assert snapshot.manual_mode_fields
    # 每个端点都应在 manual_mode_fields 中有对应字段快照
    for contract in snapshot.contracts:
        key = f"{contract.method} {contract.path}"
        assert key in snapshot.manual_mode_fields


def test_validate_api_response_fields_pass():
    """完整字段响应 → 通过（无违规）。"""
    violations = validate_api_response_fields(
        "/factors/overview",
        {"runtime": {}, "config": {}, "feature_enabled": True, "health": {}},
    )
    assert violations == []


def test_validate_api_response_fields_detects_missing():
    """缺少 feature_enabled 和 health → 返回对应违规字段。"""
    violations = validate_api_response_fields(
        "/factors/overview",
        {"runtime": {}, "config": {}},
    )
    assert isinstance(violations, list)
    assert len(violations) == 2
    joined = " ".join(violations)
    assert "feature_enabled" in joined
    assert "health" in joined


# ── 4. build_reconciliation_sample ───────────────────────


def test_build_reconciliation_sample_empty_db(db_session):
    """空 DB → factor_value_count=0, score_count=0, sample_hash=""。"""
    sample = build_reconciliation_sample(db_session)
    assert isinstance(sample, ReconciliationSample)
    assert sample.factor_value_count == 0
    assert sample.score_count == 0
    assert sample.sample_hash == ""
    assert sample.factor_values == []
    assert sample.scores == []


def test_build_reconciliation_sample_with_data(db_session):
    """seed 因子 + 标的 + FactorValue + Score → 样本正确捕获。"""
    # 1. Seed factors（Factor + FactorVersion）
    seed_factor_definitions(db_session)
    db_session.flush()

    factors = db_session.execute(select(Factor)).scalars().all()
    factor_by_code = {f.code: f for f in factors}

    # 2. 添加 2 个标的
    sym1 = Symbol(
        symbol="600000", name="Test1", asset_type="stock", market="sh", is_active=1
    )
    sym2 = Symbol(
        symbol="600001", name="Test2", asset_type="stock", market="sh", is_active=1
    )
    db_session.add_all([sym1, sym2])
    db_session.flush()

    # 3. 添加 FactorValue（trade_date=RECONCILIATION_BASE_DATE=2026-07-24）
    target_date = date(2026, 7, 24)
    db_session.add_all(
        [
            FactorValue(
                symbol_id=sym1.id,
                factor_id=factor_by_code["ep_ttm"].id,
                trade_date=target_date,
                raw_value=0.05,
                normalized_value=1.2,
                calc_batch_id="baseline-test",
            ),
            FactorValue(
                symbol_id=sym1.id,
                factor_id=factor_by_code["negative_pb"].id,
                trade_date=target_date,
                raw_value=-3.0,
                normalized_value=0.8,
                calc_batch_id="baseline-test",
            ),
            FactorValue(
                symbol_id=sym2.id,
                factor_id=factor_by_code["ep_ttm"].id,
                trade_date=target_date,
                raw_value=0.07,
                normalized_value=1.5,
                calc_batch_id="baseline-test",
            ),
        ]
    )

    # 4. 添加 Score
    db_session.add_all(
        [
            Score(
                symbol_id=sym1.id,
                trade_date=target_date,
                quality_score=70,
                quality_grade="B",
                timing_score=65,
                stage="start",
                action="open",
                priority_score=68,
                weight_mode="manual",
                calc_batch_id="baseline-test",
            ),
            Score(
                symbol_id=sym2.id,
                trade_date=target_date,
                quality_score=80,
                quality_grade="A",
                timing_score=75,
                stage="start",
                action="open",
                priority_score=78,
                weight_mode="manual",
                calc_batch_id="baseline-test",
            ),
        ]
    )
    db_session.commit()

    sample = build_reconciliation_sample(db_session)
    assert sample.factor_value_count == 3
    assert sample.score_count == 2
    assert sample.symbol_count == 2
    assert sample.sample_hash  # 非空
    # trade_date 应为基线日期
    assert sample.trade_date == "2026-07-24"
    # factor_codes 正确
    fv_codes = {fv.factor_code for fv in sample.factor_values}
    assert "ep_ttm" in fv_codes
    assert "negative_pb" in fv_codes
    # symbols 正确
    fv_symbols = {fv.symbol for fv in sample.factor_values}
    assert "600000" in fv_symbols
    assert "600001" in fv_symbols
    # scores 的 symbols 正确
    score_symbols = {s.symbol for s in sample.scores}
    assert score_symbols == {"600000", "600001"}


# ── 5. generate_baseline_freeze_report ───────────────────


def test_generate_baseline_freeze_report_structure(db_session):
    """seed 因子后生成报告 → 含 factor_baseline, api_contract,
    reconciliation_sample (may be None), runtime_state, repo_head_revision。"""
    seed_factor_definitions(db_session)
    db_session.commit()

    report = generate_baseline_freeze_report(db_session)
    assert isinstance(report, BaselineFreezeReport)
    assert report.factor_baseline is not None
    assert report.api_contract is not None
    assert report.api_contract.endpoint_count == 24
    # reconciliation_sample 可能为 None（无对账数据时）
    assert report.reconciliation_sample is None or isinstance(
        report.reconciliation_sample, ReconciliationSample
    )
    assert isinstance(report.runtime_state, dict)
    # repo_head_revision 可能为 None（无 alembic versions 时），但属性必须存在
    assert hasattr(report, "repo_head_revision")


def test_generate_baseline_freeze_report_factor_baseline_has_8(db_session):
    """seed 后报告的 factor_baseline.factor_count == 8。"""
    seed_factor_definitions(db_session)
    db_session.commit()

    report = generate_baseline_freeze_report(db_session)
    assert report.factor_baseline.factor_count == 8
