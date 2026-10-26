"""Real MySQL paths for old-0090 compensation and fresh deployment to 0091."""
import os
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.market_event import MarketEvent
from app.schemas.market_event import MarketEventCreate
from app.services.market_events import create_market_event
from tests.market_news_acceptance_support import (
    REV90, REV91, assert_identity, engine_for, evidence, isolated_url, migrate,
)

pytestmark = [pytest.mark.whitebox, pytest.mark.skipif(
    not os.environ.get("MARKET_NEWS_COMPENSATION_MYSQL57_URL")
    and os.environ.get("REQUIRE_MARKET_NEWS_ACCEPTANCE") != "1",
    reason="Requires fresh explicit compensation schemas; use the acceptance runner")]


def snapshot(connection):
    return [dict(row) for row in connection.execute(sa.text("SELECT * FROM market_events ORDER BY id")).mappings()]


def columns(connection):
    return {column["name"]: column for column in sa.inspect(connection).get_columns("market_events")}


def add_current_rows(engine):
    with Session(engine) as db:
        auto = create_market_event(db, MarketEventCreate(title="0091光伏自动标签"))
        auto_id = auto.id
        manual = create_market_event(db, MarketEventCreate(title="0091人工标签", affected_sectors="custom_review"))
        return auto_id, manual.id


@pytest.fixture(scope="module")
def compensated():
    url = isolated_url("MARKET_NEWS_COMPENSATION_MYSQL57_URL")
    engine = engine_for(url)
    with engine.connect() as connection:
        identity = assert_identity(connection, url)
        assert not sa.inspect(connection).get_table_names(), "Only a fresh schema is permitted"
        assert "STRICT_TRANS_TABLES" in identity["sql_mode"]
    migrate(url, REV90)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    with Session(engine) as db:
        db.add_all(MarketEvent(title=f"旧0090历史消息{i}", summary="原始中文摘要", impact_scope="other",
            importance_level=3, sentiment="positive", affected_sectors=tags, sector_tag_source=source,
            source="test", is_manual=int(source == "manual"), published_at=now,
            expires_at=now+timedelta(days=7), created_at=now, updated_at=now)
            for i, (tags, source) in enumerate([(None,None),("", "auto"),("custom_review", "manual"),
                                               ("retired_id", None),("solar_pv", "auto")]))
        db.commit()
    with engine.begin() as connection:
        # Reconstruct the exact defect in original 0090: the source column and
        # revision are present, but retired write constraints are still strict.
        connection.execute(sa.text("UPDATE market_events SET event_type='legacy', event_date='2026-10-26'"))
        metadata = columns(connection)
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        operations = Operations(MigrationContext.configure(connection))
        for name in ("event_type", "event_date"):
            operations.alter_column("market_events", name, existing_type=metadata[name]["type"], nullable=False)
        before = snapshot(connection)
        before_columns = columns(connection)
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == REV90
    with Session(engine) as db:
        with pytest.raises(sa.exc.OperationalError) as failure:
            create_market_event(db, MarketEventCreate(title="补偿前严格模式应拒绝"))
        assert failure.value.orig.args[0] == 1364
        db.rollback()
    # upgrade head must traverse 0091 even though 0090 is already recorded.
    migrate(url, "head")
    with engine.connect() as connection:
        upgraded, after_columns = snapshot(connection), columns(connection)
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    migrate(url, "head")
    with engine.connect() as connection:
        repeated = snapshot(connection)
    auto_id, manual_id = add_current_rows(engine)
    with engine.connect() as connection:
        with_new_rows = snapshot(connection)
    migrate(url, REV90, downgrade=True)
    with engine.connect() as connection:
        rollback_rows, rollback_columns = snapshot(connection), columns(connection)
        rollback_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    with Session(engine) as db:
        create_market_event(db, MarketEventCreate(title="回退0090后仍可写入"))
    with engine.connect() as connection:
        rollback_written = snapshot(connection)
    migrate(url, "head")
    with engine.connect() as connection:
        reupgraded = snapshot(connection)
    result = dict(identity=identity, before=before, before_columns=before_columns,
        upgraded=upgraded, after_columns=after_columns, version=version, repeated=repeated,
        auto_id=auto_id, manual_id=manual_id, with_new_rows=with_new_rows,
        rollback_rows=rollback_rows, rollback_columns=rollback_columns, rollback_revision=rollback_revision,
        rollback_written=rollback_written, reupgraded=reupgraded, reproduced_error=1364)
    evidence("mysql0091-compensation-roundtrip.json", result)
    yield result
    engine.dispose()


