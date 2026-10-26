"""Mandatory real-MySQL lane. Run via verify_market_news_acceptance.py."""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from tests.market_news_acceptance_support import REV89, REV90, assert_identity, engine_for, evidence, isolated_url, migrate

pytestmark = [pytest.mark.whitebox, pytest.mark.skipif(
    not os.environ.get("MARKET_NEWS_MYSQL57_URL") and os.environ.get("REQUIRE_MARKET_NEWS_ACCEPTANCE") != "1",
    reason="Explicit isolated MySQL lane; run scripts/verify_market_news_acceptance.py")]


def snapshot(connection):
    return [dict(row) for row in connection.execute(text("SELECT * FROM market_events ORDER BY id")).mappings()]


def business_rows(rows):
    return [{key: value for key, value in row.items() if key != "sector_tag_source"} for row in rows]


@pytest.fixture(scope="module")
def lifecycle():
    url = isolated_url()
    engine = engine_for(url)
    with engine.connect() as connection:
        identity = assert_identity(connection, url)
        assert not inspect(connection).get_table_names(), "Supply a fresh empty acceptance schema"
    migrate(url, REV89)
    with engine.begin() as connection:
        assert "sector_tag_source" not in {c["name"] for c in inspect(connection).get_columns("market_events")}
        now = datetime.now()
        records = []
        for index, labels in enumerate((None, "", "custom_review", "solar_pv", "retired_id"), 1):
            title = "迁移中文与emoji🚀" if index < 3 else f"迁移消息{index}"
            records.append(dict(title=title, title_hash=hashlib.md5(title.encode()).hexdigest(), summary="历史原文不丢失",
                impact_scope="other", importance_level=3, affected_market="A股/期货", affected_sectors=labels,
                sentiment="positive", source="manual" if index == 3 else "test", is_manual=int(index == 3),
                published_at=now-timedelta(days=index), expires_at=now+timedelta(days=7), created_at=now, updated_at=now))
        for record in records:
            record.update(event_type="legacy_fixture", event_date="2026-10-26")
        connection.execute(text("INSERT INTO market_events (event_type,event_date,title,title_hash,summary,impact_scope,importance_level,affected_market,"
            "affected_sectors,sentiment,source,is_manual,published_at,expires_at,created_at,updated_at) VALUES "
            "(:event_type,:event_date,:title,:title_hash,:summary,:impact_scope,:importance_level,:affected_market,:affected_sectors,:sentiment,:source,"
            ":is_manual,:published_at,:expires_at,:created_at,:updated_at)"), records)
        before = snapshot(connection)
    migrate(url, REV90)
    with engine.connect() as connection:
        upgraded = snapshot(connection)
        columns = inspect(connection).get_columns("market_events")
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        storage_engine = connection.scalar(text("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='market_events'"))
    migrate(url, REV90)  # no-op replay via the actual version table
    from app.models.market_event import MarketEvent
    from app.schemas.market_event import MarketEventCreate
    from app.services.market_events import create_market_event
    with Session(engine) as db:
        created = create_market_event(db, MarketEventCreate(title="光伏迁移后自动打标"))
        auto_id = created.id
        created = create_market_event(db, MarketEventCreate(title="迁移后人工板块", affected_sectors="custom_review"))
        manual_id = created.id
        assert db.get(MarketEvent, auto_id).sector_tag_source == "auto"
        assert db.get(MarketEvent, manual_id).sector_tag_source == "manual"
    with engine.connect() as connection:
        with_new_rows = snapshot(connection)
    migrate(url, REV89, downgrade=True)
    with engine.connect() as connection:
        rolled_back = snapshot(connection)
        rollback_columns = {c["name"] for c in inspect(connection).get_columns("market_events")}
        rollback_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    migrate(url, REV90)
    with engine.connect() as connection:
        restarted = snapshot(connection)
    result = dict(identity=identity, before=before, upgraded=upgraded, columns=columns, version=version,
                  engine=storage_engine, with_new_rows=with_new_rows, rolled_back=rolled_back,
                  rollback_columns=sorted(rollback_columns), rollback_revision=rollback_revision, restarted=restarted)
    evidence("mysql-migration-roundtrip.json", result)
    yield engine, result
    engine.dispose()


