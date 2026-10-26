"""Real MySQL 5.7 + two independent dispatchers + loopback HTTP acceptance."""
import os

import pytest
from sqlalchemy import inspect,text
from sqlalchemy.orm import Session

from app.models.notification import NotificationDelivery,NotificationOutbox
from app.services.notifications.outbox import manual_retry
from tests.market_news_acceptance_support import (REV91,assert_identity,engine_for,evidence,isolated_url,migrate)
from tests.notification_delivery_acceptance_support import LocalReceiver,run_workers,seed_delivery

pytestmark=[pytest.mark.whitebox,pytest.mark.skipif(
    not os.environ.get("NOTIFICATION_DELIVERY_MYSQL57_URL") and os.environ.get("REQUIRE_MARKET_NEWS_ACCEPTANCE")!="1",
    reason="Requires a fresh explicit MySQL schema; use the acceptance runner delivery stage")]


@pytest.fixture(scope="module")
def delivery_engine():
    url=isolated_url("NOTIFICATION_DELIVERY_MYSQL57_URL")
    engine=engine_for(url)
    with engine.connect() as connection:
        identity=assert_identity(connection,url)
        assert not inspect(connection).get_table_names()
    migrate(url,"head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version"))==REV91
        storage=connection.scalar(text("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='notification_outbox'"))
        assert storage=="InnoDB"
    evidence("delivery-mysql57-identity.json",dict(identity=identity,revision=REV91,storage_engine=storage))
    try:
        yield engine
    finally:
        engine.dispose()


def save_delivery_receipt(name,db,receiver,workers):
    db.expire_all()
    evidence(name,dict(receipts=receiver.receipts,effects=receiver.effects,workers=workers,
        outboxes=[dict(id=row.id,status=row.status,attempt_count=row.attempt_count) for row in db.query(NotificationOutbox)],
        deliveries=[dict(outbox_id=row.outbox_id,status=row.status,attempt_number=row.attempt_number) for row in db.query(NotificationDelivery)]))


def test_mysql57_two_processes_observing_same_pending_message_send_once(delivery_engine,tmp_path):
    with Session(delivery_engine) as db,LocalReceiver() as receiver:
        seed_delivery(db,receiver,name="mysql-concurrent")
        workers=run_workers(delivery_engine,tmp_path/"concurrent",synchronized=True)
        assert len(receiver.receipts)==1 and sorted(row["processed"] for row in workers)==[0,1]
        save_delivery_receipt("delivery-mysql57-concurrent.json",db,receiver,workers)


def test_mysql57_response_loss_restart_retry_reuses_same_key(delivery_engine,tmp_path):
    with Session(delivery_engine) as db,LocalReceiver(close_first=True) as receiver:
        item=seed_delivery(db,receiver,name="mysql-response-loss",max_attempts=1)
        item_id=item.id
        first=run_workers(delivery_engine,tmp_path/"first",count=1)
        # A manual-retry HTTP request opens a new transaction. End this test
        # coordinator's pre-worker RR snapshot before simulating that request.
        db.rollback()
        assert manual_retry(db,outbox_id=item_id) is not None
        retry=run_workers(delivery_engine,tmp_path/"retry",count=1)
        assert first==retry==[{"processed":1}]
        assert len(receiver.receipts)==2
        keys=[row["key"] for row in receiver.receipts]
        assert keys[0] and keys[0]==keys[1] and len(receiver.effects)==1
        save_delivery_receipt("delivery-mysql57-response-loss.json",db,receiver,[*first,*retry])
