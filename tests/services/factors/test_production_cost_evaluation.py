"""Cash-conserving cost replay: no SQL session, order publisher or network."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.factors import production_cost_evaluation as evaluation
from app.services.factors import production_execution as execution
from app.services.factors import production_admission as admission
from app.services.factors import production_prediction_journal as journal

pytestmark = pytest.mark.whitebox


@pytest.fixture
def panel():
    days = tuple(pd.bdate_range('2027-01-04', periods=55).date)
    symbols = tuple(f'{n:06d}' for n in range(1, 6))
    signals = [{'date': d, 'predictions': {s: 6 - i for i, s in enumerate(symbols)}} for d in days[:50]]
    cost = execution.normalize_portfolio_cost(SimpleNamespace(id=1, currency='CNY',
        total_capital=500000., investable_ratio=.9, buy_fee_pct=0., sell_fee_pct=0.))
    raw = {(s, d): {'open':100.,'high':101.,'low':99.,'close':100.,'volume':1000000.,'amount':100000000.}
        for s in symbols for d in days}
    qfq = {(s, d): row['close'] for (s, d), row in raw.items()}
    return dict(signals=signals, calendar=days, symbols=symbols, raw_bars=raw, cost=cost, qfq_closes=qfq)


def test_real_matcher_minimum_commission_sell_tax_slippage_and_cash_conserve(panel):
    result = evaluation.replay_cash_sleeves(**panel)
    first = result['traces'][0]['orders'][0]
    assert first['buy']['filled_quantity'] == 800  # 900 shares + costs exceed 90000 CNY.
    assert first['buy']['commission'] == 5.
    assert first['buy']['executed_price'] == 100.05
    assert first['sell']['executed_price'] == 99.95
    assert first['sell']['commission'] == 5.
    assert first['sell']['stamp_tax'] == 39.98
    assert first['net_profit'] == pytest.approx(-131.58)
    assert result['round_trips'] == 50 and result['net_return'] < 0
    assert result['ending_cash'] == pytest.approx(450000 + sum(o['net_profit'] for t in result['traces'] for o in t['orders']))
    assert result['ending_cash'] == pytest.approx(sum(result['sleeve_ending_cash']))
    assert result['orders_submitted'] == 0 and not result['qualification_certificate']
    gross = evaluation.replay_cash_sleeves(**panel, zero_cost=True)
    assert gross['net_return'] == 0 and gross['fees_paid'] == 0
    assert gross['traces'][0]['orders'][0]['buy']['filled_quantity'] == 900


def test_future_untradable_top_stock_never_replaced_with_next_ranked_stock(panel):
    top = panel['symbols'][0]
    # Known zero-volume bars are suspensions. All selected entries retain cash.
    for d in panel['calendar']:
        panel['raw_bars'][(top,d)]['volume'] = 0.
    result = evaluation.replay_cash_sleeves(**panel)
    assert result['round_trips'] == 0 and result['ending_cash'] == result['capital']
    assert all(t['orders'][0]['symbol'] == top for t in result['traces'])
    assert all(t['orders'][0]['outcome'] == 'cash_retained_no_replacement' for t in result['traces'])


def test_unfilled_exit_blocks_instead_of_discarding_losing_position(panel):
    panel['raw_bars'][(panel['symbols'][0],panel['calendar'][5])]['volume'] = 0.
    with pytest.raises(ValueError, match='exit_unresolved_no_position_discard'):
        evaluation.replay_cash_sleeves(**panel)


def test_missing_raw_bar_is_not_treated_as_a_known_suspension(panel):
    del panel['raw_bars'][(panel['symbols'][0],panel['calendar'][1])]
    with pytest.raises(ValueError, match='missing_or_invalid_execution_bar'):
        evaluation.replay_cash_sleeves(**panel)


def test_labels_cannot_filter_the_pre_entry_top_quintile(panel):
    # Future qfq/price evidence is missing for the highest prediction. Reject;
    # never substitute the next-ranked stock or hide the missing outcome.
    panel['qfq_closes'].pop((panel['symbols'][0],panel['calendar'][4]))
    with pytest.raises(ValueError, match='adjustment_evidence_missing'):
        evaluation.replay_cash_sleeves(**panel)


def test_corporate_action_blocks_but_price_rounding_does_not(panel):
    s = panel['symbols'][0]
    for n,d in enumerate(panel['calendar']):
        price = round(100 + n * .13,2)
        panel['raw_bars'][(s,d)].update(open=price,close=price,high=price+1,low=price-1)
        panel['qfq_closes'][(s,d)] = round(price * .7321,2)
    assert evaluation.replay_cash_sleeves(**panel)['round_trips'] == 50
    panel['qfq_closes'][(s,panel['calendar'][4])] *= .9
    with pytest.raises(ValueError, match='corporate_action_requires_explicit_treatment'):
        evaluation.replay_cash_sleeves(**panel)


@pytest.mark.parametrize('mutation', ['date_gap','immature','cohort','coverage'])
def test_cannot_select_better_dates_or_reduce_denominator(panel,mutation):
    if mutation == 'date_gap': panel['signals'][20]['date'] = panel['calendar'][21]
    if mutation == 'immature': panel['calendar'] = panel['calendar'][:-1]
    if mutation == 'cohort': del panel['signals'][0]['predictions'][panel['symbols'][0]]
    if mutation == 'coverage': panel['signals'][0]['predictions'][panel['symbols'][0]] = None
    with pytest.raises((ValueError, KeyError)):
        evaluation.replay_cash_sleeves(**panel)


def test_expensive_stock_keeps_cash_without_negative_balance(panel):
    panel['cost']['evaluation_capital'] = 100.
    result = evaluation.replay_cash_sleeves(**panel)
    assert result['ending_cash'] == 100. and result['round_trips'] == 0


def test_buy_size_includes_the_matchers_actual_tick_rounding(panel):
    panel['cost']['evaluation_capital'] = 50075.
    for key,row in panel['raw_bars'].items():
        row.update(open=10.003,close=10.003,high=10.1,low=9.9)
        panel['qfq_closes'][key] = 10.003
    result = evaluation.replay_cash_sleeves(**panel)
    first = result['traces'][0]['orders'][0]
    assert first['buy']['filled_quantity'] == 900
    assert first['entry_debit'] <= 10015.


@pytest.mark.parametrize('value', [-1.,True,float('nan'),float('inf')])
def test_invalid_fee_cannot_become_a_profitable_rebate(panel,value):
    panel['cost']['fees']['min_commission'] = value
    with pytest.raises(ValueError,match='invalid_frozen_fee'):
        evaluation.replay_cash_sleeves(**panel)


def test_cost_policy_is_frozen_before_holdout_and_cannot_change_costs(tmp_path, monkeypatch, panel):
    contract = {'plan_id':'a'*64,'protocol':{'observed_through':'2026-10-17'},'cost':panel['cost']}
    candidate = {'model_id':'candidate'}
    monkeypatch.setattr(execution,'EXECUTION_ROOT',tmp_path/'executions')
    monkeypatch.setattr(journal,'_candidate',lambda *args:(deepcopy(contract),deepcopy(candidate)))
    monkeypatch.setattr(journal,'journal_status',lambda *args:{'journal_count':0})
    class BeforeHoldout(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,17,8,tzinfo=timezone.utc)
    monkeypatch.setattr(evaluation,'datetime',BeforeHoldout)
    frozen = evaluation.freeze_cost_policy(None,contract['plan_id'])
    assert evaluation.freeze_cost_policy(None,contract['plan_id']) == frozen
    changed = deepcopy(contract);changed['cost']['fees']['min_commission'] = 1.
    with pytest.raises(ValueError,match='policy_changed'):
        evaluation.load_cost_policy(changed)
    class AfterHoldout(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,19,8,tzinfo=timezone.utc)
    monkeypatch.setattr(evaluation,'datetime',AfterHoldout)
    monkeypatch.setattr(execution,'EXECUTION_ROOT',tmp_path/'other-executions')
    with pytest.raises(ValueError,match='must_precede_independent_period'):
        evaluation.freeze_cost_policy(None,contract['plan_id'])


def test_http_status_is_readonly_and_never_claims_a_net_cost_result(tmp_path,monkeypatch,panel,db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from app.api.routes.scoring_facade import router
    from app.db.session import get_db
    contract = {'plan_id':'a'*64,'protocol':{'observed_through':'2026-10-17'},'cost':panel['cost']}
    monkeypatch.setattr(execution,'EXECUTION_ROOT',tmp_path/'absent')
    monkeypatch.setattr(journal,'_candidate',lambda *args:(contract,{'model_id':'pending'}))
    monkeypatch.setattr(journal,'journal_status',lambda *args:{'journal_count':0})
    app = FastAPI();app.include_router(router,prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db_session
    statements = []
    def listen(conn,cursor,statement,*args):statements.append(statement.lstrip().split()[0].upper())
    event.listen(db_session.bind,'before_cursor_execute',listen)
    try:
        client = TestClient(app)
        response = client.get('/api/v1/scoring/production-candidates/' + contract['plan_id'] + '/independent-evaluation')
    finally:
        event.remove(db_session.bind,'before_cursor_execute',listen)
    assert response.status_code == 200,response.text
    result = response.json()
    assert result['net_cost_result'] is None and result['journal_count'] == 0
    assert not result['qualification_issued'] and not result['model_activated']
    assert result['cash_replay_engine_supported'] and result['independent_evidence_binding_supported']
    assert result['qualification_issuer_supported']
    assert 'COST_POLICY_NOT_FROZEN' in result['blockers']
    assert not {'INSERT','UPDATE','DELETE','REPLACE'} & set(statements)
    assert not execution.EXECUTION_ROOT.exists()
