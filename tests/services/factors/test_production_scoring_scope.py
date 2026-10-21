"""Prospective models cannot bypass confirmation or their locked cohort."""
import json

import pytest
from sqlalchemy import select

from app.models.factor_model import FactorModelRun
from app.models.factor_governance import ScoringLineage
from app.models.score import Score
from app.services.factors.store import FactorWarehouse
from app.services.factors.scoring_bridge import validate_production_scoring_scope
from tests.test_factor_score_publication_scope import _seed, _publish, MODEL, DAY, BATCH, CUTOFF

pytestmark = pytest.mark.whitebox


def test_validated_flag_cannot_publish_prospective_scores_without_confirmation(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'unconfirmed-cohort.duckdb')
    _seed(db_session, warehouse)
    model = db_session.get(FactorModelRun, MODEL)
    model.hyperparameters_json = json.dumps({'production_admission': {
        'plan_id': 'a' * 64, 'execution_sha256': 'b' * 64}})
    db_session.flush()
    with pytest.raises(ValueError, match='本人确认|确认记录|资料或记录不一致'):
        _publish(db_session, warehouse)
    assert list(db_session.scalars(select(Score))) == []
    assert list(db_session.scalars(select(ScoringLineage))) == []


@pytest.fixture
def scope_inputs(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / 'locked-cohort.duckdb')
    _seed(db_session, warehouse)
    # This scope-only input is not a qualification certificate. Real release
    # confirmation remains covered by the public 50-date evidence workflow.
    protocol = {'symbols': ['000001', '000002', '000003'], 'feature_versions': {'ep_ttm': 1}}
    return warehouse, protocol


def check_scope(inputs, target=None):
    warehouse, protocol = inputs
    return validate_production_scoring_scope(warehouse, protocol=protocol,
        trade_date=DAY, factor_calc_batch_id=BATCH,
        target_symbols=frozenset(target) if target is not None else None)


@pytest.mark.parametrize('target', [None, ('000003',), ('000001', '000003')])
def test_checked_protocol_keeps_full_inference_pool_for_publication_subsets(scope_inputs, target):
    approved, publication = check_scope(scope_inputs, target)
    assert approved == {'000001', '000002', '000003'}
    assert publication == (approved if target is None else set(target))


@pytest.mark.parametrize('target', [None, ('000003',)])
@pytest.mark.parametrize('eligible', [True, False])
def test_checked_protocol_rejects_outside_normalization_input_even_for_inside_output(scope_inputs, db_session, target, eligible):
    warehouse, _ = scope_inputs
    warehouse.upsert_records('factor_values', [{'symbol': '000004', 'trade_date': DAY,
        'factor_code': 'ep_ttm', 'factor_version': 1, 'raw_value': 100000., 'normalized_value': 5.,
        'is_imputed': False, 'eligible': eligible, 'data_cutoff_at': CUTOFF, 'calc_batch_id': BATCH, 'created_at': CUTOFF}])
    with pytest.raises(ValueError, match='原股票池重新计算'):
        check_scope(scope_inputs, target)
    assert list(db_session.scalars(select(Score))) == []
    assert list(db_session.scalars(select(ScoringLineage))) == []


def test_checked_protocol_rejects_requested_outside_symbol(scope_inputs):
    with pytest.raises(ValueError, match='只能发布'):
        check_scope(scope_inputs, ('000004',))


@pytest.mark.parametrize('change', ["DELETE FROM factor_values WHERE symbol='000001'",
    "UPDATE factor_values SET factor_version=2 WHERE symbol='000001'"])
def test_checked_protocol_rejects_missing_pool_rows_or_wrong_versions(scope_inputs, change):
    with scope_inputs[0].connection() as conn:
        conn.execute(change)
    with pytest.raises(ValueError, match='版本记录'):
        check_scope(scope_inputs)


@pytest.mark.parametrize('change', ["UPDATE factor_values SET eligible=false WHERE symbol='000001'",
    "UPDATE factor_values SET normalized_value=NULL WHERE symbol='000001'",
    "UPDATE factor_values SET normalized_value='NaN'::DOUBLE WHERE symbol='000001'"])
def test_checked_protocol_keeps_explicit_unavailable_original_pool_records(scope_inputs, db_session, change):
    with scope_inputs[0].connection() as conn:
        conn.execute(change)
    approved, publication = check_scope(scope_inputs)
    assert approved == publication == {'000001', '000002', '000003'}
    # Existing score publication still excludes unavailable inputs; scope
    # membership never becomes an invented zero or a production certificate.
    result = _publish(db_session, scope_inputs[0], target_symbols=tuple(publication))
    scores = list(db_session.scalars(select(Score).where(Score.calc_batch_id == result.calc_batch_id)))
    assert len(scores) == 2 and result.missing_factor_count == 1


def test_unrelated_batch_does_not_expand_the_checked_cohort(scope_inputs):
    warehouse, _ = scope_inputs
    warehouse.upsert_records('factor_values', [{'symbol': '000004', 'trade_date': DAY,
        'factor_code': 'ep_ttm', 'factor_version': 1, 'raw_value': 99999., 'normalized_value': 5.,
        'is_imputed': False, 'eligible': True, 'data_cutoff_at': CUTOFF, 'calc_batch_id': 'another-batch', 'created_at': CUTOFF}])
    assert check_scope(scope_inputs)[0] == {'000001', '000002', '000003'}