def test_old_0090_is_actually_broken_before_compensation(compensated):
    assert compensated["reproduced_error"] == 1364
    assert all(not compensated["before_columns"][name]["nullable"] for name in ("event_type", "event_date"))
    assert "sector_tag_source" in compensated["before_columns"]


def test_upgrade_head_repairs_constraints_preserving_all_values_and_origins(compensated):
    assert compensated["version"] == REV91
    assert compensated["upgraded"] == compensated["before"]
    assert all(compensated["after_columns"][name]["nullable"] for name in ("event_type", "event_date"))
    for name in ("event_type", "event_date"):
        before, after = compensated["before_columns"][name], compensated["after_columns"][name]
        assert str(before["type"]) == str(after["type"]) and before["default"] == after["default"]


def test_repeated_head_is_safe_and_new_writes_keep_explicit_origins(compensated):
    assert compensated["repeated"] == compensated["upgraded"]
    rows = {row["id"]: row for row in compensated["with_new_rows"]}
    assert rows[compensated["auto_id"]]["sector_tag_source"] == "auto"
    assert rows[compensated["manual_id"]]["sector_tag_source"] == "manual"
    assert rows[compensated["manual_id"]]["affected_sectors"] == "custom_review"
    assert rows[compensated["auto_id"]]["event_type"] is None
    assert rows[compensated["manual_id"]]["event_date"] is None


def test_compensation_rollback_keeps_new_rows_and_current_0090_writer_working(compensated):
    assert compensated["rollback_revision"] == REV90
    assert compensated["rollback_rows"] == compensated["with_new_rows"]
    assert all(compensated["rollback_columns"][name]["nullable"] for name in ("event_type", "event_date"))
    assert len(compensated["rollback_written"]) == 8
    assert compensated["rollback_written"][-1]["title"] == "回退0090后仍可写入"


def test_reupgrade_after_compensation_rollback_keeps_all_rows_and_origins(compensated):
    assert compensated["reupgraded"] == compensated["rollback_written"]


def test_fresh_schema_full_chain_to_head_and_current_writer():
    url = isolated_url("MARKET_NEWS_FRESH_MYSQL57_URL")
    engine = engine_for(url)
    try:
        with engine.connect() as connection:
            identity = assert_identity(connection, url)
            assert not sa.inspect(connection).get_table_names()
        migrate(url, "head")
        auto_id, manual_id = add_current_rows(engine)
        with engine.connect() as connection:
            version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            metadata, rows = columns(connection), snapshot(connection)
        assert version == REV91
        assert all(metadata[name]["nullable"] for name in ("event_type", "event_date", "sector_tag_source"))
        assert len(rows) == 2 and {row["sector_tag_source"] for row in rows} == {"auto", "manual"}
        from app.models.notification import (NotificationChannel, NotificationDelivery, NotificationOutbox,
            NotificationPolicy, NotificationPolicyChannel)
        from app.services.notifications.dispatcher import Dispatcher
        from app.services.notifications.event_emitter import emit_market_news_collect_failed
        with Session(engine) as db:
            channel = NotificationChannel(name="MySQL隔离站内验收", channel_type="in_app", enabled=True, status="enabled")
            policy = NotificationPolicy(name="MySQL隔离采集告警", enabled=True, source_types_json='["system"]',
                min_severity="warn", scope_type="all", delivery_mode="instant")
            db.add_all([channel, policy])
            db.commit()
            db.add(NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id))
            db.commit()
            key = "market_news_collect_failed:2026-10-26"
            alerts = emit_market_news_collect_failed(db, errors=["隔离故障注入"], event_key=key)
            assert len(alerts) == 1
            assert emit_market_news_collect_failed(db, errors=["隔离故障注入"], event_key=key) == []
            Dispatcher()._process_one(db, alerts[0])
            assert emit_market_news_collect_failed(db, errors=["隔离故障注入"], event_key=key) == []
            assert db.query(NotificationOutbox).count() == 1
            assert db.query(NotificationOutbox).one().status == "sent"
            assert db.query(NotificationDelivery).one().status == "success"
            notification = dict(outbox_count=1, delivery_status="success", pending_dedup=True, sent_dedup=True)
        evidence("mysql0091-fresh-head.json", dict(identity=identity, version=version, columns=metadata,
            rows=rows, auto_id=auto_id, manual_id=manual_id, in_app_notification=notification))
    finally:
        engine.dispose()
