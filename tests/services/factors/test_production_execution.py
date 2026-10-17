"""Cost/contract integrity and a genuine isolated pending-candidate fit."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from app.services.factors import production_execution as execution
from app.services.factors import production_admission as admission
from tests.test_turnover_v3_public_warehouse_lifecycle import turnover_client


def portfolio():
    return SimpleNamespace(id=1, currency='CNY', total_capital=500000., investable_ratio=.9,
                           buy_fee_pct=0., sell_fee_pct=.00025)


def test_actual_cost_aliases_keep_zero_and_expose_each_fallback():
    snap = SimpleNamespace(id='applied', snapshot_hash='a'*64,
        cost_config_json=json.dumps({'buy_commission_pct':0.,'sell_commission_pct':0.,
            'buy_slippage_bps':5,'sell_slippage_bps':5,'stamp_duty_pct':.001}))
    cost = execution.normalize_portfolio_cost(portfolio(), snap)
    assert cost['fees'] == {'buy_commission_rate':0., 'sell_commission_rate':0.,
        'min_commission':5., 'stamp_tax_rate':.001, 'transfer_fee_rate':.00001,
        'slippage_buy_bps':5., 'slippage_sell_bps':5.}
    assert cost['evaluation_capital'] == 450000.
    assert cost['provenance']['min_commission'] == 'matching_engine.default'
    assert cost['provenance']['buy_commission_rate'] == 'strategy_snapshot.buy_commission_pct'
    assert cost['regulatory_rates_verified'] is False


@pytest.mark.parametrize('value', [None, True, -.01, float('nan'), float('inf'), 2., '0.0003'])
def test_invalid_cost_is_not_silently_defaulted(value):
    snap=SimpleNamespace(id='applied',snapshot_hash='a'*64,
        cost_config_json=json.dumps({'commission_rate':value}))
    with pytest.raises(ValueError, match='invalid_cost'):
        execution.normalize_portfolio_cost(portfolio(), snap)


def test_conflicting_aliases_and_invalid_capital_are_rejected():
    snap=SimpleNamespace(cost_config_json='{"commission_rate":0.0003,"buy_commission_pct":0.0}')
    with pytest.raises(ValueError,match='conflicting_cost_aliases'):
        execution.normalize_portfolio_cost(portfolio(),snap)
    p=portfolio();p.investable_ratio=0
    with pytest.raises(ValueError,match='invalid_evaluation_capital'):
        execution.normalize_portfolio_cost(p)


def write_contract(tmp_path, monkeypatch, protocol, source):
    monkeypatch.setattr(admission,'PLAN_ROOT',tmp_path/'plans')
    monkeypatch.setattr(execution,'EXECUTION_ROOT',tmp_path/'executions')
    plan_id=admission.fingerprint(protocol)
    plan={'schema_version':1,'plan_id':plan_id,'identity':protocol,
          'frozen_at':datetime.now(timezone.utc).isoformat()}
    admission.PLAN_ROOT.mkdir()
    sha=admission.fingerprint(plan)
    (admission.PLAN_ROOT/(plan_id+'.json')).write_text(admission.canonical({'plan':plan,'sha256':sha}))
    contract={'schema_version':1,'plan_id':plan_id,'plan_sha256':sha,'protocol':protocol,
        'source':source,'training_cutoff':protocol['observed_through'],
        'cost':execution.normalize_portfolio_cost(portfolio()),
        'locked_at':datetime.now(timezone.utc).isoformat()}
    execution._publish(execution.EXECUTION_ROOT/(plan_id+'.json'),
        {'contract':contract,'sha256':admission.fingerprint(contract)})
    return contract, {'plan_id':plan_id,'execution_sha256':admission.fingerprint(contract)}


def test_real_pending_candidate_uses_locked_policy_and_blocks_every_activation(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_trusted_history_training_pipeline import _dataset, _frozen_set
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    from app.services.market_history_evidence import load_trusted_history_dataset
    from app.services.factors.ridge_model import ModelGate, train_rolling_ridge
    from app.services.factors.production_candidate_task import run_candidate
    from app.services.factors import input_replay
    from app.services.factors.factor_engine import resolve_frozen_factor_specs
    from app.services.factors.store import FactorWarehouse
    from app.models.factor_model import FactorModelRun
    from app.models.score import Score
    _seed_local_market(db_session,tmp_path,day_count=2)
    set_id=_frozen_set(turnover_client,db_session)
    dataset_id,days,directory=_dataset(tmp_path,monkeypatch,day_count=310,illiquid_day=200)
    dataset=load_trusted_history_dataset(dataset_id)
    specs=resolve_frozen_factor_specs(db_session,(set_id,))
    parent={'input_fingerprint':'a'*64}
    protocol={'parent_model_id':'research-parent','parent_fingerprint':admission.fingerprint(parent),
        'factor_set_id':set_id,'feature_versions':{s.code:s.version for s in specs},
        'observed_through':str(days[-1]),'symbols':list(dataset.symbols),
        'minimum_raw_trade_dates':274,'window_days':250,'validation_days':50,
        'alphas':[.1,1.,10.,100.],'fit_policy':'cross_sectional_rank_v1',
        'model_gate':asdict(ModelGate()),'governance_gate_config':{
            'coverage_threshold':.7,'ic_min':-1.,'ic_max':1.,'lookback_days':30,
            'min_sample_count':0,'max_active_ic_delta_pct':.5}}
    source={'bundle_id':'isolated-bundle','bundle_manifest_sha256':'b'*64,
            'dataset_manifest_sha256':dataset.manifest_sha256}
    contract,identity=write_contract(tmp_path,monkeypatch,protocol,source)
    monkeypatch.setattr(execution,'current_runtime_baseline',lambda db:None)
    monkeypatch.setattr(input_replay,'_load_inputs',lambda *args:({'dataset_id':dataset_id},{}))
    monkeypatch.setattr(admission,'_parent_context',lambda *args:(deepcopy(parent),{}))
    runtime=turnover_client.get('/api/v1/factor-models/runtime').json()
    db_session.rollback()
    result=run_candidate(db_session,identity)
    candidate=result['candidate'];model_id=candidate['model_run_id']
    assert candidate['status']=='pending_validation',candidate
    assert candidate['trade_date_count']==250
    assert result['full_cohort_denominator']==24
    assert result['production_scores_published']==0 and not result['model_activated']
    model=db_session.get(FactorModelRun,model_id)
    hp=json.loads(model.hyperparameters_json);metrics=json.loads(model.metrics_json)
    assert hp['production_admission']==identity and 'trusted_history_dataset' not in hp
    assert metrics['production_model_gate_status']=='validated'
    assert metrics['trusted_joint_sample_quality']['minimum_coverage']==.90
    from app.services.factors.training_input_snapshot import load_training_input_snapshot
    frame,manifest=load_training_input_snapshot(model.artifact_path)
    assert manifest['split_metadata']['production_admission']==identity
    assert not frame.trade_date.isin([days[195],days[199]]).any()
    for prefix in ('/api/v1/factor-models/','/api/v1/scoring/models/'):
        response=turnover_client.post(prefix+model_id+'/activate',json={'mode':'ridge'})
        assert response.status_code in (400,409),response.text
    detail=turnover_client.get('/api/v1/factor-models/'+model_id).json()
    assert detail['readiness_code']=='MODEL_INDEPENDENT_QUALIFICATION_PENDING'
    assert not detail['production_eligible']
    assert turnover_client.get('/api/v1/factor-models/runtime').json()==runtime
    assert db_session.scalars(select(Score)).all()==[]
    with pytest.raises(ValueError,match='contract_already_locked'):
        run_candidate(db_session,identity)
    # The normal HTTP training path cannot attach forged production metadata.
    with pytest.raises(ValueError,match='reserved_metadata'):
        train_rolling_ridge(db_session,FactorWarehouse(tmp_path/'not-opened.duckdb'),
            factor_calc_batch_id='x',target_calc_batch_id='y',hyperparameters={'production_admission':identity})
    path=execution.EXECUTION_ROOT/(contract['plan_id']+'.json')
    envelope=json.loads(path.read_bytes());envelope['contract']['cost']['evaluation_capital']=1.
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError,match='contract_changed'):
        execution.load_execution(contract['plan_id'])
