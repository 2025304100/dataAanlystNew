"""MySQL 5.7 candidate query and send-time reservations in fresh schemas."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.session import get_db
from app.api.routes.market_signals import router
from app.models.symbol import Symbol
from app.models.market_signal import MarketSignalEvidence, MarketSignalAssessment, MarketSignalPrediction
from app.models.notification import NotificationOutbox
from tests.test_market_signals_api import seed_signal_prices, make_rule_and_evidence
from tests.test_market_signal_priority_regressions import enable_without_starting_scheduler, seed_assessment

pytestmark = pytest.mark.whitebox


@pytest.fixture
def native(record_property):
    configured = os.environ.get('MARKET_SIGNAL_PRIORITY_MYSQL57_URL')
    if not configured:
        pytest.skip('Use the isolated priority verification driver')
    url = make_url(configured)
    assert url.host in ('127.0.0.1', 'localhost') and not url.database
    schema = 'codex_ms_priority_'+uuid4().hex[:16]
    admin = create_engine(url, hide_parameters=True)
    with admin.begin() as connection:
        version = connection.scalar(text('SELECT VERSION()'))
        assert version.startswith('5.7.')
        connection.exec_driver_sql(f'CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    engine = create_engine(url.set(database=schema), hide_parameters=True)
    Base.metadata.create_all(engine)
    record_property('mysql_version', version)
    record_property('isolated_schema', schema)
    with Session(engine) as db:
        db.add(Symbol(symbol='000001', name='验收银行', market='SZ', asset_type='stock'))
        db.commit()
        seed_signal_prices(db)
        enable_without_starting_scheduler(db)
    app = FastAPI()
    app.include_router(router)
    def isolated_db():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = isolated_db
    with TestClient(app) as client:
        yield engine, client
    engine.dispose()
    admin.dispose()  # Keep test schemas and their evidence; never touch gpfx.


def test_mysql_fresh_event_survives_501_expired_scores(native, monkeypatch):
    from app.services import market_signal_sources as sources
    engine, client = native
    now = datetime(2026, 10, 27, 9, 10)
    monkeypatch.setattr(sources, 'utcnow', lambda: now)
    rule, evidence = make_rule_and_evidence(client, window_days=1)
    with Session(engine) as db:
        for number in range(501):
            item = MarketSignalEvidence(fingerprint=f'old-{number}', title=f'Old event {number}',
                text='Independent historical facts', source='fixture', published_at=now-timedelta(days=10),
                time_quality='manual', metadata_json='{}')
            db.add(item)
            db.flush()
            db.add(MarketSignalAssessment(evidence_id=item.id, rule_id=rule['id'], rule_revision=1,
                request_key=f'old-{number}', status='scored', score=95, event_key=f'old-{number}',
                fact_key=f'old-{number}', analysis_json='{"novelty_points":30}', input_json='{}'))
        db.commit()
        seed_assessment(db, rule['id'], evidence['id'], 'fresh')
    response = client.post('/market-signals/run', json={'analyze': False})
    assert response.status_code == 200 and response.json()['created'] == 1, response.text
    assert client.post('/market-signals/run', json={'analyze': False}).json()['created'] == 0


def test_mysql_concurrent_preflights_reserve_one_target_slot(native, monkeypatch):
    from app.services import market_signal_sources as sources
    from app.services.market_signal_delivery import prepare_notification
    from app.services.market_event_collection_lock import MarketEventCollectionBusy
    engine, client = native
    now = [datetime(2026, 10, 27, 9, 10)]
    monkeypatch.setattr(sources, 'utcnow', lambda: now[0])
    rule, evidence = make_rule_and_evidence(client, mode='trial', window_days=1, max_daily=1,
        max_age_hours=168, cooldown_hours=0)
    with Session(engine) as db:
        seed_assessment(db, rule['id'], evidence['id'], 'first')
    assert client.post('/market-signals/run', json={'analyze': False}).json()['created'] == 1
    now[0] = datetime(2026, 10, 28, 9, 10)
    evidence = client.post('/market-signals/evidence', json={'title': '验收银行另一独立订单正式实施',
        'text': '验收银行获得新增订单100亿元，项目即日起执行。', 'published_at': now[0].isoformat(),
        'source_url': 'https://example.com/second'}).json()
    with Session(engine) as db:
        seed_assessment(db, rule['id'], evidence['id'], 'second')
    assert client.post('/market-signals/run', json={'analyze': False}).json()['created'] == 1
    now[0] = datetime(2026, 10, 29, 9, 10)
    with Session(engine) as db:
        ids = list(db.scalars(select(NotificationOutbox.id).order_by(NotificationOutbox.id)))
    assert len(ids) == 2
    def invoke(box_id):
        with Session(engine) as db:
            box = db.get(NotificationOutbox, box_id)
            # Begin a normal repeatable-read transaction before the named lock.
            db.scalar(select(MarketSignalPrediction.id).limit(1))
            try:
                return prepare_notification(db, box)
            except MarketEventCollectionBusy:
                return 'busy'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ids))
    results = [invoke(box_id) if result == 'busy' else result for box_id, result in zip(ids, results)]
    assert results.count(True) == 1 and results.count(False) == 1, results
    with Session(engine) as db:
        assert db.scalar(text("SELECT COUNT(*) FROM notification_deliveries")) == 0
        predictions = list(db.scalars(select(MarketSignalPrediction)))
        assert sum(row.delivery_status == 'queued' and json.loads(row.snapshot_json)['budget_day'] == '2026-10-29'
                   for row in predictions) == 1
