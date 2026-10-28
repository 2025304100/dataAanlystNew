"""Offline review probes. Every dispatcher adapter is a local callable substitute.

Red assertions preserve defects; they are deliberately not production regressions.
No socket is allowed and no real adapter's send_message method is executed.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event, Lock
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel, NotificationDelivery, NotificationOutbox
from app.models.market_signal import MarketSignalPrediction
from app.services.notifications import dispatcher as dispatch, outbox as service
from app.services.notifications.base import SendResult
from tests.test_market_signal_priority_regressions import (
    no_real_network, enable_without_starting_scheduler, seed_assessment,
)
from tests.test_market_signals_api import signals_client, seed_signal_prices, make_rule_and_evidence

pytestmark = pytest.mark.whitebox


def observe(name, **value):
    print('OBSERVATION ' + json.dumps({'probe': name, **value}, ensure_ascii=False, default=str))


class AdapterSubstitute:
    def __init__(self, action=None):
        self.calls = []
        self.lock = Lock()
        self.action = action

    def send_message(self, **kwargs):
        with self.lock:
            self.calls.append(copy.deepcopy(kwargs))
            index = len(self.calls)
        return self.action(index, kwargs) if self.action else SendResult(success=True, status_code=200)

    def normalize_error(self, exc):
        return type(exc).__name__, str(exc)


def wire(monkeypatch, engine, substitute):
    monkeypatch.setattr(dispatch, 'SessionLocal', lambda: Session(engine, autoflush=False))
    monkeypatch.setattr(dispatch, 'get_adapter', lambda kind: substitute)


def seed(db, state='pending', channel_type='in_app'):
    channel = NotificationChannel(name='Offline queue audit', channel_type=channel_type,
                                  enabled=True, status='enabled')
    db.add(channel)
    db.flush()
    box = NotificationOutbox(event_key='offline-review', source_type='system',
        event_type='offline_review', severity='info', channel_id=channel.id,
        payload_json=json.dumps({'title': 'Offline probe', 'body': 'Never transported'}),
        status=state, attempt_count=1 if state=='dead_letter' else 0, max_attempts=5,
        next_retry_at=datetime(2020, 1, 1))
    db.add(box)
    db.commit()
    return box.id


@pytest.mark.parametrize('worker_state', ['sending', 'sent'])
def test_stale_manual_retry_must_not_revive_another_workers_claim_or_success(db_session, monkeypatch, worker_state):
    # MySQL DATETIME(0) rounds microseconds; freeze a whole-second clock so the
    # probe tests ownership rather than a subsecond scheduling delay.
    fixed_now = datetime(2026, 10, 28, 4, 0, 0)
    monkeypatch.setattr(service, '_now_utc', lambda: fixed_now)
    engine = db_session.get_bind()
    box_id = seed(db_session, state='dead_letter')
    entered, release = Event(), Event()
    def action(index, kwargs):
        if index == 1 and worker_state == 'sending':
            entered.set()
            assert release.wait(8)
        return SendResult(success=True, status_code=200)
    fake = AdapterSubstitute(action)
    wire(monkeypatch, engine, fake)
    with Session(engine) as delayed_request, Session(engine) as first_request:
        stale = delayed_request.get(NotificationOutbox, box_id)
        assert stale.status == 'dead_letter'
        assert service.manual_retry(first_request, outbox_id=box_id) is not None
        if worker_state == 'sending':
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(dispatch.Dispatcher().run_once)
                try:
                    assert entered.wait(8)
                    revived = service.manual_retry(delayed_request, outbox_id=box_id)
                    dispatch.Dispatcher().run_once()
                finally:
                    release.set()
                assert first.result(timeout=8) == 1
        else:
            assert dispatch.Dispatcher().run_once() == 1
            revived = service.manual_retry(delayed_request, outbox_id=box_id)
            dispatch.Dispatcher().run_once()
    with Session(engine) as verifier:
        stored = verifier.get(NotificationOutbox, box_id)
        successes = verifier.query(NotificationDelivery).filter_by(outbox_id=box_id, status='success').count()
        observe('manual_retry_' + worker_state, delayed_retry_accepted=revived is not None,
            adapter_substitute_calls=len(fake.calls), success_rows=successes, final_state=stored.status)
    accepted = revived is not None
    assert not accepted and len(fake.calls) == 1, 'A stale failed/dead-letter snapshot revived an owned/sent row'


def test_success_persistence_exception_must_not_become_automatic_transport_retry(db_session, monkeypatch):
    engine = db_session.get_bind()
    box_id = seed(db_session)
    fake = AdapterSubstitute()
    wire(monkeypatch, engine, fake)
    actual_mark_sent = dispatch.mark_sent
    once = [True]
    def unavailable_receipt_store(*args, **kwargs):
        if once[0]:
            once[0] = False
            raise RuntimeError('Injected receipt-store exception after transport success')
        return actual_mark_sent(*args, **kwargs)
    monkeypatch.setattr(dispatch, 'mark_sent', unavailable_receipt_store)
    assert dispatch.Dispatcher().run_once() == 1
    with Session(engine) as verifier:
        row = verifier.get(NotificationOutbox, box_id)
        after_success = row.status
        row.next_retry_at = datetime(2020, 1, 1)  # Advance only the isolated retry clock.
        verifier.commit()
    dispatch.Dispatcher().run_once()
    observe('success_persistence_exception', state_after_transport_success=after_success,
            adapter_substitute_calls=len(fake.calls), fault_injection='before success persistence')
    assert after_success != 'pending' and len(fake.calls) == 1, 'Receipt storage error was treated as transport failure'


def setup_signal(signals_client, db, monkeypatch):
    from app.services import market_signal_sources as sources
    from app.utils.secret_mask import encrypt_config
    now = datetime(2026, 10, 27, 9, 10)
    monkeypatch.setattr(sources, 'utcnow', lambda: now)
    monkeypatch.setattr(service, '_now_utc', lambda: now)
    monkeypatch.setattr(dispatch, '_now_utc_naive', lambda: now)
    seed_signal_prices(db)
    enable_without_starting_scheduler(db)
    channel = NotificationChannel(name='Offline webhook substitute', channel_type='webhook',
        enabled=True, status='enabled', config_encrypted_json=encrypt_config(json.dumps({'url':'http://127.0.0.1:9/never'})))
    db.add(channel)
    db.commit()
    rule, evidence = make_rule_and_evidence(signals_client, mode='trial', window_days=1,
        max_daily=10, cooldown_hours=0, channel_ids=[channel.id])
    seed_assessment(db, rule['id'], evidence['id'], 'shared-fact')
    result = signals_client.post('/market-signals/run', json={'analyze':False})
    assert result.status_code == 200 and result.json()['created'] == 1, result.text
    return rule, evidence, channel.id


def test_accepted_retry_payload_must_remain_immutable_for_its_idempotency_key(signals_client, db_session, monkeypatch):
    rule, evidence, channel_id = setup_signal(signals_client, db_session, monkeypatch)
    effects = {}
    def accepting_receiver(index, kwargs):
        key = kwargs['payload']['_notification_idempotency_key']
        effects.setdefault(key, copy.deepcopy(kwargs['payload']))
        if index == 1:
            return SendResult(success=False, error_code='ReadTimeout', error_message='Injected lost acknowledgement')
        return SendResult(success=True, status_code=200)
    fake = AdapterSubstitute(accepting_receiver)
    engine = db_session.get_bind()
    wire(monkeypatch, engine, fake)
    assert dispatch.Dispatcher().run_once() == 1
    second = signals_client.post('/market-signals/rules', json={'name':'Same fact new window',
        'target_type':'symbol', 'target_key':'000001', 'mode':'trial', 'window_days':3,
        'max_daily':10, 'cooldown_hours':0, 'adaptive_threshold':False, 'channel_ids':[channel_id]})
    assert second.status_code == 201, second.text
    seed_assessment(db_session, second.json()['id'], evidence['id'], 'shared-fact')
    result = signals_client.post('/market-signals/run', json={'analyze':False})
    assert result.status_code == 200 and result.json()['created'] == 1, result.text
    with Session(engine) as db:
        box = db.scalar(select(NotificationOutbox))
        box.next_retry_at = datetime(2020, 1, 1)
        db.commit()
    assert dispatch.Dispatcher().run_once() == 1
    attempts = [item['payload'] for item in fake.calls]
    keys = [item['_notification_idempotency_key'] for item in attempts]
    with Session(engine) as db:
        predictions = [(row.id, row.delivery_status) for row in db.scalars(select(MarketSignalPrediction).order_by(MarketSignalPrediction.id))]
    observe('mutable_payload_same_key', keys=keys, requested_prediction_ids=[p['prediction_ids'] for p in attempts],
        receiver_effect_prediction_ids=[p['prediction_ids'] for p in effects.values()], stored_predictions=predictions,
        actual_transport_calls=0, adapter_substitute_calls=len(fake.calls))
    assert keys[0] != keys[1] or attempts[0] == attempts[1], 'Same idempotency key carried a different message after a lost response'


def test_two_workers_claim_same_pending_row_only_once(db_session, monkeypatch):
    engine = db_session.get_bind()
    box_id = seed(db_session)
    fake = AdapterSubstitute()
    wire(monkeypatch, engine, fake)
    gate = Barrier(2)
    def worker():
        with Session(engine) as db:
            row = db.get(NotificationOutbox, box_id)
            gate.wait(timeout=8)
            return dispatch.Dispatcher()._process_one(db, row)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: worker(), range(2)))
    observe('same_row_atomic_claim', workers=results, adapter_substitute_calls=len(fake.calls))
    assert sorted(results) == [False, True] and len(fake.calls) == 1


@pytest.mark.parametrize('accepted_before_interrupt', [False, True])
def test_process_interruption_leaves_sending_without_automatic_reclaim(db_session, monkeypatch, accepted_before_interrupt):
    engine = db_session.get_bind()
    box_id = seed(db_session)
    accepted = []
    def interrupted(index, kwargs):
        if accepted_before_interrupt:
            accepted.append('fixture-effect')
        raise SystemExit('Simulated process termination inside substituted transport')
    fake = AdapterSubstitute(interrupted)
    wire(monkeypatch, engine, fake)
    with pytest.raises(SystemExit):
        dispatch.Dispatcher().run_once()
    assert dispatch.Dispatcher().run_once() == 0
    with Session(engine) as db:
        assert db.get(NotificationOutbox, box_id).status == 'sending'
    observe('interruption_' + str(accepted_before_interrupt), stub_effects=len(accepted),
            adapter_substitute_calls=len(fake.calls), reclaimed=False)
    assert len(fake.calls) == 1


def test_unchanged_webhook_retry_reuses_key_and_receiver_effect(db_session, monkeypatch):
    engine = db_session.get_bind()
    box_id = seed(db_session, channel_type='webhook')
    effects = set()
    def receive(index, kwargs):
        effects.add(kwargs['payload']['_notification_idempotency_key'])
        return SendResult(success=index > 1, error_code=None if index > 1 else 'ReadTimeout')
    fake = AdapterSubstitute(receive)
    wire(monkeypatch, engine, fake)
    assert dispatch.Dispatcher().run_once() == 1
    with Session(engine) as db:
        row = db.get(NotificationOutbox, box_id)
        row.next_retry_at = datetime(2020, 1, 1)
        db.commit()
    assert dispatch.Dispatcher().run_once() == 1
    observe('unchanged_payload_retry', adapter_substitute_calls=len(fake.calls), receiver_effects=len(effects))
    assert len(effects) == 1 and fake.calls[0]['payload'] == fake.calls[1]['payload']


@pytest.mark.parametrize('state', ['pending', 'sending', 'sent'])
def test_fresh_manual_retry_rejects_non_failed_states(db_session, state):
    box_id = seed(db_session, state=state)
    assert service.manual_retry(db_session, outbox_id=box_id) is None


def test_analytics_callback_failure_cannot_requeue_success(signals_client, db_session, monkeypatch):
    setup_signal(signals_client, db_session, monkeypatch)
    from app.services import market_signal_delivery as delivery
    def callback_error(*args, **kwargs):
        raise RuntimeError('Injected analytics callback error')
    fake = AdapterSubstitute()
    wire(monkeypatch, db_session.get_bind(), fake)
    monkeypatch.setattr(delivery, 'record_delivery', callback_error)
    assert dispatch.Dispatcher().run_once() == 1
    assert dispatch.Dispatcher().run_once() == 0
    with Session(db_session.get_bind()) as db:
        assert db.scalar(select(NotificationOutbox)).status == 'sent'
    observe('analytics_callback_failure', adapter_substitute_calls=len(fake.calls), requeued=False)


def test_two_independent_processes_poll_same_row_and_invoke_substitute_once(db_session, tmp_path):
    seed(db_session)
    barrier = tmp_path / 'process-barrier'
    barrier.mkdir()
    env = dict(os.environ, OFFLINE_QUEUE_URL=db_session.get_bind().url.render_as_string(hide_password=False),
               OFFLINE_QUEUE_BARRIER=str(barrier), DATABASE_URL='sqlite:///:memory:',
               AUTO_UNIVERSE_SYNC_ON_STARTUP='0', AUTO_TRADE_CALENDAR_REFRESH='0')
    processes = []
    try:
        for worker_id in ('a', 'b'):
            processes.append(subprocess.Popen([sys.executable, '-m', 'tests.offline_delivery_review_worker'],
                cwd=Path(__file__).resolve().parents[1], env=dict(env, OFFLINE_WORKER_ID=worker_id),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        deadline = time.monotonic() + 15
        while len(list(barrier.glob('ready-*'))) != 2:
            assert time.monotonic() < deadline, 'Child processes did not reach the queue-read barrier'
            assert not any(p.poll() is not None for p in processes), 'Child exited before synchronization'
            time.sleep(0.02)
        assert [p.read_text() for p in sorted(barrier.glob('ready-*'))] == ['1', '1']
        (barrier / 'go').touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr[-1800:]
            results.append(json.loads(stdout.strip().splitlines()[-1]))
        observe('two_process_atomic_claim', workers=results)
        assert sum(row['processed'] for row in results) == sum(row['adapter_substitute_calls'] for row in results) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()  # Only these newly created, identified test children.
                process.wait(timeout=5)
