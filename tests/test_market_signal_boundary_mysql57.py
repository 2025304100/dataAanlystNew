"""Real MySQL concurrency with local fixture receipts, never transports."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event
import json

from sqlalchemy import event,select
from sqlalchemy.orm import Session
import pytest

from tests.test_market_signal_priority_mysql57 import native
from tests.test_market_signals_api import make_rule_and_evidence
from tests.test_market_signal_boundary_regressions import add_event,run,seed_assessment
from app.models.market_signal import MarketSignalPrediction
from app.models.notification import NotificationOutbox,NotificationDelivery
from app.services.market_signal_delivery import prepare_notification,record_delivery
from app.services.market_event_collection_lock import MarketEventCollectionBusy

pytestmark=pytest.mark.whitebox


def test_mysql_two_claims_across_days_admit_exactly_one(native,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    engine,client=native
    now=[datetime(2026,10,27,9,10)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    monkeypatch.setattr(outbox,'_now_utc',lambda:now[0])
    rule,evidence=make_rule_and_evidence(client,mode='trial',window_days=1,max_daily=1,cooldown_hours=0,max_age_hours=168)
    with Session(engine) as db:
        seed_assessment(db,rule['id'],evidence['id'],'day-one')
    assert run(client)['created']==1
    now[0]=datetime(2026,10,28,9,10)
    evidence=add_event(client,now[0],'day-two')
    with Session(engine) as db:
        seed_assessment(db,rule['id'],evidence['id'],'day-two')
    assert run(client)['created']==1
    now[0]=datetime(2026,10,29,9,10)
    with Session(engine) as db:
        ids=list(db.scalars(select(NotificationOutbox.id).order_by(NotificationOutbox.id)))
    gate=Barrier(2)
    def claim_and_prepare(box_id):
        with Session(engine) as db:
            assert outbox.mark_sending(db,outbox_id=box_id) is not None
            gate.wait(timeout=5)
            try:
                return prepare_notification(db,db.get(NotificationOutbox,box_id))
            except MarketEventCollectionBusy:
                return 'busy'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim_and_prepare,ids))
    for index,value in enumerate(results):
        if value=='busy':
            with Session(engine) as db:
                results[index]=prepare_notification(db,db.get(NotificationOutbox,ids[index]))
    assert results.count(True)==1 and results.count(False)==1,results
    with Session(engine) as db:
        assert db.query(NotificationDelivery).count()==0
        rows=list(db.scalars(select(MarketSignalPrediction)))
        assert sum(row.delivery_status=='queued' and json.loads(row.snapshot_json)['budget_day']=='2026-10-29' for row in rows)==1


def test_mysql_actual_midnight_receipt_and_new_run_share_quota(native,monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.notifications import outbox
    engine,client=native
    now=[datetime(2026,10,27,15,59,50)]
    monkeypatch.setattr(sources,'utcnow',lambda:now[0])
    monkeypatch.setattr(outbox,'_now_utc',lambda:now[0])
    rule,evidence=make_rule_and_evidence(client,mode='trial',window_days=1,max_daily=1,cooldown_hours=0,max_age_hours=168)
    with Session(engine) as db:
        seed_assessment(db,rule['id'],evidence['id'],'midnight-first')
    assert run(client)['created']==1
    with Session(engine) as db:
        box=db.scalar(select(NotificationOutbox))
        box_id=box.id
        assert outbox.mark_sending(db,outbox_id=box_id)
        assert prepare_notification(db,box)
    now[0]=datetime(2026,10,27,16,0,10)
    with Session(engine) as db:
        box=db.get(NotificationOutbox,box_id)
        outbox.mark_sent(db,outbox_id=box.id,delivery=NotificationDelivery(outbox_id=box.id,
            channel_id=box.channel_id,attempt_number=1,status='success',duration_ms=0,
            sent_at=now[0],created_at=now[0]))  # Persistence input; no adapter is called.
    fresh=add_event(client,now[0],'midnight-second')
    with Session(engine) as db:
        seed_assessment(db,rule['id'],fresh['id'],'midnight-second')
    gate=Barrier(2)
    def callback():
        with Session(engine) as db:
            box=db.get(NotificationOutbox,box_id)
            gate.wait(timeout=5)
            try:
                record_delivery(db,box)
                return 'recorded'
            except MarketEventCollectionBusy:
                return 'busy'
    def process():
        gate.wait(timeout=5)
        return client.post('/market-signals/run',json={'analyze':False})
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipt=pool.submit(callback)
        processing=pool.submit(process)
        result=processing.result(timeout=10)
        receipt.result(timeout=10)
    if result.status_code==409:
        result=client.post('/market-signals/run',json={'analyze':False})
    assert result.status_code==200 and result.json()['created']==0,result.text
    with Session(engine) as db:
        row=db.scalar(select(MarketSignalPrediction))
        assert row.delivery_status=='sent'
        assert json.loads(row.snapshot_json)['budget_day']=='2026-10-28'
        assert db.query(NotificationDelivery).count()==1


def test_mysql_cancel_cannot_commit_inside_queue_write(native,monkeypatch):
    from app.services import market_signal_sources as sources
    engine,client=native
    now=datetime(2026,10,27,9,10)
    monkeypatch.setattr(sources,'utcnow',lambda:now)
    rule,evidence=make_rule_and_evidence(client,mode='trial',window_days=1)
    with Session(engine) as db:
        seed_assessment(db,rule['id'],evidence['id'],'cancel-during-write')
    entered,release=Event(),Event()
    def pause_insert(_connection,_cursor,statement,_parameters,_context,_many):
        if statement.lower().replace('`','').startswith('insert into market_signal_predictions'):
            entered.set()
            assert release.wait(5)
    event.listen(engine,'before_cursor_execute',pause_insert)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(client.post,'/market-signals/run',json={'analyze':False})
            try:
                assert entered.wait(5)
                blocked=client.put('/market-signals/settings',json={'enabled':False})
                assert blocked.status_code==409,blocked.text
            finally:
                release.set()
            completed=future.result(timeout=10)
            assert completed.status_code==200 and completed.json()['created']==1,completed.text
    finally:
        release.set()
        event.remove(engine,'before_cursor_execute',pause_insert)
    cancelled=client.put('/market-signals/settings',json={'enabled':False})
    assert cancelled.status_code==200,cancelled.text
    assert client.get('/market-signals/predictions').json()['items'][0]['delivery_status']=='suppressed'
    with Session(engine) as db:
        assert db.query(NotificationDelivery).count()==0
