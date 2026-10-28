"""Regression contracts for reviewed signal, delivery and runtime defects."""
from datetime import datetime, timedelta
from pathlib import Path
import inspect
import json
import socket

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_market_signals_api import signals_client, make_rule_and_evidence, seed_signal_prices  # noqa: F401
from app.models.ai_profile import AIProfile
from app.models.market_signal import MarketSignalAssessment, MarketSignalSettings, MarketSignalPrediction
from app.models.notification import NotificationOutbox
from app.services.market_signals import DEFAULT_SETTINGS

pytestmark = pytest.mark.whitebox
def observe(key, value):
    print(json.dumps({key: value}, ensure_ascii=False, default=str))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    original_connect = socket.socket.connect
    def forbidden(sock, address):
        # Windows asyncio implements its private wakeup pipe via socketpair().
        # Permit only that stdlib callsite; application transports remain blocked.
        caller = inspect.currentframe().f_back
        if caller.f_code.co_name == 'socketpair' and Path(caller.f_code.co_filename).name == 'socket.py':
            return original_connect(sock, address)
        raise AssertionError('This offline review must not open a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


def enable_without_starting_scheduler(db):
    # A valid, unused configuration. No inference or scheduler is invoked.
    profile = AIProfile(name='Offline review only', provider='openai_compatible', auth_type='none',
                        model='never-called', base_url='http://127.0.0.1:9/v1', purpose='explanation')
    db.add(profile)
    db.flush()
    db.add(MarketSignalSettings(id=1, config_json=json.dumps({**DEFAULT_SETTINGS, 'enabled': True,
        'ai_profile_id': profile.id, 'activation_at': '2026-10-20T00:00:00'})))
    db.commit()


def seed_assessment(db, rule_id, evidence_id, number, score=85):
    row = MarketSignalAssessment(evidence_id=evidence_id, rule_id=rule_id, rule_revision=1,
        request_key=f'review-{number}', status='scored', score=score,
        event_key=f'event-{number}', fact_key=f'fact-{number}',
        analysis_json=json.dumps({'novelty_points': 30, 'new_facts': ['新增订单100亿元'],
            'evidence_quotes': ['验收银行获得新增订单100亿元']}), input_json='{}', total_tokens=0)
    db.add(row)
    db.commit()
    return row


def test_real_failures_must_open_supplier_breaker(monkeypatch):
    from app.services import akshare_guard as guard, akshare_registry as registry
    from app.services.akshare_utils import call_akshare_with_retry
    monkeypatch.setattr(registry, 'is_api_enabled', lambda key: True)
    monkeypatch.setattr(registry, 'apply_delay', lambda key: None)
    guard.reset_all()
    calls = []
    def supplier():
        calls.append('request')
        response = requests.Response()
        response.status_code = 429
        raise requests.HTTPError('429 Too Many Requests', response=response)
    try:
        for _ in range(2):
            with pytest.raises(requests.HTTPError):
                call_akshare_with_retry(supplier, api_key='stock_individual_fund_flow', max_attempts=1)
        blocked = False
        try:
            call_akshare_with_retry(supplier, api_key='stock_individual_fund_flow', max_attempts=1)
        except guard.SupplierCoolingDown:
            blocked = True
        except requests.HTTPError:
            pass
        observe('supplier_breaker', {'network_allowed': False, 'supplier_stub_calls': len(calls),
            'third_call_blocked': blocked, 'guard': guard.snapshot()})
        assert blocked, f'Two 429s did not open breaker; supplier was called {len(calls)} times'
    finally:
        guard.reset_all()


@pytest.fixture
def supplier_clock(monkeypatch):
    from tests.test_capital_flow_retry_contract import Clock
    from app.services import akshare_guard, akshare_utils, akshare_registry
    clock = Clock()
    monkeypatch.setattr(akshare_guard, 'time', clock)
    monkeypatch.setattr(akshare_utils, 'time', clock)
    monkeypatch.setattr(akshare_guard, '_PACER_JITTER', 0)
    monkeypatch.setattr(akshare_registry, 'get_runtime_config', lambda _key: {
        'enabled': True, 'delay_min_ms': 0, 'delay_max_ms': 0, 'max_retries': 3})
    return clock


@pytest.mark.parametrize('failure,threshold', [(requests.ConnectionError('connection reset'), 5),
    (requests.HTTPError('403 Forbidden'), 2), (requests.HTTPError('429 Too Many Requests'), 2)])
def test_supplier_failures_open_before_next_external_call(supplier_clock, failure, threshold):
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.akshare_guard import SupplierCoolingDown
    calls = []
    def fail():
        calls.append(1)
        raise failure
    for _ in range(threshold):
        with pytest.raises(type(failure)):
            call_akshare_with_retry(fail, api_key='regression_supplier', max_attempts=1)
    with pytest.raises(SupplierCoolingDown):
        call_akshare_with_retry(fail, api_key='regression_supplier', max_attempts=1)
    assert len(calls) == threshold


def test_supplier_success_resets_failures_and_single_half_open_probe_recovers(supplier_clock):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.akshare_guard import SupplierCoolingDown
    key = 'regression_supplier'
    def fail():
        raise requests.HTTPError('429 Too Many Requests')
    with pytest.raises(requests.HTTPError):
        call_akshare_with_retry(fail, api_key=key, max_attempts=1)
    assert call_akshare_with_retry(lambda: 'ok', api_key=key) == 'ok'
    # Success reset: one more severe failure alone must not open the circuit.
    with pytest.raises(requests.HTTPError):
        call_akshare_with_retry(fail, api_key=key, max_attempts=1)
    assert call_akshare_with_retry(lambda: 'ok', api_key=key) == 'ok'
    for _ in range(2):
        with pytest.raises(requests.HTTPError):
            call_akshare_with_retry(fail, api_key=key, max_attempts=1)
    supplier_clock.value += 241
    entered, release = Event(), Event()
    def probe():
        entered.set()
        assert release.wait(3)
        return 'probe ok'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call_akshare_with_retry, probe, api_key=key)
        try:
            assert entered.wait(3)
            with pytest.raises(SupplierCoolingDown):
                call_akshare_with_retry(lambda: 'must not run', api_key=key)
        finally:
            release.set()
        assert future.result(timeout=3) == 'probe ok'
    assert call_akshare_with_retry(lambda: 'recovered', api_key=key) == 'recovered'


