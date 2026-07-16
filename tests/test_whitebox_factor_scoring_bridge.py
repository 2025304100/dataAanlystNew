from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import func, select

pytest.importorskip('duckdb')

from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
from app.models.scan import ScanResult
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.ridge_model import FEATURE_CODES
from app.services.factors.scoring_bridge import materialize_factor_scores
from app.services.factors.store import FactorWarehouse
from app.services.scans import run_scan

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


def _seed_bridge_inputs(db_session, warehouse: FactorWarehouse):
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
                scoring_config_snapshot_json=json.dumps(
                    {
                        'final_weights': {
                            'quality': 0.4,
                            'timing': 0.5,
                            'news': 0.1,
                        }
                    }
                ),
                dimension_scores_json=json.dumps({'manual_dimension': 60}),
                factor_scores_json=json.dumps(
                    {
                        'trend_score': {
                            'normalized_value': 60,
                            'weight': 1,
                            'contribution': 60,
                        }
                    }
                ),
                weight_mode='manual',
                calc_batch_id='manual-bridge-test',
            )
        )
    db_session.add_all(manual_scores)

    model = FactorModelRun(
        id='ridge-bridge-test',
        model_type='ridge',
        asset_type='stock',
        target_code='target_5d_return',
        status='validated',
        data_cutoff_at=_CUTOFF,
        sample_count=1000,
        symbol_count=100,
        trade_date_count=250,
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
                    'calc_batch_id': 'factor-score-test',
                    'created_at': _CUTOFF,
                }
            )
    factor_rows.append(
        {
            'symbol': '000003',
            'trade_date': _TRADE_DATE,
            'factor_code': 'lhb_institution_net_ratio',
            'factor_version': 1,
            'raw_value': 0.08,
            'winsorized_value': 0.08,
            'normalized_value': 1.2,
            'is_imputed': False,
            'imputation_method': None,
            'eligible': True,
            'data_cutoff_at': _CUTOFF,
            'calc_batch_id': 'factor-score-test',
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


def test_manual_mode_performs_no_dynamic_write(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'factor.duckdb')
    _seed_bridge_inputs(db_session, warehouse)
    before = db_session.scalar(select(func.count()).select_from(Score))

    result = materialize_factor_scores(
        db_session,
        None,
        trade_date=_TRADE_DATE,
        mode='manual',
    )

    assert result.materialized_count == 0
    assert result.calc_batch_id is None
    assert db_session.scalar(select(func.count()).select_from(Score)) == before


def test_shadow_clones_manual_scores_and_records_full_explanation(
    db_session, tmp_path
):
    warehouse = FactorWarehouse(tmp_path / 'factor.duckdb')
    _, manual_scores, _ = _seed_bridge_inputs(db_session, warehouse)

    result = materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-score-test',
        model_run_id='ridge-bridge-test',
        mode='shadow',
        pipeline_run_id='pipeline-1',
    )
    repeated = materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-score-test',
        model_run_id='ridge-bridge-test',
        mode='shadow',
        pipeline_run_id='pipeline-1',
    )
    shadow_scores = _dynamic_scores(db_session, 'shadow')

    assert result.materialized_count == 3
    assert repeated.calc_batch_id == result.calc_batch_id
    assert len(shadow_scores) == 3
    for manual, shadow in zip(manual_scores, shadow_scores):
        assert shadow.quality_score == manual.quality_score
        assert shadow.timing_score == manual.timing_score
        assert shadow.priority_score == manual.priority_score
        assert shadow.stage == manual.stage
        assert shadow.action == manual.action
        detail = json.loads(shadow.factor_scores_json)['_dynamic_model']
        assert detail['model_run_id'] == 'ridge-bridge-test'
        assert set(detail['factors']) == set(FEATURE_CODES)
        assert detail['factors']['turnover_z20']['coefficient'] == -0.1
        assert detail['macro']['regime'] == 'neutral'
        assert shadow.factor_data_cutoff_at == _CUTOFF
    last_detail = json.loads(
        shadow_scores[-1].factor_scores_json
    )['_dynamic_model']
    event = last_detail['event_factors']['lhb_institution_net_ratio']
    assert event['raw_value'] == 0.08
    assert event['coefficient'] is None
    assert event['contribution'] is None


