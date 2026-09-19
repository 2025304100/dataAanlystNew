"""WP7-05: Score 解释追溯白盒测试。

覆盖：
- materialize_factor_scores 生成的 Score 包含 factor_set_id（从模型 hyperparameters_json 读取）
- Score 的 factor_member_versions_json 包含正确的成员版本快照
- manual 模式下 factor_set_id 为 None（向后兼容）
- factor_scores_json 中包含 factor_set_id 追溯信息
- legacy 模型（无 factor_set_id）时 Score 的 factor_set_id 为 None
"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import select

pytest.importorskip('duckdb')

from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.ridge_model import FEATURE_CODES
from app.services.factors.scoring_bridge import materialize_factor_scores
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


_TRADE_DATE = date(2026, 1, 5)
_CUTOFF = datetime(2026, 1, 5, 16, 0)
_COEFFICIENTS = {
    'ep_ttm': 0.3,
    'negative_pb': 0.15,
    'roe_yoy_growth': 0.15,
    'main_inflow_5d_ratio': 0.3,
    'turnover_z20': -0.1,
}


def _seed_traceability_inputs(
    db_session,
    warehouse: FactorWarehouse,
    *,
    factor_set_id: str | None = 'fs-trace-001',
    model_run_id: str = 'ridge-trace-test',
):
    """种子测试数据，可选指定 factor_set_id 写入模型 hyperparameters_json。"""
    symbols = []
    manual_scores = []
    values_by_symbol = {
        '000001': (-1.0, -1.0, -1.0, -1.0, 1.0),
        '000002': (0.0, 0.0, 0.0, 0.0, 0.0),
        '000003': (1.0, 1.0, 1.0, 1.0, -1.0),
    }
    for index, symbol_code in enumerate(values_by_symbol, start=1):
        symbol = Symbol(
            symbol=symbol_code,
            name=f'Test {index}',
            asset_type='stock',
            market='CN',
            is_active=1,
        )
        db_session.add(symbol)
        db_session.flush()
        symbols.append(symbol)
        manual_scores.append(
            Score(
                symbol_id=symbol.id,
                trade_date=_TRADE_DATE,
                quality_score=60,
                quality_grade='B',
                timing_score=40,
                stage='start',
                action='open',
                priority_score=float(index * 10),
                scoring_asset_type='stock',
                scoring_config_snapshot_json='{}',
                dimension_scores_json='{}',
                factor_scores_json='{}',
                weight_mode='manual',
                calc_batch_id='manual-trace-test',
            )
        )
    db_session.add_all(manual_scores)

    # hyperparameters_json 包含 factor_set_id（WP7-03 写入）
    hyperparams = {'selected_alpha': 1.0}
    if factor_set_id is not None:
        hyperparams['factor_set_id'] = factor_set_id

    model = FactorModelRun(
        id=model_run_id,
        model_type='ridge',
        asset_type='stock',
        target_code='target_5d_return',
        status='validated',
        data_cutoff_at=_CUTOFF,
        sample_count=1000,
        symbol_count=100,
        trade_date_count=250,
        hyperparameters_json=json.dumps(hyperparams, sort_keys=True),
        feature_versions_json=json.dumps(
            {code: 1 for code in FEATURE_CODES}, sort_keys=True
        ),
    )
    for code in FEATURE_CODES:
        coefficient = _COEFFICIENTS[code]
        model.weights.append(
            FactorWeightSnapshot(
                factor_code=code,
                factor_version=1,
                coefficient=coefficient,
                normalized_weight=coefficient,
            )
        )
    db_session.add(model)
    db_session.flush()

    factor_rows = []
    for symbol_code, values in values_by_symbol.items():
        for factor_code, value in zip(FEATURE_CODES, values):
            factor_rows.append(
                {
                    'symbol': symbol_code,
                    'trade_date': _TRADE_DATE,
                    'factor_code': factor_code,
                    'factor_version': 1,
                    'raw_value': value,
                    'winsorized_value': value,
                    'normalized_value': value,
                    'is_imputed': False,
                    'imputation_method': None,
                    'eligible': True,
                    'data_cutoff_at': _CUTOFF,
                    'calc_batch_id': 'factor-trace-test',
                    'created_at': _CUTOFF,
                }
            )
    warehouse.upsert_records('factor_values', factor_rows)
    return symbols, manual_scores, model


def _dynamic_scores(db_session, mode: str) -> list[Score]:
    return db_session.execute(
        select(Score)
        .where(Score.weight_mode == mode)
        .order_by(Score.symbol_id)
    ).scalars().all()


# ── WP7-05: Score 追溯字段测试 ─────────────────────────────


def test_shadow_score_contains_factor_set_id(db_session, tmp_path):
    """shadow 模式 Score 记录包含 factor_set_id（从模型 hyperparameters_json 读取）。"""
    warehouse = FactorWarehouse(tmp_path / 'factor_trace.duckdb')
    _seed_traceability_inputs(
        db_session, warehouse, factor_set_id='fs-trace-001'
    )

    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-trace-test',
        model_run_id='ridge-trace-test',
        mode='shadow',
        pipeline_run_id='pipeline-trace',
    )

    shadow_scores = _dynamic_scores(db_session, 'shadow')
    assert len(shadow_scores) == 3
    for score in shadow_scores:
        assert score.factor_set_id == 'fs-trace-001'


def test_shadow_score_contains_member_versions_json(db_session, tmp_path):
    """shadow 模式 Score 记录包含 factor_member_versions_json 成员版本快照。"""
    warehouse = FactorWarehouse(tmp_path / 'factor_trace_mv.duckdb')
    _seed_traceability_inputs(
        db_session, warehouse, factor_set_id='fs-trace-002'
    )

    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-trace-test',
        model_run_id='ridge-trace-test',
        mode='shadow',
        pipeline_run_id='pipeline-trace-mv',
    )

    shadow_scores = _dynamic_scores(db_session, 'shadow')
    assert len(shadow_scores) == 3
    for score in shadow_scores:
        assert score.factor_member_versions_json is not None
        snapshot = json.loads(score.factor_member_versions_json)
        # 快照包含所有特征因子
        assert set(snapshot.keys()) == set(FEATURE_CODES)
        # 每个成员包含 version/coefficient/normalized_weight
        for code in FEATURE_CODES:
            assert code in snapshot
            member = snapshot[code]
            assert member['version'] == 1
            assert member['coefficient'] == _COEFFICIENTS[code]
            assert member['normalized_weight'] == _COEFFICIENTS[code]


def test_ridge_score_contains_factor_set_id(db_session, tmp_path):
    """ridge 模式 Score 记录包含 factor_set_id。"""
    warehouse = FactorWarehouse(tmp_path / 'factor_trace_ridge.duckdb')
    _seed_traceability_inputs(
        db_session, warehouse, factor_set_id='fs-trace-ridge'
    )

    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-trace-test',
        model_run_id='ridge-trace-test',
        mode='ridge',
        pipeline_run_id='pipeline-trace-ridge',
    )

    ridge_scores = _dynamic_scores(db_session, 'ridge')
    assert len(ridge_scores) == 3
    for score in ridge_scores:
        assert score.factor_set_id == 'fs-trace-ridge'
        assert score.factor_member_versions_json is not None


def test_legacy_model_without_factor_set_id_yields_none(db_session, tmp_path):
    """legacy 模型（hyperparameters_json 无 factor_set_id）时 Score 的 factor_set_id 为 None。"""
    warehouse = FactorWarehouse(tmp_path / 'factor_trace_legacy.duckdb')
    _seed_traceability_inputs(
        db_session, warehouse, factor_set_id=None, model_run_id='ridge-legacy'
    )

    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-trace-test',
        model_run_id='ridge-legacy',
        mode='shadow',
        pipeline_run_id='pipeline-trace-legacy',
    )

    shadow_scores = _dynamic_scores(db_session, 'shadow')
    assert len(shadow_scores) == 3
    for score in shadow_scores:
        # legacy 模型无 factor_set_id
        assert score.factor_set_id is None
        # 成员版本快照仍然存在（来自 model.weights）
        assert score.factor_member_versions_json is not None


def test_factor_scores_json_contains_factor_set_id(db_session, tmp_path):
    """factor_scores_json 中的 _dynamic_model 解释包含 factor_set_id 追溯信息。"""
    warehouse = FactorWarehouse(tmp_path / 'factor_trace_json.duckdb')
    _seed_traceability_inputs(
        db_session, warehouse, factor_set_id='fs-trace-json'
    )

    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-trace-test',
        model_run_id='ridge-trace-test',
        mode='shadow',
        pipeline_run_id='pipeline-trace-json',
    )

    shadow_scores = _dynamic_scores(db_session, 'shadow')
    assert len(shadow_scores) == 3
    for score in shadow_scores:
        details = json.loads(score.factor_scores_json or '{}')
        dynamic = details.get('_dynamic_model', {})
        assert dynamic.get('factor_set_id') == 'fs-trace-json'
        assert dynamic.get('model_run_id') == 'ridge-trace-test'


def test_manual_mode_score_has_no_factor_set_id(db_session, tmp_path):
    """manual 模式不写入动态因子，Score 的 factor_set_id 保持 None。"""
    warehouse = FactorWarehouse(tmp_path / 'factor_trace_manual.duckdb')
    _seed_traceability_inputs(
        db_session, warehouse, factor_set_id='fs-trace-manual'
    )

    materialize_factor_scores(
        db_session,
        None,
        trade_date=_TRADE_DATE,
        mode='manual',
    )

    # manual 模式不创建新 Score，原有 manual score 的 factor_set_id 为 None
    manual_scores = _dynamic_scores(db_session, 'manual')
    for score in manual_scores:
        assert score.factor_set_id is None