def test_supplier_retries_respect_breaker_and_global_pacing(supplier_clock, monkeypatch):
    from app.services import akshare_registry
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.akshare_guard import SupplierCoolingDown
    monkeypatch.setattr(akshare_registry, 'get_runtime_config', lambda _key: {
        'enabled': True, 'delay_min_ms': 1000, 'delay_max_ms': 1000, 'max_retries': 10})
    calls = []
    def fail():
        calls.append(supplier_clock.value)
        raise requests.ConnectionError('connection reset')
    with pytest.raises(SupplierCoolingDown):
        call_akshare_with_retry(fail, api_key='paced_supplier', max_attempts=10, base_delay=0)
    assert calls == [0, 1, 2, 3, 4]


def test_supplier_pacer_rejects_excess_backlog_without_contacting_early(supplier_clock, monkeypatch):
    from app.services import akshare_registry
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.akshare_guard import SupplierCoolingDown
    monkeypatch.setattr(akshare_registry, 'get_runtime_config', lambda _key: {
        'enabled': True, 'delay_min_ms': 40000, 'delay_max_ms': 40000, 'max_retries': 1})
    calls = []
    def supplier():
        calls.append(supplier_clock.value)
        return 'ok'
    assert call_akshare_with_retry(supplier, api_key='long_interval') == 'ok'
    with pytest.raises(SupplierCoolingDown):
        call_akshare_with_retry(supplier, api_key='long_interval')
    assert calls == [0]
    supplier_clock.value = 15
    assert call_akshare_with_retry(supplier, api_key='long_interval') == 'ok'
    assert calls == [0, 40]


def test_pacing_rejection_returns_half_open_probe_without_poisoning_supplier(supplier_clock, monkeypatch):
    from app.services import akshare_registry, akshare_guard as guard
    from app.services.akshare_utils import call_akshare_with_retry
    monkeypatch.setattr(akshare_registry, 'get_runtime_config', lambda _key: {
        'enabled': True, 'delay_min_ms': 300000, 'delay_max_ms': 300000, 'max_retries': 1})
    calls = []
    def supplier():
        calls.append(supplier_clock.value)
        return 'ok'
    key = 'half-open-backlog'
    call_akshare_with_retry(supplier, api_key=key)
    for _ in range(2):
        guard.record_failure(key, severe=True, error='isolated pre-existing outage')
    supplier_clock.value = 241
    with pytest.raises(guard.SupplierCoolingDown):
        call_akshare_with_retry(supplier, api_key=key)
    assert calls == [0]
    supplier_clock.value = 275  # Still within the 120s probe lease.
    assert call_akshare_with_retry(supplier, api_key=key) == 'ok'
    assert calls == [0, 300]


