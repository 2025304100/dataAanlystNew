"""Isolated review probes; no model, scheduler or notification transport."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Barrier
import json

import pytest
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_market_signals_api import signals_client, seed_signal_prices, make_rule_and_evidence
from tests.test_market_signal_priority_regressions import (
    no_real_network, supplier_clock, enable_without_starting_scheduler, seed_assessment,
)
from app.models.market_signal import MarketSignalPrediction
from app.models.notification import NotificationOutbox, NotificationDelivery
from app.services.market_signal_delivery import prepare_notification, record_delivery

pytestmark = pytest.mark.whitebox
OUTPUT = Path(__file__).resolve().parents[2]/'observations.json'


def observe(key, value):
    data=json.loads(OUTPUT.read_text(encoding='utf-8')) if OUTPUT.exists() else {}
    data[key]=value
    OUTPUT.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


def add_event(client, now, number):
    response=client.post('/market-signals/evidence',json={'title':f'验收银行新增独立订单第{number}项',
        'text':'验收银行获得新增订单100亿元，项目即日起执行。',
        'source_url':f'https://example.com/review/{number}','published_at':now.isoformat(),
        'symbol_codes':['000001']})
    assert response.status_code==201,response.text
    return response.json()


def run(client):
    response=client.post('/market-signals/run',json={'analyze':False})
    assert response.status_code==200,response.text
    return response.json()


def receipt_only(db, box, now):
    """Seed an external success receipt; never invoke a notification adapter."""
    box.status='sent'
    box.sent_at=now
    db.commit()
    record_delivery(db,box)


@pytest.mark.parametrize('cross_midnight',[False,True])
def test_actual_receipt_date_must_occupy_delivery_day_budget(signals_client,db_session,monkeypatch,cross_midnight):
    from app.services import market_signal_sources as sources
    now=[datetime(2026,10,27,15,59,50)]  # China 23:59:50.
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,
        max_daily=1,cooldown_hours=0,max_age_hours=168)
    seed_assessment(db_session,rule['id'],evidence['id'],'receipt-first')
    assert run(signals_client)['created']==1
    first=db_session.scalar(select(NotificationOutbox).order_by(NotificationOutbox.id))
    assert prepare_notification(db_session,first)
    now[0]=datetime(2026,10,27,16,0,10) if cross_midnight else datetime(2026,10,27,15,59,55)
    receipt_only(db_session,first,now[0])
    prediction=db_session.scalar(select(MarketSignalPrediction).order_by(MarketSignalPrediction.id))
    snapshot=json.loads(prediction.snapshot_json)
    expected_day=(now[0]+timedelta(hours=8)).date().isoformat()
    now[0]+=timedelta(seconds=1)
    second=add_event(signals_client,now[0],'receipt-second')
    seed_assessment(db_session,rule['id'],second['id'],'receipt-second')
    generated=run(signals_client)['created']
    boxes=list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    allowed=prepare_notification(db_session,boxes[-1]) if len(boxes)>1 else None
    observe(f'actual_delivery_day_{cross_midnight}',{'expected_day':expected_day,
        'stored_budget_day':snapshot['budget_day'],'second_created':generated,'second_preflight':allowed,
        'actual_transport_calls':0})
    assert (snapshot['budget_day'],generated)==(expected_day,0), 'Actual delivery day lost its quota reservation'


@pytest.mark.parametrize('prior_mode',['shadow','trial'])
def test_shadow_history_must_not_suppress_first_live_event(signals_client,db_session,monkeypatch,prior_mode):
    from app.services import market_signal_sources as sources
    now=[datetime(2026,10,27,9,10)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode=prior_mode,window_days=1,max_daily=10,cooldown_hours=6)
    seed_assessment(db_session,rule['id'],evidence['id'],'prior-mode')
    assert run(signals_client)['created']==1
    if prior_mode=='trial':
        box=db_session.scalar(select(NotificationOutbox))
        assert prepare_notification(db_session,box)
        receipt_only(db_session,box,now[0])
    now[0]+=timedelta(hours=1)
    patch=signals_client.put(f"/market-signals/rules/{rule['id']}",json={'mode':'trial'})
    assert patch.status_code==200,patch.text
    evidence=add_event(signals_client,now[0],'after-mode-switch')
    assessment=seed_assessment(db_session,rule['id'],evidence['id'],'after-mode-switch')
    assessment.rule_revision=patch.json()['revision']
    db_session.commit()
    generated=run(signals_client)['created']
    db_session.refresh(assessment)
    observe(f'shadow_cooldown_{prior_mode}',{'created':generated,'reason':assessment.decision_reason,
        'expected_created':1 if prior_mode=='shadow' else 0,'actual_transport_calls':0})
    assert generated==(1 if prior_mode=='shadow' else 0), 'A shadow observation consumed the live cooldown'


@pytest.mark.parametrize('same_event',[False,True])
def test_cancelled_prediction_must_not_be_revived_by_queue_merge(signals_client,db_session,monkeypatch,same_event):
    from app.services import market_signal_sources as sources
    now=[datetime(2026,10,27,9,10)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=10,cooldown_hours=0)
    seed_assessment(db_session,rule['id'],evidence['id'],'shared-event')
    assert run(signals_client)['created']==1
    old_id=db_session.scalar(select(MarketSignalPrediction.id))
    off=signals_client.put('/market-signals/settings',json={'enabled':False})
    assert off.status_code==200,off.text
    db_session.expire_all()
    assert db_session.get(MarketSignalPrediction,old_id).delivery_status=='suppressed'
    on=signals_client.put('/market-signals/settings',json={'enabled':True})
    assert on.status_code==200,on.text
    second_rule=signals_client.post('/market-signals/rules',json={'name':'第二个独立窗口','target_type':'symbol',
        'target_key':'000001','mode':'trial','window_days':3,'max_daily':10,'cooldown_hours':0,'adaptive_threshold':False})
    assert second_rule.status_code==201,second_rule.text
    seed_assessment(db_session,second_rule.json()['id'],evidence['id'],'shared-event' if same_event else 'different-event')
    assert run(signals_client)['created']==1
    db_session.expire_all()
    old=db_session.get(MarketSignalPrediction,old_id)
    boxes=list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    observe(f'cancelled_merge_{same_event}',{'old_status_after_off':'suppressed','old_status_after_merge':old.delivery_status,
        'outbox_payload_ids':[json.loads(box.payload_json)['prediction_ids'] for box in boxes],
        'delivery_rows':db_session.query(NotificationDelivery).count(),'actual_transport_calls':0})
    assert old.delivery_status=='suppressed', 'Queue append revived a cancellation from the global off action'


@pytest.mark.parametrize('second_minute',[10,11])
def test_new_digest_event_after_completed_slot_must_not_be_silently_dropped(signals_client,db_session,monkeypatch,second_minute):
    from app.services import market_signal_sources as sources
    now=[datetime(2026,10,27,8,9,50)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=10,
        cooldown_hours=0,delivery_mode='digest',digest_time='16:10')
    seed_assessment(db_session,rule['id'],evidence['id'],'digest-first')
    assert run(signals_client)['created']==1
    now[0]=datetime(2026,10,27,8,10,5)
    run(signals_client)
    first=db_session.scalar(select(NotificationOutbox))
    assert first is not None and prepare_notification(db_session,first)
    receipt_only(db_session,first,now[0])
    now[0]=datetime(2026,10,27,8,second_minute,30)
    evidence=add_event(signals_client,now[0],'digest-second')
    seed_assessment(db_session,rule['id'],evidence['id'],'digest-second')
    assert run(signals_client)['created']==1
    row=db_session.scalar(select(MarketSignalPrediction).order_by(MarketSignalPrediction.id.desc()))
    observe(f'digest_same_slot_{second_minute}',{'new_event_status':row.delivery_status,
        'new_event_reason':json.loads(row.outcome_json).get('reason'),'digest_due_at':json.loads(row.snapshot_json)['digest_due_at'],
        'actual_transport_calls':0})
    assert row.delivery_status in ('queued','waiting_digest'), 'New independent event was discarded as an already-delivered digest'


@pytest.mark.parametrize('expired_probe',[False,True])
def test_late_probe_result_must_not_clear_a_new_probe_lease(supplier_clock,expired_probe):
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.akshare_guard import SupplierCoolingDown
    key='lease-review'
    def fail():
        raise requests.HTTPError('429 Too Many Requests')
    for _ in range(2):
        with pytest.raises(requests.HTTPError):
            call_akshare_with_retry(fail,api_key=key,max_attempts=1)
    supplier_clock.value=241
    entered_a,release_a,entered_b,release_b=Event(),Event(),Event(),Event()
    def probe_a():
        entered_a.set()
        assert release_a.wait(5)
        return 'old success'
    def probe_b():
        entered_b.set()
        assert release_b.wait(5)
        return 'new success'
    third_calls=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a=pool.submit(call_akshare_with_retry,probe_a,api_key=key,max_attempts=1)
        future_b=None
        try:
            assert entered_a.wait(5)
            if expired_probe:
                supplier_clock.value+=122
                future_b=pool.submit(call_akshare_with_retry,probe_b,api_key=key,max_attempts=1)
                assert entered_b.wait(5)
                release_a.set()
                assert future_a.result(timeout=5)=='old success'
            blocked=False
            try:
                call_akshare_with_retry(lambda:third_calls.append('entered'),api_key=key,max_attempts=1)
            except SupplierCoolingDown:
                blocked=True
            observe(f'probe_lease_{expired_probe}',{'third_blocked':blocked,'third_callback_calls':len(third_calls),
                'new_probe_still_in_flight':expired_probe and not release_b.is_set(),'real_supplier_calls':0})
        finally:
            release_a.set()
            release_b.set()
            future_a.result(timeout=5)
            if future_b is not None:
                future_b.result(timeout=5)
    assert blocked, 'Retired probe cleared the newer probe ownership and let another request enter'


def test_atomic_claim_allows_only_one_worker_without_invoking_transport(signals_client,db_session,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    now=datetime(2026,10,27,9,10)
    monkeypatch.setattr(sources,'utcnow',lambda:now)
    monkeypatch.setattr(outbox,'_now_utc',lambda:now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1)
    seed_assessment(db_session,rule['id'],evidence['id'],'claim')
    assert run(signals_client)['created']==1
    box_id=db_session.scalar(select(NotificationOutbox.id))
    engine=db_session.get_bind()
    gate=Barrier(2)
    def claim(_):
        with Session(engine) as db:
            candidates=outbox.get_pending_outbox(db)
            assert any(box.id==box_id for box in candidates)
            gate.wait(timeout=5)
            return outbox.mark_sending(db,outbox_id=box_id) is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,range(2)))
    observe('atomic_claim_control',{'claim_results':results,'actual_transport_calls':0})
    assert results.count(True)==1 and results.count(False)==1
