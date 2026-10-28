"""Red review probes, isolated DB and blocked network; not deployment acceptance."""
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
OUTPUT = Path(__file__).resolve().parents[2] / 'observations.json'


def observe(key, value):
    data = json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {}
    data[key] = value
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


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


@pytest.mark.parametrize('expired_count', [499, 500])
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


def test_delayed_alert_must_respect_target_daily_cap_at_send_time(signals_client, db_session, monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.market_signal_delivery import prepare_notification
    now = [datetime(2026, 10, 27, 9, 10)]
    monkeypatch.setattr(sources, 'utcnow', lambda: now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule, evidence = make_rule_and_evidence(signals_client, mode='trial', window_days=1,
        max_daily=1, max_age_hours=168, cooldown_hours=6)
    seed_assessment(db_session, rule['id'], evidence['id'], 'old')
    first = signals_client.post('/market-signals/run', json={'analyze': False})
    assert first.status_code == 200 and first.json()['created'] == 1, first.text
    # Old outbox stays pending, without contacting any transport.
    now[0] = datetime(2026, 10, 29, 9, 10)
    fresh = signals_client.post('/market-signals/evidence', json={
        'title': '验收银行又一独立订单正式实施', 'text': '验收银行获得新增订单100亿元，项目即日起执行。',
        'source_url': 'https://example.com/new-event', 'published_at': now[0].isoformat(),
        'symbol_codes': ['000001']}).json()
    seed_assessment(db_session, rule['id'], fresh['id'], 'new')
    second = signals_client.post('/market-signals/run', json={'analyze': False})
    assert second.status_code == 200 and second.json()['created'] == 1, second.text
    boxes = list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    assert len(boxes) == 2
    allowed = [prepare_notification(db_session, box) for box in boxes]
    predictions = list(db_session.scalars(select(MarketSignalPrediction).order_by(MarketSignalPrediction.id)))
    observe('delayed_per_target_budget', {'configured_max_daily': 1, 'actual_transport_calls': 0,
        'send_preflight_allowed': allowed, 'budget_days': [json.loads(p.snapshot_json)['budget_day'] for p in predictions],
        'delivery_statuses': [p.delivery_status for p in predictions], 'target_keys': [p.target_key for p in predictions]})
    assert sum(allowed) == 1, 'Two distinct alerts for the same target passed on one day despite max_daily=1'


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
