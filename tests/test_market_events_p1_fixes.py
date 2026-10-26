"""Regression coverage for R01–R05; only isolated SQLite and mocked sources."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes.market_events import router
from app.db.session import get_db
from app.models.market_event import MarketEvent, MarketEventCollectRun
from app.models.notification import NotificationChannel, NotificationPolicy, NotificationPolicyChannel, NotificationOutbox
from app.schemas.market_event import MarketEventCleanupRequest, MarketEventCollectRequest
from app.services import market_events as svc, scheduled_tasks
from app.services.market_event_collection_lock import MarketEventCollectionBusy, market_event_write_lock

pytestmark = pytest.mark.whitebox
T = datetime(2026, 10, 26)


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as api:
        yield api


def add_event(db, *, days, title=None, **changes):
    values = dict(title=title or f"光伏历史事件{days}", impact_scope="other", importance_level=3,
                  affected_market="A股/期货", affected_sectors="solar_pv", sentiment="positive",
                  source="test", is_manual=0, published_at=T-timedelta(days=days),
                  expires_at=T-timedelta(days=1))
    values.update(changes)
    row = MarketEvent(**values)
    db.add(row)
    db.commit()
    return row


def raw():
    return {"title": "央行公布新的货币政策安排", "source": "eastmoney-global", "published_at": T}


def test_all_source_errors_reach_response_ledger_and_first_scheduled_alert(db_session, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline after retry")
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", fail)
    emit = MagicMock(return_value=[])
    monkeypatch.setattr("app.services.notifications.event_emitter.emit_market_news_collect_failed", emit)
    source, result = scheduled_tasks._dispatch_task(SimpleNamespace(task_type="market_events_collect", payload_json="{}"))
    assert source == "market_events" and result["status"] == "failed"
    runs = svc.list_collect_runs(db_session)
    assert len(runs) == 1 and runs[0].status == "failed"
    errors = json.loads(runs[0].errors_json)
    assert len(errors) == 6 and all(any(error.startswith(s) for error in errors) for s in svc.ALL_COLLECT_SOURCES)
    emit.assert_called_once()
    assert len(emit.call_args.kwargs["errors"]) == 6
    assert not svc.recent_collects_all_empty(db_session)


def test_partial_source_failure_keeps_successes_without_all_failed_alert(db_session, monkeypatch):
    def fetch(fetcher, *, api_key, **kwargs):
        if api_key == "caixin":
            raise RuntimeError("one source offline")
        return pd.DataFrame([{"title": f"市场消息发布来自{api_key}", "date": T}])
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", fetch)
    emit = MagicMock()
    monkeypatch.setattr("app.services.notifications.event_emitter.emit_market_news_collect_failed", emit)
    _, task = scheduled_tasks._dispatch_task(SimpleNamespace(task_type="market_events_collect", payload_json="{}"))
    assert task["status"] == "done"
    run = svc.list_collect_runs(db_session)[0]
    assert run.status == "partial" and run.collected == 5
    assert len(json.loads(run.errors_json)) == 1
    emit.assert_not_called()


def test_first_source_failure_writes_real_outbox_and_repeated_day_is_deduplicated(db_session, monkeypatch):
    channel = NotificationChannel(name="market-regression", channel_type="in_app", enabled=True, status="enabled")
    policy = NotificationPolicy(name="market-regression", enabled=True, source_types_json='["system"]',
                                min_severity="warn", scope_type="all", delivery_mode="instant")
    db_session.add_all([channel, policy])
    db_session.commit()
    db_session.add(NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id))
    db_session.commit()
    def fail(*args, **kwargs):
        raise RuntimeError("offline after retry")
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", fail)
    task = SimpleNamespace(task_type="market_events_collect", payload_json="{}")
    assert scheduled_tasks._dispatch_task(task)[1]["status"] == "failed"
    outboxes = db_session.query(NotificationOutbox).all()
    assert len(outboxes) == 1
    assert outboxes[0].event_type == "market_news_collect_failed"
    assert outboxes[0].status == "pending"
    assert "offline after retry" in outboxes[0].payload_json
    scheduled_tasks._dispatch_task(task)
    assert db_session.query(NotificationOutbox).count() == 1


def test_manual_api_reports_failed_source_status(client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline after retry")
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", fail)
    response = client.post("/market-events/collect", json={"sources": ["caixin"]})
    assert response.status_code == 200  # Successful request with explicit domain failure.
    assert response.json()["status"] == "failed"
    assert response.json()["errors"] == ["caixin: offline after retry"]


def test_empty_sources_and_healthy_duplicates_are_distinct(db_session, monkeypatch):
    monkeypatch.setattr(svc, "_fetch_global_news", lambda: [raw()])
    monkeypatch.setattr(svc, "_now", lambda: T)
    payload = MarketEventCollectRequest(sources=["eastmoney-global"])
    assert svc.collect_market_events(db_session, payload).collected == 1
    for _ in range(2):
        result = svc.collect_market_events(db_session, payload)
        assert result.status == "done" and result.skipped_duplicate == 1
    assert not svc.recent_collects_all_empty(db_session)
    monkeypatch.setattr(svc, "_fetch_global_news", lambda: [])
    for _ in range(2):
        assert svc.collect_market_events(db_session, payload).errors == []
    assert svc.recent_collects_all_empty(db_session)


@pytest.mark.parametrize("value", [7, 29, 89, 0, -1, 3651, "90", 90.0, True, None])
def test_retention_guard_rejects_same_values_at_all_entrypoints(db_session, client, value):
    row = add_event(db_session, days=800)
    with pytest.raises(ValidationError):
        MarketEventCleanupRequest(keep_days=value)
    with pytest.raises(ValueError):
        scheduled_tasks.validate_task_payload("market_events_cleanup", {"keep_days": value})
    with pytest.raises(ValueError):
        svc.cleanup_expired_market_events(db_session, keep_days=value)
    assert client.post("/market-events/cleanup", json={"keep_days": value}).status_code == 422
    assert db_session.get(MarketEvent, row.id) is not None


@pytest.mark.parametrize("requested,effective", [(90, 365), (180, 365), (365, 365), (730, 730), (3650, 3650)])
def test_legacy_retention_is_promoted_and_reported(db_session, client, monkeypatch, requested, effective):
    monkeypatch.setattr(svc, "_now", lambda: T)
    add_event(db_session, days=effective+1)
    protected = add_event(db_session, days=effective)
    assert scheduled_tasks.validate_task_payload("market_events_cleanup", {"keep_days": requested}) == {"keep_days": effective}
    response = client.post("/market-events/cleanup", json={"keep_days": requested})
    assert response.status_code == 200
    assert response.json() == {"deleted": 1, "keep_days": effective, "cutoff_date": (T-timedelta(days=effective)).isoformat()}
    assert db_session.get(MarketEvent, protected.id) is not None


def test_default_cleanup_preserves_all_year_hotspots_and_is_idempotent(db_session, client, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: T)
    for days in (2, 20, 60, 120, 300, 400):
        add_event(db_session, days=days)
    before = svc.get_market_event_hotspots(db_session, days=365).model_dump(exclude={"generated_at"})
    assert before["sectors"][0]["total"] == 5
    assert client.post("/market-events/cleanup").json()["deleted"] == 1
    assert svc.get_market_event_hotspots(db_session, days=365).model_dump(exclude={"generated_at"}) == before
    assert client.post("/market-events/cleanup").json()["deleted"] == 0
    assert scheduled_tasks.validate_task_payload("market_events_cleanup", {}) == {"keep_days": 365}


def test_existing_ninety_day_schedule_remains_runnable_and_preserves_history(db_session, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: T)
    protected = add_event(db_session, days=120)
    old = add_event(db_session, days=400)
    old_id, protected_id = old.id, protected.id
    _, task = scheduled_tasks._dispatch_task(SimpleNamespace(task_type="market_events_cleanup", payload_json='{"keep_days":90}'))
    db_session.expire_all()
    assert task["status"] == "done" and "365" in task["message"]
    assert db_session.get(MarketEvent, protected_id) is not None
    assert db_session.get(MarketEvent, old_id) is None


@pytest.mark.parametrize("level", [2, 3, 4, 5])
def test_json_level_string_keys_replace_matching_level(tmp_path, monkeypatch, level):
    target = tmp_path / "keywords.json"
    target.write_text(json.dumps({"importance_level": {str(level): ["审查独有词甲"]}}), encoding="utf-8")
    monkeypatch.setattr(svc, "_KEYWORD_OVERRIDE_FILE", target)
    monkeypatch.setattr(svc, "_keyword_cache", {"mtime": None, "scope": None, "level": None, "sentiment": None})
    assert svc._classify_importance_level("审查独有词甲", None) == level
    assert svc._merged_keywords()[1][level] == ["审查独有词甲"]
    target.unlink()
    assert svc._classify_importance_level("审查独有词甲", None) == 2


@pytest.mark.parametrize("config", [[], {"importance_level": []}, {"importance_level": {"9": ["错词"]}},
                                  {"sentiment": {"unknown": ["错词"]}}, {"importance_level": {"5": [None]}}])
def test_invalid_keyword_configuration_falls_back_as_a_whole(tmp_path, monkeypatch, config):
    target = tmp_path / "keywords.json"
    target.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(svc, "_KEYWORD_OVERRIDE_FILE", target)
    monkeypatch.setattr(svc, "_keyword_cache", {"mtime": None, "scope": None, "level": None, "sentiment": None})
    assert svc._classify_importance_level("战争突发", None) == 5
    assert svc._classify_sentiment("利好上涨", None) == "positive"


def test_interleaved_collect_is_busy_then_retry_deduplicates(db_session, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: T)
    monkeypatch.setattr(svc, "_fetch_global_news", lambda: [raw()])
    other = sessionmaker(bind=db_session.get_bind())()
    payload = MarketEventCollectRequest(sources=["eastmoney-global"])
    original = svc._existing_title_hashes
    attempted = False
    def interleave(db, candidates):
        nonlocal attempted
        snapshot = original(db, candidates)
        if not attempted:
            attempted = True
            with pytest.raises(MarketEventCollectionBusy):
                svc.collect_market_events(other, payload, trigger_source="scheduled")
        return snapshot
    monkeypatch.setattr(svc, "_existing_title_hashes", interleave)
    try:
        assert svc.collect_market_events(db_session, payload).collected == 1
        again = svc.collect_market_events(other, payload, trigger_source="scheduled")
        assert again.collected == 0 and again.skipped_duplicate == 1
        assert db_session.query(MarketEvent).count() == 1
    finally:
        other.close()


def test_busy_collect_is_http_409_and_lock_releases_on_exception(db_session, client, monkeypatch):
    monkeypatch.setattr(svc, "_fetch_global_news", lambda: [])
    with market_event_write_lock(db_session):
        response = client.post("/market-events/collect", json={"sources": ["eastmoney-global"]})
        assert response.status_code == 409
    with pytest.raises(RuntimeError):
        with market_event_write_lock(db_session):
            raise RuntimeError("abort writer")
    assert client.post("/market-events/collect", json={"sources": ["eastmoney-global"]}).status_code == 200


def test_sqlite_lock_blocks_another_process_and_recovers_after_owner_exit(tmp_path):
    url = f"sqlite:///{(tmp_path/'shared.db').as_posix()}"
    engine = create_engine(url)
    child_code = """
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.market_event_collection_lock import market_event_write_lock, MarketEventCollectionBusy
try:
    with Session(create_engine(sys.argv[1])) as db, market_event_write_lock(db):
        print('acquired', flush=True)
