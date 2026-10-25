"""Validate the real audit CHECK and transactional receipts in isolated SQLite."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
from typing import get_args

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services.data_governance_audit import (
    ALLOWED_AUDIT_ACTIONS,
    AuditAction,
    DataGovernanceAuditEvent,
    write_audit_event,
)

pytestmark = pytest.mark.whitebox
ROOT = Path(__file__).resolve().parents[1]
FACTOR_ACTIONS = {
    "FACTOR_DRAFT_SUBMITTED", "FACTOR_DRAFT_APPROVED",
    "FACTOR_DRAFT_REJECTED", "FACTOR_VERSION_PROMOTED",
}


def _migration(connection):
    path = next((ROOT / "alembic" / "versions").glob("*0071*market_bar_correction_audit.py"))
    spec = importlib.util.spec_from_file_location("market_bar_correction_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def _legacy_schema(connection, actions):
    connection.execute(text("PRAGMA foreign_keys=ON"))
    connection.execute(text("CREATE TABLE portfolios (id INTEGER PRIMARY KEY)"))
    connection.execute(text("INSERT INTO portfolios VALUES (1)"))
    # An extra legacy column makes loss of reflected columns/data observable.
    check = ""
    if actions is not None:
        check = ",CONSTRAINT ck_dg_audit_action_values CHECK (action IN ({}))".format(
            ",".join("'{}'".format(action) for action in sorted(actions))
        )
    connection.execute(text("""CREATE TABLE data_governance_audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, action VARCHAR(64) NOT NULL,
        portfolio_id INTEGER REFERENCES portfolios(id) ON DELETE SET NULL,
        symbol_id INTEGER, business_key VARCHAR(128), occurred_at DATETIME NOT NULL,
        operator_id VARCHAR(128) NOT NULL DEFAULT 'system', correlation_id VARCHAR(128),
        before_json TEXT, after_json TEXT, attributes_json TEXT, note TEXT,
        legacy_blob TEXT""" + check + ")"))
    connection.execute(text("CREATE INDEX ix_audit_legacy_action ON data_governance_audit_events(action)"))
    connection.execute(text("""CREATE INDEX ix_dg_audit_portfolio_action_time
        ON data_governance_audit_events(portfolio_id,action,occurred_at)"""))
    connection.execute(text("""INSERT INTO data_governance_audit_events
        (action,portfolio_id,occurred_at,before_json,after_json,attributes_json,note,legacy_blob)
        VALUES ('DATA_SOURCE_FAILOVER',1,'2026-10-15 12:34:56',:before,:after,:attrs,:note,:legacy)
        """), {
        "before": '{ "source": "old" }', "after": '{"source":"new"}',
        "attrs": '{"历史证据":[1,2]}', "note": "preserve verbatim",
        "legacy": "legacy-data-stays",
    })


def _rows(connection):
    return list(connection.execute(text("SELECT * FROM data_governance_audit_events ORDER BY id")))


def _check_actions(connection):
    item = next(c for c in inspect(connection).get_check_constraints("data_governance_audit_events")
                if c["name"] == "ck_dg_audit_action_values")
    return set(re.findall(r"'([^']+)'", item["sqltext"]))


def test_literal_export_helper_and_metadata_share_complete_contract():
    constraint = next(c for c in DataGovernanceAuditEvent.__table__.constraints
                      if isinstance(c, CheckConstraint) and c.name == "ck_dg_audit_action_values")
    assert set(get_args(AuditAction)) == ALLOWED_AUDIT_ACTIONS
    assert set(re.findall(r"'([^']+)'", str(constraint.sqltext))) == ALLOWED_AUDIT_ACTIONS
    assert len(ALLOWED_AUDIT_ACTIONS) == 18
    assert FACTOR_ACTIONS | {"MARKET_BAR_CORRECTION"} <= ALLOWED_AUDIT_ACTIONS


@pytest.mark.parametrize("missing_factor_actions", [False, True])
def test_upgrade_replaces_named_check_preserving_rows_indexes_and_foreign_key(missing_factor_actions):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            old_actions = ALLOWED_AUDIT_ACTIONS - {"MARKET_BAR_CORRECTION"}
            if missing_factor_actions:
                old_actions -= FACTOR_ACTIONS
            _legacy_schema(connection, old_actions)
            before = _rows(connection)
            indexes = inspect(connection).get_indexes("data_governance_audit_events")
            foreign_keys = inspect(connection).get_foreign_keys("data_governance_audit_events")
            migration = _migration(connection)
            migration.upgrade()
            assert _rows(connection) == before
            assert _check_actions(connection) == ALLOWED_AUDIT_ACTIONS
            assert inspect(connection).get_indexes("data_governance_audit_events") == indexes
            assert inspect(connection).get_foreign_keys("data_governance_audit_events") == foreign_keys
            # Exercise the real helper against the migrated table, not create_all.
            with Session(bind=connection) as db:
                for action in sorted(ALLOWED_AUDIT_ACTIONS):
                    result = write_audit_event(db, action, portfolio_id=1, business_key=action)
                    assert result.action == action
                db.flush()
            # A direct SQL write still cannot bypass the widened SQLite CHECK.
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("""INSERT INTO data_governance_audit_events
                    (action,occurred_at) VALUES ('NOT_AN_AUDIT_ACTION','2026-10-16')"""))
            migration.upgrade()  # Already aligned is safe to replay.
            all_evidence = _rows(connection)
            assert len(all_evidence) == 1 + len(ALLOWED_AUDIT_ACTIONS)
            assert any(row.action == "MARKET_BAR_CORRECTION" for row in all_evidence)
            migration.downgrade()
            migration.upgrade()
            assert _rows(connection) == all_evidence
            assert _check_actions(connection) == ALLOWED_AUDIT_ACTIONS
    finally:
        engine.dispose()


def test_upgrade_adds_absent_check_without_rewriting_rows():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection, None)
            before = _rows(connection)
            _migration(connection).upgrade()
            assert _rows(connection) == before
            assert _check_actions(connection) == ALLOWED_AUDIT_ACTIONS
    finally:
        engine.dispose()


def test_unknown_persisted_action_fails_before_schema_or_evidence_change():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection, None)
            connection.execute(text("""INSERT INTO data_governance_audit_events
                (action,occurred_at,legacy_blob) VALUES ('SITE_EXTENSION','2026-10-16','retain')"""))
            before = _rows(connection)
            ddl_before = connection.execute(text("""SELECT sql FROM sqlite_master
                WHERE type='table' AND name='data_governance_audit_events'""")).scalar_one()
            with pytest.raises(RuntimeError, match="Unrecognized persisted audit actions"):
                _migration(connection).upgrade()
            assert _rows(connection) == before
            assert connection.execute(text("""SELECT sql FROM sqlite_master
                WHERE type='table' AND name='data_governance_audit_events'""")).scalar_one() == ddl_before
    finally:
        engine.dispose()


@pytest.mark.parametrize("commit", [False, True])
def test_correction_receipt_commits_or_rolls_back_with_bar_change(commit):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            _legacy_schema(connection, ALLOWED_AUDIT_ACTIONS - {"MARKET_BAR_CORRECTION"})
            _migration(connection).upgrade()
            connection.execute(text("CREATE TABLE market_bar_probe (id INTEGER PRIMARY KEY, close FLOAT)"))
            connection.execute(text("INSERT INTO market_bar_probe VALUES (1,26.37)"))
        with Session(engine) as db:
            db.execute(text("UPDATE market_bar_probe SET close=43.07 WHERE id=1"))
            result = write_audit_event(
                db, "MARKET_BAR_CORRECTION", symbol_id=1,
                business_key="market_bar_probe:1", operator_id="test-operator",
                before={"id": 1, "close": 26.37}, after={"id": 1, "close": 43.07},
                attributes={"evidence_sha256": "f" * 64, "provider": "verified-provider"},
            )
            assert result.action == "MARKET_BAR_CORRECTION"
            assert result.event_id is not None
            if commit:
                db.commit()
            else:
                db.rollback()
        with Session(engine) as db:
            assert db.execute(text("SELECT close FROM market_bar_probe WHERE id=1")).scalar_one() == (
                43.07 if commit else 26.37
            )
            receipt = db.scalar(select(DataGovernanceAuditEvent).where(
                DataGovernanceAuditEvent.action == "MARKET_BAR_CORRECTION"
            ))
            if not commit:
                assert receipt is None
            else:
                assert receipt.operator_id == "test-operator"
                assert json.loads(receipt.before_json) == {"id": 1, "close": 26.37}
                assert json.loads(receipt.after_json) == {"id": 1, "close": 43.07}
                assert "original_action" not in json.loads(receipt.attributes_json)
    finally:
        engine.dispose()


def test_missing_audit_table_is_an_explicit_migration_failure():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="data_governance_audit_events is missing"):
                _migration(connection).upgrade()
    finally:
        engine.dispose()


def test_precision_revision_retains_correction_audit_in_single_head_lineage():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(cfg)
    assert len(scripts.get_heads()) == 1
    assert "wps_0023_072_verified_input_precision" in {rev.revision for rev in scripts.walk_revisions()}
    assert scripts.get_revision("wps_0023_072_verified_input_precision").down_revision == (
        "wps_0023_071_market_bar_correction_audit"
    )
    assert scripts.get_revision("wps_0023_071_market_bar_correction_audit").down_revision == (
        "wps_0023_070_signal_rules_contract"
    )