def test_0090_upgrade_adds_nullable_column_without_rewriting_business_data(lifecycle):
    _, result = lifecycle
    assert result["version"] == REV90 and result["engine"] == "InnoDB"
    column = next(c for c in result["columns"] if c["name"] == "sector_tag_source")
    assert column["nullable"] is True and column["type"].length == 16
    assert column["type"].__class__.__name__ == "VARCHAR"
    assert business_rows(result["upgraded"]) == result["before"]
    assert all(row["sector_tag_source"] is None for row in result["upgraded"])


def test_0090_rollback_preserves_old_and_post_upgrade_rows(lifecycle):
    _, result = lifecycle
    assert result["rollback_revision"] == REV89
    assert "sector_tag_source" not in result["rollback_columns"]
    assert result["rolled_back"] == business_rows(result["with_new_rows"])


def test_retired_write_requirements_relaxed_without_inventing_legacy_facts(lifecycle):
    _, result = lifecycle
    columns = {c["name"]: c for c in result["columns"]}
    assert columns["event_type"]["nullable"] and columns["event_date"]["nullable"]
    assert all(row["event_type"] == "legacy_fixture" and row["event_date"] == "2026-10-26" for row in result["upgraded"])
    assert result["with_new_rows"][-1]["event_type"] is None
    assert result["with_new_rows"][-1]["event_date"] is None


def test_reupgrade_is_safe_after_restart_and_keeps_legacy_labels(lifecycle):
    engine, result = lifecycle
    assert business_rows(result["restarted"]) == result["rolled_back"]
    assert all(row["sector_tag_source"] is None for row in result["restarted"])
    from app.models.market_event import MarketEvent
    from app.schemas.market_event import MarketEventUpdate
    from app.services.market_events import update_market_event
    with Session(engine) as db:
        item = db.get(MarketEvent, 3)
        assert item.affected_sectors == "custom_review"
        update_market_event(db, item.id, MarketEventUpdate(title="黄金文本变更但保留历史标签"))
        assert item.affected_sectors == "custom_review"
        update_market_event(db, item.id, MarketEventUpdate(sector_tag_source="auto"))
        assert item.sector_tag_source == "auto" and "precious_metals" in item.affected_sectors


def test_mysql_named_lock_blocks_a_second_process_and_releases(lifecycle):
    engine, _ = lifecycle
    from app.services.market_event_collection_lock import market_event_write_lock
    code = """
from sqlalchemy.orm import Session
from tests.market_news_acceptance_support import isolated_url,engine_for
from app.services.market_event_collection_lock import market_event_write_lock,MarketEventCollectionBusy
with Session(engine_for(isolated_url())) as db:
 try:
  with market_event_write_lock(db): print('acquired',flush=True)
 except MarketEventCollectionBusy: print('busy',flush=True)
"""
    with Session(engine) as db, market_event_write_lock(db):
        db.commit()
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "busy"
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0 and child.stdout.strip() == "acquired", child.stderr
    evidence("mysql-cross-process-lock.json", {"while_held": "busy", "after_release": "acquired", "business_commit_preserved_lock": True})


def test_mysql_repeatable_read_old_snapshot_does_not_allow_duplicate(lifecycle, monkeypatch):
    engine, _ = lifecycle
    from app.models.market_event import MarketEvent
    from app.schemas.market_event import MarketEventCollectRequest
    from app.services import market_events as service
    title = "真实MySQL重复读去重验收专属标题"
    monkeypatch.setattr(service, "_fetch_global_news", lambda: [{"title": title, "published_at": service._now(), "source": "eastmoney-global"}])
    payload = MarketEventCollectRequest(sources=["eastmoney-global"])
    with Session(engine) as stale, Session(engine) as writer:
        assert stale.query(MarketEvent).filter_by(title=title).count() == 0
        assert service.collect_market_events(writer, payload).collected == 1
        assert stale.query(MarketEvent).filter_by(title=title).count() == 0  # actual old RR snapshot
        result = service.collect_market_events(stale, payload)
        assert result.collected == 0 and result.skipped_duplicate == 1
    with Session(engine) as fresh:
        assert fresh.query(MarketEvent).filter_by(title=title).count() == 1