except MarketEventCollectionBusy:
    print('busy', flush=True)
"""
    with Session(engine) as db, market_event_write_lock(db):
        child = subprocess.run([sys.executable, "-c", child_code, url], capture_output=True, text=True, timeout=20)
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "busy"
    owner_code = child_code.replace("print('acquired', flush=True)", "print('acquired', flush=True); __import__('os')._exit(0)")
    child = subprocess.run([sys.executable, "-c", owner_code, url], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0 and child.stdout.strip() == "acquired", child.stderr
    with Session(engine) as db, market_event_write_lock(db):
        pass  # OS released the crashed owner's handle; lock file remains reusable.
    engine.dispose()


@pytest.mark.parametrize("outcome", ["success", "exception", "release_error", "busy"])
def test_mysql_named_lock_ownership_uses_dedicated_connection(outcome):
    connection = MagicMock()
    release = RuntimeError("lost connection") if outcome == "release_error" else 1
    connection.scalar.side_effect = [0] if outcome == "busy" else [1, release]
    engine = MagicMock()
    engine.engine = engine
    engine.dialect.name = "mysql"
    engine.url.database = "market_test"
    engine.connect.return_value.__enter__.return_value = connection
    db = MagicMock()
    db.get_bind.return_value = engine
    if outcome in ("exception", "release_error"):
        with pytest.raises(RuntimeError):
            with market_event_write_lock(db):
                if outcome == "exception":
                    raise RuntimeError("writer failed")
    elif outcome == "busy":
        with pytest.raises(MarketEventCollectionBusy):
            with market_event_write_lock(db):
                pytest.fail("must not enter write section")
    else:
        with market_event_write_lock(db):
            db.commit()  # Business commit does not end the dedicated lock.
    assert "GET_LOCK" in str(connection.scalar.call_args_list[0].args[0])
    if outcome != "busy":
        assert "RELEASE_LOCK" in str(connection.scalar.call_args_list[1].args[0])
    if outcome == "release_error":
        connection.invalidate.assert_called_once()


def test_failed_database_flush_rolls_back_before_failure_ledger(db_session, monkeypatch):
    monkeypatch.setattr(svc, "_fetch_global_news", lambda: [raw()])
    monkeypatch.setattr(svc, "_now", lambda: T)
    def violate(session, context, instances):
        for item in session.new:
            if isinstance(item, MarketEvent):
                item.title = None
    event.listen(db_session, "before_flush", violate)
    try:
        with pytest.raises(IntegrityError):
            svc.collect_market_events(db_session, MarketEventCollectRequest(sources=["eastmoney-global"]))
    finally:
        event.remove(db_session, "before_flush", violate)
    assert db_session.query(MarketEvent).count() == 0
    runs = db_session.query(MarketEventCollectRun).all()
    assert len(runs) == 1 and runs[0].status == "failed"
    assert "IntegrityError" in runs[0].errors_json