@pytest.mark.parametrize('expired_count', [499, 500, 501])
def test_fresh_candidate_must_not_be_starved_by_expired_scores(signals_client, db_session, monkeypatch, expired_count):
    from app.services import market_signal_sources as sources
    from app.models.market_signal import MarketSignalEvidence
    now = datetime(2026, 10, 27, 9, 10)
    monkeypatch.setattr(sources, 'utcnow', lambda: now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule, fresh = make_rule_and_evidence(signals_client, window_days=1)
    for number in range(expired_count):
        evidence = MarketSignalEvidence(fingerprint=f'review-expired-{number}', title=f'Expired event {number}',
            text='Distinct historical event with obsolete facts', source='offline-fixture',
            published_at=now-timedelta(days=10), time_quality='manual', metadata_json='{}')
        db_session.add(evidence)
        db_session.flush()
        db_session.add(MarketSignalAssessment(evidence_id=evidence.id, rule_id=rule['id'], rule_revision=1,
            request_key=f'expired-{number}', status='scored', score=95,
            event_key=f'expired-event-{number}', fact_key=f'expired-fact-{number}',
            analysis_json='{"novelty_points":30}', input_json='{}'))
    db_session.commit()
    fresh_score = seed_assessment(db_session, rule['id'], fresh['id'], 'fresh', 85)
    response = signals_client.post('/market-signals/run', json={'analyze': False})
    assert response.status_code == 200, response.text
    db_session.refresh(fresh_score)
    observe(f'candidate_history_{expired_count}', {'response': response.json(),
        'fresh_score': fresh_score.score, 'fresh_decision_reason': fresh_score.decision_reason,
        'expired_score': 95, 'expired_age_days': 10, 'max_age_hours': 24})
    assert response.json()['created'] == 1, 'Fresh eligible event lost behind expired top-500 history'


@pytest.mark.parametrize('blocker', ['paused_rule', 'old_revision', 'already_handled', 'no_novelty'])
def test_historical_or_ineligible_scores_do_not_block_other_live_candidates(signals_client, db_session, monkeypatch, blocker):
    from app.services import market_signal_sources as sources
    from app.models.market_signal import MarketSignalEvidence
    now = datetime(2026, 10, 27, 9, 10)
    monkeypatch.setattr(sources, 'utcnow', lambda: now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    live_rule, fresh = make_rule_and_evidence(signals_client, window_days=1)
    blocked_rule = live_rule
    if blocker == 'paused_rule':
        response = signals_client.post('/market-signals/rules', json={'name': 'Paused history',
            'target_type': 'symbol', 'target_key': '000001', 'enabled': False, 'adaptive_threshold': False})
        assert response.status_code == 201, response.text
        blocked_rule = response.json()
    for number in range(501):
        evidence = MarketSignalEvidence(fingerprint=f'block-{number}', title=f'Historical {number}',
            text='Independent facts', source='offline-fixture', published_at=now-timedelta(hours=1),
            time_quality='manual', metadata_json='{}')
        db_session.add(evidence)
        db_session.flush()
        assessment = MarketSignalAssessment(evidence_id=evidence.id, rule_id=blocked_rule['id'],
            rule_revision=0 if blocker == 'old_revision' else 1, request_key=f'block-{number}',
            status='scored', score=95, event_key=f'blocked-event-{number}', fact_key=f'blocked-fact-{number}',
            analysis_json=json.dumps({'novelty_points': 0 if blocker == 'no_novelty' else 30}), input_json='{}')
        db_session.add(assessment)
        db_session.flush()
        if blocker == 'already_handled':
            db_session.add(MarketSignalPrediction(dedup_key=f'handled-{number}',
                event_key=assessment.event_key, fact_key=assessment.fact_key, assessment_id=assessment.id,
                rule_id=blocked_rule['id'], rule_revision=1, target_type='symbol', target_key='000001',
                delivery_status='suppressed', snapshot_json='{}', outcome_status='not_applicable',
                outcome_json='{}', due_at=now, created_at=now-timedelta(days=1)))
    db_session.commit()
    seed_assessment(db_session, live_rule['id'], fresh['id'], 'fresh-after-blockers')
    response = signals_client.post('/market-signals/run', json={'analyze': False})
    assert response.status_code == 200, response.text
    assert response.json()['created'] == 1
    # A later monitor run must not create a second record for the same event.
    repeat = signals_client.post('/market-signals/run', json={'analyze': False})
    assert repeat.status_code == 200 and repeat.json()['created'] == 0, repeat.text


@pytest.mark.parametrize('second_day', [28, 29])
@pytest.mark.parametrize('cooldown_hours', [0, 6])
@pytest.mark.parametrize('max_daily', [1, 10])
def test_delayed_alert_must_respect_target_daily_cap_at_send_time(signals_client, db_session, monkeypatch,
        second_day, cooldown_hours, max_daily):
    from app.services import market_signal_sources as sources
    from app.services.market_signal_delivery import prepare_notification
    now = [datetime(2026, 10, 27, 9, 10)]
    monkeypatch.setattr(sources, 'utcnow', lambda: now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule, evidence = make_rule_and_evidence(signals_client, mode='trial', window_days=1,
        max_daily=max_daily, max_age_hours=168, cooldown_hours=cooldown_hours)
    seed_assessment(db_session, rule['id'], evidence['id'], 'old')
    first = signals_client.post('/market-signals/run', json={'analyze': False})
    assert first.status_code == 200 and first.json()['created'] == 1, first.text
    # Old outbox stays pending, without contacting any transport.
    now[0] = datetime(2026, 10, second_day, 9, 10)
    fresh = signals_client.post('/market-signals/evidence', json={
        'title': '验收银行又一独立订单正式实施', 'text': '验收银行获得新增订单100亿元，项目即日起执行。',
        'source_url': 'https://example.com/new-event', 'published_at': now[0].isoformat(),
        'symbol_codes': ['000001']}).json()
    seed_assessment(db_session, rule['id'], fresh['id'], 'new')
    second = signals_client.post('/market-signals/run', json={'analyze': False})
    assert second.status_code == 200 and second.json()['created'] == 1, second.text
    now[0] = datetime(2026, 10, 29, 9, 10)
    boxes = list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    assert len(boxes) == 2
    allowed = [prepare_notification(db_session, box) for box in boxes]
    predictions = list(db_session.scalars(select(MarketSignalPrediction).order_by(MarketSignalPrediction.id)))
    observe('delayed_per_target_budget', {'configured_max_daily': max_daily, 'actual_transport_calls': 0,
        'send_preflight_allowed': allowed, 'budget_days': [json.loads(p.snapshot_json)['budget_day'] for p in predictions],
        'delivery_statuses': [p.delivery_status for p in predictions], 'target_keys': [p.target_key for p in predictions]})
    expected = 1 if max_daily == 1 or cooldown_hours else 2
    assert sum(allowed) == expected, 'Send-time reservations did not enforce the independent target limits'
    if max_daily == 10 and cooldown_hours:
        suppressed = next(p for p in predictions if p.delivery_status == 'suppressed')
        assert '冷却' in json.loads(suppressed.outcome_json)['reason']
    # Re-checking an already allowed outbox must not consume an extra slot.
    for box, was_allowed in zip(boxes, allowed):
        if was_allowed:
            assert prepare_notification(db_session, box)


def test_same_event_multichannel_is_one_target_budget_reservation(signals_client, db_session, monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.market_signal_delivery import prepare_notification
    from app.models.notification import NotificationChannel
    now = datetime(2026, 10, 27, 9, 10)
    monkeypatch.setattr(sources, 'utcnow', lambda: now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    channels = [NotificationChannel(name=f'Local test {number}', channel_type='in_app', enabled=True, status='enabled')
                for number in range(2)]
    db_session.add_all(channels)
    db_session.commit()
    rule, evidence = make_rule_and_evidence(signals_client, mode='trial', window_days=1,
        max_daily=1, channel_ids=[channel.id for channel in channels])
    seed_assessment(db_session, rule['id'], evidence['id'], 'multichannel')
    response = signals_client.post('/market-signals/run', json={'analyze': False})
    assert response.status_code == 200 and response.json()['created'] == 1, response.text
    boxes = list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    assert len(boxes) == 2
    assert [prepare_notification(db_session, box) for box in boxes] == [True, True]


def test_validation_get_must_not_mutate_portfolio(db_session, monkeypatch):
    import app.main as main
    from app.models.portfolio import Portfolio
    row = Portfolio(name='验收模式遗留组合', account_type='sim', total_capital=100000,
                    investable_ratio=0.8, cash_reserve_ratio=0.2, is_default=0, is_test=0)
    db_session.add(row)
    db_session.commit()
    row_id = row.id
    monkeypatch.setenv('DATABASE_URL', db_session.get_bind().url.render_as_string(hide_password=False))
    monkeypatch.setattr(main.settings, 'APP_VALIDATION_MODE', True, raising=False)
    monkeypatch.setattr(main.settings, 'AUTO_UNIVERSE_SYNC_ON_STARTUP', False)
    with TestClient(main.app) as client:
        response = client.get('/api/v1/portfolios?include_test=true')
    assert response.status_code == 200, response.text
    db_session.expire_all()
    after = db_session.get(Portfolio, row_id)
    observe('validation_get_write', {'response_status': response.status_code,
        'validation_header': response.headers.get('X-App-Validation-Mode'),
        'is_test_before': 0, 'is_test_after': after.is_test})
    assert after.is_test == 0, 'Read request mutated portfolio metadata in validation-only mode'


@pytest.mark.parametrize('write_kind', ['orm', 'bulk', 'raw'])
def test_validation_read_boundary_rejects_unexpected_database_writes(db_session, monkeypatch, write_kind):
    from fastapi import FastAPI
    from sqlalchemy import text, update
    from app.models.portfolio import Portfolio
    import app.main as main
    row = Portfolio(name='Read only fixture', account_type='sim', total_capital=100000,
                    investable_ratio=0.8, cash_reserve_ratio=0.2, is_test=0)
    db_session.add(row)
    db_session.commit()
    row_id = row.id
    monkeypatch.setattr(main.settings, 'APP_VALIDATION_MODE', True)
    probe_app = FastAPI()
    probe_app.middleware('http')(main.validation_runtime_guard)
    # Model a regression inside an otherwise permitted read endpoint.
    @probe_app.get('/api/v1/market-signals/evidence')
    def accidental_write():
        if write_kind == 'orm':
            db_session.get(Portfolio, row_id).is_test = 1
        elif write_kind == 'bulk':
            db_session.execute(update(Portfolio).where(Portfolio.id == row_id).values(is_test=1))
        else:
            db_session.execute(text('UPDATE portfolios SET is_test=1 WHERE id=:id'), {'id': row_id})
        db_session.commit()
        return {'unexpected': True}
    with TestClient(probe_app) as client:
        response = client.get('/api/v1/market-signals/evidence')
        assert response.status_code == 409, response.text
    db_session.rollback()
    assert db_session.get(Portfolio, row_id).is_test == 0


@pytest.mark.parametrize('future_score', [False, True])
def test_validation_symbol_detail_never_repairs_scores_or_trade_setups(db_session, monkeypatch, future_score):
    from tests.test_whitebox_universe_bar_fallback import _seed_universe_only_symbol
    from app.models.portfolio import Portfolio
    from app.models.score import Score
    from app.models.trade_setup import TradeSetup
    import app.main as main
    symbol, start = _seed_universe_only_symbol(db_session, code='588299')
    portfolio = Portfolio(name='Read only account', account_type='sim', total_capital=100000,
                          investable_ratio=0.8, cash_reserve_ratio=0.2, is_default=1)
    db_session.add(portfolio)
    db_session.add(Score(symbol_id=symbol.id, trade_date=start+timedelta(days=90 if future_score else 79),
        quality_score=78, quality_grade='B', timing_score=72, stage='start', action='open',
        priority_score=75, calc_batch_id='readonly-fixture'))
    db_session.commit()
    before = (db_session.query(Score).count(), db_session.query(TradeSetup).count())
    monkeypatch.setenv('DATABASE_URL', db_session.get_bind().url.render_as_string(hide_password=False))
    monkeypatch.setattr(main.settings, 'APP_VALIDATION_MODE', True)
    with TestClient(main.app) as client:
        response = client.get('/api/v1/dashboard/symbol-detail', params={'symbol_id': symbol.id, 'portfolio_id': portfolio.id})
        assert response.status_code == 200, response.text
        assert client.get('/api/v1/system/export/unknown').status_code == 409
    db_session.expire_all()
    assert (db_session.query(Score).count(), db_session.query(TradeSetup).count()) == before