def test_ridge_blends_independent_score_batch(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'factor.duckdb')
    _seed_bridge_inputs(db_session, warehouse)

    result = materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-score-test',
        model_run_id='ridge-bridge-test',
        mode='ridge',
    )
    ridge_scores = _dynamic_scores(db_session, 'ridge')

    assert result.materialized_count == 3
    assert [score.factor_quality_score for score in ridge_scores] == [
        0.0,
        50.0,
        100.0,
    ]
    assert [score.factor_timing_score for score in ridge_scores] == [
        0.0,
        50.0,
        100.0,
    ]
    assert [score.quality_score for score in ridge_scores] == [
        36.0,
        56.0,
        76.0,
    ]
    assert [score.timing_score for score in ridge_scores] == [
        20.0,
        45.0,
        70.0,
    ]
    assert ridge_scores[-1].priority_score > ridge_scores[0].priority_score
    assert all(score.factor_model_run_id == 'ridge-bridge-test' for score in ridge_scores)


def test_shadow_scan_reads_manual_and_explicit_ridge_reads_ridge(
    db_session, tmp_path
):
    warehouse = FactorWarehouse(tmp_path / 'factor.duckdb')
    symbols, _, _ = _seed_bridge_inputs(db_session, warehouse)
    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-score-test',
        model_run_id='ridge-bridge-test',
        mode='shadow',
    )
    materialize_factor_scores(
        db_session,
        warehouse,
        trade_date=_TRADE_DATE,
        factor_calc_batch_id='factor-score-test',
        model_run_id='ridge-bridge-test',
        mode='ridge',
    )
    shadow_scores = _dynamic_scores(db_session, 'shadow')
    shadow_scores[0].priority_score = 99
    ridge_scores = _dynamic_scores(db_session, 'ridge')
    ridge_scores[0].priority_score = 98
    db_session.flush()

    shadow_scan = run_scan(
        db=db_session,
        scope_snapshot={'asset_types': ['stock']},
        filters_snapshot={'min_score': 0, 'factor_weight_mode': 'shadow'},
        portfolio_id=None,
        portfolio_rule_id=None,
        run_name='shadow-scan',
        preset_id=None,
    )
    ridge_scan = run_scan(
        db=db_session,
        scope_snapshot={'asset_types': ['stock']},
        filters_snapshot={
            'min_score': 0,
            'factor_weight_mode': 'ridge',
            'factor_model_run_id': 'ridge-bridge-test',
        },
        portfolio_id=None,
        portfolio_rule_id=None,
        run_name='ridge-scan',
        preset_id=None,
    )
    db_session.flush()

    shadow_top = db_session.execute(
        select(ScanResult).where(
            ScanResult.scan_run_id == shadow_scan.id,
            ScanResult.result_type == 'quality',
            ScanResult.rank_no == 1,
        )
    ).scalar_one()
    ridge_top = db_session.execute(
        select(ScanResult).where(
            ScanResult.scan_run_id == ridge_scan.id,
            ScanResult.result_type == 'quality',
            ScanResult.rank_no == 1,
        )
    ).scalar_one()
    assert shadow_top.symbol_id == symbols[-1].id
    assert ridge_top.symbol_id == symbols[0].id
    assert json.loads(shadow_scan.filters_snapshot)['score_weight_mode'] == 'manual'
    assert json.loads(ridge_scan.filters_snapshot)['score_weight_mode'] == 'ridge'


def test_rejected_model_cannot_be_materialized(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'factor.duckdb')
    _, _, model = _seed_bridge_inputs(db_session, warehouse)
    model.status = 'rejected'
    db_session.flush()

    with pytest.raises(ValueError, match='must be validated'):
        materialize_factor_scores(
            db_session,
            warehouse,
            trade_date=_TRADE_DATE,
            factor_calc_batch_id='factor-score-test',
            model_run_id=model.id,
            mode='ridge',
        )
