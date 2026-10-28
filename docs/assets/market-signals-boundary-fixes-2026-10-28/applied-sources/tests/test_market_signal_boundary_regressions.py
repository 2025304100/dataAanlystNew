"""Boundary regressions with fixture receipts; no model or transport calls."""
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
def observe(key, value):
    print(json.dumps({key:value},ensure_ascii=False,default=str))


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
@pytest.mark.parametrize('cap_scope',['target','global'])
def test_actual_receipt_date_must_occupy_delivery_day_budget(signals_client,db_session,monkeypatch,cross_midnight,cap_scope):
    from app.services import market_signal_sources as sources
    now=[datetime(2026,10,27,15,59,50)]  # China 23:59:50.
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,
        max_daily=1 if cap_scope=='target' else 10,cooldown_hours=0,max_age_hours=168)
    if cap_scope=='global':
        response=signals_client.put('/market-signals/settings',json={'global_daily_limit':1})
        assert response.status_code==200,response.text
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


@pytest.mark.parametrize('prepared',[False,True])
def test_only_admitted_inflight_transport_reserves_the_new_day(signals_client,db_session,monkeypatch,prepared):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    now=[datetime(2026,10,27,15,59,50)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    monkeypatch.setattr(outbox,'_now_utc',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=1,
        cooldown_hours=0,max_age_hours=168)
    seed_assessment(db_session,rule['id'],evidence['id'],'inflight-first')
    assert run(signals_client)['created']==1
    box_id=db_session.scalar(select(NotificationOutbox.id))
    box=outbox.mark_sending(db_session,outbox_id=box_id)
    assert box is not None
    if prepared:
        assert prepare_notification(db_session,box)
    now[0]=datetime(2026,10,27,16,0,5)
    fresh=add_event(signals_client,now[0],'inflight-next')
    seed_assessment(db_session,rule['id'],fresh['id'],'inflight-next')
    assert run(signals_client)['created']==(0 if prepared else 1)
    if not prepared:
        assert not prepare_notification(db_session,box)


def test_receipt_reconciliation_keeps_earliest_delivery_and_is_idempotent(signals_client,db_session,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    from app.models.notification import NotificationChannel
    now=[datetime(2026,10,27,15,59,50)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    monkeypatch.setattr(outbox,'_now_utc',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    channels=[NotificationChannel(name=f'Fixture {n}',channel_type='in_app',enabled=True,status='enabled') for n in range(2)]
    db_session.add_all(channels)
    db_session.commit()
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=1,cooldown_hours=0,
        channel_ids=[channel.id for channel in channels])
    seed_assessment(db_session,rule['id'],evidence['id'],'two-receipts')
    assert run(signals_client)['created']==1
    boxes=list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    for box in boxes:
        assert outbox.mark_sending(db_session,outbox_id=box.id) is not None
        assert prepare_notification(db_session,box)
    now[0]=datetime(2026,10,27,16,0,10)
    receipt_only(db_session,boxes[0],now[0])
    # Earlier channel success is durable, but its analytics callback was lost.
    boxes[1].status='sent'
    boxes[1].sent_at=datetime(2026,10,27,15,59,55)
    db_session.commit()
    assert run(signals_client)['created']==0
    for _ in range(2):
        record_delivery(db_session,boxes[0])
        record_delivery(db_session,boxes[1])
    value=signals_client.get('/market-signals/predictions').json()['items'][0]
    assert value['sent_at']=='2026-10-27T15:59:55'
    assert value['snapshot']['budget_day']=='2026-10-27'
    assert value['snapshot']['delivery_day']=='2026-10-27'
    assert value['delivery_status']=='sent'
    assert db_session.query(NotificationDelivery).count()==0


@pytest.mark.parametrize('prior_mode',['shadow','trial'])
@pytest.mark.parametrize('next_mode',['shadow','trial'])
def test_shadow_history_must_not_suppress_first_live_event(signals_client,db_session,monkeypatch,prior_mode,next_mode):
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
    patch=signals_client.put(f"/market-signals/rules/{rule['id']}",json={'mode':next_mode})
    assert patch.status_code==200,patch.text
    evidence=add_event(signals_client,now[0],'after-mode-switch')
    assessment=seed_assessment(db_session,rule['id'],evidence['id'],'after-mode-switch')
    assessment.rule_revision=patch.json()['revision']
    db_session.commit()
    generated=run(signals_client)['created']
    db_session.refresh(assessment)
    observe(f'shadow_cooldown_{prior_mode}',{'created':generated,'reason':assessment.decision_reason,
        'expected_created':int(prior_mode!=next_mode),'actual_transport_calls':0})
    assert generated==int(prior_mode!=next_mode), 'Cooldown leaked across shadow/trial modes'


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
    if same_event:
        assert old_id not in json.loads(boxes[0].payload_json)['prediction_ids']


def test_global_cancel_and_queue_writes_share_a_lock(signals_client,db_session,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.market_event_collection_lock import market_event_write_lock
    now=datetime(2026,10,27,9,10)
    monkeypatch.setattr(sources,'utcnow',lambda:now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1)
    seed_assessment(db_session,rule['id'],evidence['id'],'lock-cancel')
    with Session(db_session.get_bind()) as writer:
        with market_event_write_lock(writer,namespace='market-signals-engine'):
            rejected=signals_client.put('/market-signals/settings',json={'enabled':False})
            assert rejected.status_code==409,rejected.text
    assert run(signals_client)['created']==1
    cancelled=signals_client.put('/market-signals/settings',json={'enabled':False})
    assert cancelled.status_code==200,cancelled.text
    assert signals_client.get('/market-signals/predictions').json()['items'][0]['delivery_status']=='suppressed'


def test_append_preserves_sent_member_on_other_pending_channel(signals_client,db_session,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.models.notification import NotificationChannel
    now=datetime(2026,10,27,9,10)
    monkeypatch.setattr(sources,'utcnow',lambda:now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    channels=[NotificationChannel(name=f'Merge {n}',channel_type='in_app',enabled=True,status='enabled') for n in range(2)]
    db_session.add_all(channels)
    db_session.commit()
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=10,cooldown_hours=0,
        channel_ids=[channel.id for channel in channels])
    seed_assessment(db_session,rule['id'],evidence['id'],'merge-sent')
    assert run(signals_client)['created']==1
    boxes=list(db_session.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)))
    assert prepare_notification(db_session,boxes[0])
    receipt_only(db_session,boxes[0],now)
    old_id=db_session.scalar(select(MarketSignalPrediction.id))
    second=signals_client.post('/market-signals/rules',json={'name':'Additional window','target_type':'symbol',
        'target_key':'000001','mode':'trial','window_days':3,'max_daily':10,'cooldown_hours':0,
        'adaptive_threshold':False,'channel_ids':[channels[1].id]})
    assert second.status_code==201,second.text
    seed_assessment(db_session,second.json()['id'],evidence['id'],'merge-sent')
    assert run(signals_client)['created']==1
    db_session.expire_all()
    assert db_session.get(MarketSignalPrediction,old_id).delivery_status=='sent'
    assert prepare_notification(db_session,boxes[1])
    assert db_session.get(MarketSignalPrediction,old_id).delivery_status=='sent'


def test_manual_failed_retry_survives_append_without_implicit_revival(signals_client,db_session,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    now=datetime(2026,10,27,9,10)
    monkeypatch.setattr(sources,'utcnow',lambda:now)
    monkeypatch.setattr(outbox,'_now_utc',lambda:now)
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=10,cooldown_hours=0)
    seed_assessment(db_session,rule['id'],evidence['id'],'retry-member')
    assert run(signals_client)['created']==1
    box=db_session.scalar(select(NotificationOutbox))
    box.max_attempts=1
    db_session.commit()
    assert outbox.mark_sending(db_session,outbox_id=box.id)
    assert prepare_notification(db_session,box)
    outbox.mark_failed(db_session,outbox_id=box.id,error_code='fixture_503',error_message='fixture unavailable',status_code=503)
    run(signals_client)
    old_id=db_session.scalar(select(MarketSignalPrediction.id))
    assert db_session.get(MarketSignalPrediction,old_id).delivery_status=='failed'
    assert outbox.manual_retry(db_session,outbox_id=box.id) is not None
    second=signals_client.post('/market-signals/rules',json={'name':'Retry extra window','target_type':'symbol',
        'target_key':'000001','mode':'trial','window_days':3,'max_daily':10,'cooldown_hours':0,'adaptive_threshold':False})
    assert second.status_code==201,second.text
    seed_assessment(db_session,second.json()['id'],evidence['id'],'retry-member')
    assert run(signals_client)['created']==1
    db_session.expire_all()
    assert db_session.get(MarketSignalPrediction,old_id).delivery_status=='failed'
    assert prepare_notification(db_session,box)
    assert db_session.get(MarketSignalPrediction,old_id).delivery_status=='queued'
    assert box.attempt_count==1


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
    assert row.delivery_status=='waiting_digest'
    assert json.loads(row.snapshot_json)['budget_day']=='2026-10-28'


@pytest.mark.parametrize('prior_status',['pending','sending','sent','dead_letter'])
def test_digest_exact_deadline_handles_claimed_and_closed_batches(signals_client,db_session,monkeypatch,prior_status):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    now=[datetime(2026,10,27,8,9,50)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    monkeypatch.setattr(outbox,'_now_utc',lambda:now[0])
    seed_signal_prices(db_session)
    enable_without_starting_scheduler(db_session)
    rule,evidence=make_rule_and_evidence(signals_client,mode='trial',window_days=1,max_daily=10,cooldown_hours=0,
        delivery_mode='digest',digest_time='16:10')
    seed_assessment(db_session,rule['id'],evidence['id'],'slot-initial')
    assert run(signals_client)['created']==1
    now[0]=datetime(2026,10,27,8,10,0)
    run(signals_client)
    box=db_session.scalar(select(NotificationOutbox))
    if prior_status!='pending':
        box.max_attempts=1
        db_session.commit()
        assert outbox.mark_sending(db_session,outbox_id=box.id)
        assert prepare_notification(db_session,box)
        if prior_status=='sent':
            receipt_only(db_session,box,now[0])
        elif prior_status=='dead_letter':
            outbox.mark_failed(db_session,outbox_id=box.id,error_code='fixture',error_message='fixture failure',status_code=503)
    second=add_event(signals_client,now[0],'slot-independent')
    seed_assessment(db_session,rule['id'],second['id'],'slot-independent')
    assert run(signals_client)['created']==1
    latest=signals_client.get('/market-signals/predictions').json()['items'][0]
    if prior_status=='pending':
        assert latest['delivery_status']=='queued'
        assert len(json.loads(box.payload_json)['prediction_ids'])==2
    else:
        assert latest['delivery_status']=='waiting_digest'
        assert latest['snapshot']['budget_day']=='2026-10-28'
        assert len(json.loads(box.payload_json)['prediction_ids'])==1


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


@pytest.mark.parametrize('old_result',['success','failure'])
@pytest.mark.parametrize('new_result',['pending','failure'])
def test_retired_probe_results_do_not_change_new_owner_or_new_cooldown(supplier_clock,old_result,new_result):
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services import akshare_guard as guard
    key='fenced-probe'
    def fail():
        raise requests.HTTPError('429 Too Many Requests')
    for _ in range(2):
        with pytest.raises(requests.HTTPError):
            call_akshare_with_retry(fail,api_key=key,max_attempts=1)
    supplier_clock.value=241
    a_enter,a_release,b_enter,b_release=Event(),Event(),Event(),Event()
    def old():
        a_enter.set()
        assert a_release.wait(5)
        if old_result=='failure':
            raise requests.HTTPError('429 Old probe failure')
        return 'old success'
    def new():
        b_enter.set()
        assert b_release.wait(5)
        if new_result=='failure':
            raise requests.HTTPError('429 New probe failure')
        return 'new success'
    def finish(future,kind):
        if kind=='failure':
            with pytest.raises(requests.HTTPError):
                future.result(timeout=5)
        else:
            future.result(timeout=5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(call_akshare_with_retry,old,api_key=key,max_attempts=1)
        second=None
        try:
            assert a_enter.wait(5)
            supplier_clock.value+=122
            second=pool.submit(call_akshare_with_retry,new,api_key=key,max_attempts=1)
            assert b_enter.wait(5)
            if new_result=='failure':
                b_release.set()
                finish(second,'failure')
            before=guard.snapshot()['breakers'][key]
            a_release.set()
            finish(first,old_result)
            after=guard.snapshot()['breakers'][key]
            assert after['generation']==before['generation']
            assert after['probe_in_flight']==before['probe_in_flight']
            assert after['cooldown_remaining_s']==before['cooldown_remaining_s']
            assert after['stale_results_ignored']==before['stale_results_ignored']+1
            with pytest.raises(guard.SupplierCoolingDown):
                call_akshare_with_retry(lambda:'must not enter',api_key=key,max_attempts=1)
        finally:
            a_release.set()
            b_release.set()
            finish(first,old_result)
            if second is not None:
                finish(second,'failure' if new_result=='failure' else 'success')


def test_retired_release_and_reset_cannot_release_new_generation(supplier_clock):
    from app.services import akshare_guard as guard
    key='release-fence'
    for _ in range(2):
        guard.record_failure(key,severe=True,error='fixture')
    supplier_clock.value=241
    retired=guard.ensure_available(key)
    supplier_clock.value+=122
    current=guard.ensure_available(key)
    guard.release_probe(key,permit=retired)
    with pytest.raises(guard.SupplierCoolingDown):
        guard.ensure_available(key)
    guard.record_success(key,permit=current)
    # Reset does not recycle generation identities held by old callers.
    guard.reset_all()
    for _ in range(2):
        guard.record_failure(key,severe=True,error='fixture')
    supplier_clock.value+=241
    newest=guard.ensure_available(key)
    guard.record_success(key,permit=current)
    with pytest.raises(guard.SupplierCoolingDown):
        guard.ensure_available(key)
    guard.record_success(key,permit=newest)
    assert not guard.ensure_available(key)


@pytest.mark.parametrize('invalid_attempts',[0,-1,2.5])
def test_invalid_retry_budget_never_takes_a_probe_or_calls_supplier(supplier_clock,invalid_attempts):
    from app.services.akshare_utils import call_akshare_with_retry
    calls=[]
    with pytest.raises(ValueError):
        call_akshare_with_retry(lambda:calls.append('bad'),api_key='invalid-attempts',max_attempts=invalid_attempts)
    assert not calls
    assert call_akshare_with_retry(lambda:'ok',api_key='invalid-attempts',max_attempts=1)=='ok'


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
