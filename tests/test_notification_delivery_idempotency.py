"""Observable delivery guarantees through real Dispatcher.run_once and HTTP."""
import pytest
from datetime import datetime,timedelta,timezone

from tests.notification_delivery_acceptance_support import LocalReceiver, run_workers, seed_delivery
from app.services.notifications.outbox import manual_retry,mark_sending,enqueue

pytestmark=pytest.mark.whitebox


def test_two_processes_reading_same_pending_message_deliver_only_once(db_session,tmp_path):
    with LocalReceiver() as receiver:
        seed_delivery(db_session,receiver)
        results=run_workers(db_session.get_bind(),tmp_path/"workers",synchronized=True)
        assert len(receiver.receipts)==1
        assert sorted(item["processed"] for item in results)==[0,1]


def test_response_lost_after_acceptance_retries_with_same_idempotency_key(db_session,tmp_path):
    with LocalReceiver(close_first=True) as receiver:
        outbox=seed_delivery(db_session,receiver,max_attempts=1)
        outbox_id=outbox.id
        assert run_workers(db_session.get_bind(),tmp_path/"first",count=1)==[{"processed":1}]
        db_session.expire_all()
        assert manual_retry(db_session,outbox_id=outbox_id) is not None
        assert run_workers(db_session.get_bind(),tmp_path/"retry",count=1)==[{"processed":1}]
        assert len(receiver.receipts)==2
        keys=[receipt["key"] for receipt in receiver.receipts]
        assert keys[0] and keys[0]==keys[1]
        assert len(receiver.effects)==1


def test_adapter_retry_uses_same_key_without_repeating_receiver_effect(db_session,tmp_path):
    with LocalReceiver(close_first=True) as receiver:
        seed_delivery(db_session,receiver,retries=1)
        assert run_workers(db_session.get_bind(),tmp_path/"adapter",count=1)==[{"processed":1}]
        assert len(receiver.receipts)==2
        assert receiver.receipts[0]["key"]==receiver.receipts[1]["key"]
        assert len(receiver.effects)==1


def test_different_rows_and_channels_have_different_delivery_keys(db_session,tmp_path):
    with LocalReceiver() as receiver:
        first=seed_delivery(db_session,receiver,name="same-payload-a")
        second=seed_delivery(db_session,receiver,name="same-payload-b")
        enqueue(db_session,event_key="another-event-same-payload",source_type="system",source_id=None,
            event_type="market_news_collect_failed",severity="warn",channel_id=first.channel_id,
            payload={"title":"隔离采集失败","body":"受控投递验收","_notification_idempotency_key":"spoofed"})
        assert run_workers(db_session.get_bind(),tmp_path/"distinct",count=1)==[{"processed":3}]
        keys=[receipt["key"] for receipt in receiver.receipts]
        assert len(keys)==len(set(keys))==3 and "spoofed" not in keys
        assert len(receiver.effects)==3


@pytest.mark.parametrize("state",["sending","sent","failed","dead_letter","future_pending"])
def test_only_due_pending_message_is_claimable(db_session,state):
    with LocalReceiver() as receiver:
        outbox=seed_delivery(db_session,receiver)
        if state=="future_pending":
            outbox.next_retry_at=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(hours=1)
        else:
            outbox.status=state
        db_session.commit()
        assert mark_sending(db_session,outbox_id=outbox.id) is None
        assert receiver.receipts==[]
