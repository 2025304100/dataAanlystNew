"""Real final evaluation compares the frozen candidate panel with active factor versions."""
from datetime import datetime

import pandas as pd
import pytest

from app.models.factor import Factor
from app.models.factor_mining import FactorMiningRun
from app.models.factor_model import FactorVersion
from app.services.factors.mining.contracts import MiningContext
from app.services.factors.mining.evaluation_adapter import build_split, evaluate_full
from app.services.factors.store import FactorWarehouse
from test_m1_closure import warehouse_path
from test_m1_closure import mining_env
from test_factor_mining_routes import _client
from test_mining_runtime_integrity import _seed, _inputs, _wait

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize('broken_pin', [False, True])
def test_final_evaluation_compares_pinned_active_version_on_its_frozen_panel(db_session, warehouse_path, broken_pin):
    db = db_session
    baseline = Factor(code='pinned_baseline', name='Pinned baseline', category='trend',
                      direction='positive', status='active', lifecycle_status='active', is_active=1)
    db.add(baseline)
    db.flush()
    active = FactorVersion(factor_id=baseline.id, version=1, formula_expr='close',
                           validation_status='valid', is_latest=0)
    newer = FactorVersion(factor_id=baseline.id, version=2, formula_expr='pe_ttm',
                          validation_status='valid', is_latest=1)
    db.add_all([active, newer])
    db.flush()
    pin = active.id
    if broken_pin:
        other = Factor(code='foreign_owner', name='Foreign owner', category='trend',
                       direction='positive', status='draft', is_active=0)
        db.add(other)
        db.flush()
        foreign = FactorVersion(factor_id=other.id, version=1, formula_expr='close',
                                validation_status='valid')
        db.add(foreign)
        db.flush()
        pin = foreign.id
    baseline.active_version_id = pin
    run = FactorMiningRun(id='novelty-run', status='running',
        candidate_pool_snapshot_id='novelty-snapshot', data_cutoff_at=datetime(2027, 3, 1),
        start_date=datetime(2026, 1, 5), end_date=datetime(2027, 2, 1),
        rebalance_frequency='daily', target_horizon=5, total_trials=1)
    db.add(run)
    db.commit()
    wh = FactorWarehouse(warehouse_path)
    targets, _, _ = wh.get_target_panel('a5-target', 'target_5d_return',
        start_date=run.start_date.date(), end_date=run.end_date.date(),
        as_of_exit_date=run.data_cutoff_at.date())
    dates = sorted(set(pd.to_datetime(targets['signal_date']).dt.date))
    split, budget = build_split(all_dates=dates, frequency='daily', target_horizon=5,
                                tail_loss_points=0)
    ctx = MiningContext(run_id=run.id, candidate_pool_snapshot_id='novelty-snapshot',
        data_cutoff_at=run.data_cutoff_at, start_date=run.start_date.date(),
        end_date=run.end_date.date(), rebalance_frequency='daily', target_horizon=5,
        split=split, purge_points=budget.purge_points, embargo_points=budget.embargo_points,
        train_ratio=.6, validation_ratio=.2, random_seed=42, config_hash='novelty',
        split_algorithm_version='split-1.0.0', target_calc_batch_id='a5-target',
        warehouse_path=warehouse_path, candidate_symbols=tuple(sorted(targets['symbol'].unique())),
        signal_dates=tuple(dates))
    outcome = evaluate_full(ctx, individual={'formula': 'close', 'canonical_formula': 'close',
        'formula_hash': 'a12b34c56d78e90fa12b34c56d78e90f'}, db=db,
        factor_values=None, forward_returns=None, train_icir=.3)
    evidence = outcome.metrics['novelty']
    if broken_pin:
        assert outcome.metrics['correlation'] is None
        assert evidence['status'] == 'incomplete'
        assert evidence['reference_version_ids'] == [pin]
        return
    assert outcome.metrics['correlation'] == pytest.approx(1.0)
    assert evidence['status'] == 'complete'
    assert evidence['reference_version_ids'] == [active.id]
    assert evidence['compared_reference_count'] == 1


def test_unknown_reference_data_does_not_label_real_worker_results_as_d(mining_env, warehouse_path):
    db = mining_env
    _seed(db)
    baseline = Factor(code='missing_reference', name='Missing reference', category='valuation',
        direction='positive', status='active', lifecycle_status='active', is_active=1)
    db.add(baseline)
    db.flush()
    version = FactorVersion(factor_id=baseline.id, version=1, formula_expr='pe_ttm',
                            validation_status='valid')
    db.add(version)
    db.flush()
    baseline.active_version_id = version.id
    db.commit()
    client = _client(db)
    payload = _inputs(warehouse_path, finalize=1)
    payload['evolution_params']['ai_enabled'] = False
    created = client.post('/factor-mining/runs', json=payload)
    assert created.status_code == 201, created.text
    run_id = created.json()['run_id']
    detail = _wait(client, db, run_id, lambda r: r['status'] in ('succeeded', 'failed'))
    assert detail['status'] == 'succeeded', detail
    candidates = client.get(f'/factor-mining/runs/{run_id}/candidates').json()['items']
    evaluated = next(c for c in candidates if c['latest_evaluation_id'])
    evidence = client.get(f"/factor-mining/candidates/{evaluated['id']}/grade/evidence").json()
    assert evidence['grade'] is None, evidence
    assert '相关性' in evidence['reason_zh']
    evaluation = client.get(f"/factor-mining/evaluations/{evaluated['latest_evaluation_id']}").json()
    assert evaluation['metrics']['novelty']['status'] == 'incomplete'
    assert evaluation['metrics']['correlation'] is None
