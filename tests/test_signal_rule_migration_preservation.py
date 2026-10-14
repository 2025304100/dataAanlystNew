"""Exercise the real legacy schema and ORM; never hide drift with create_all."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio
from app.models.signal_rule import SignalRule
from app.schemas.signal_rule import SignalRuleUpsert
from app.services.signal_rules import get_active_signal_rule, upsert_active_signal_rule

pytestmark = pytest.mark.whitebox
ROOT = Path(__file__).resolve().parents[1]
LEGACY_COLUMNS = (
    "id, portfolio_id, rule_name, rule_type, enabled, config_json, created_at, updated_at"
)


def _migration(connection):
    path = next((ROOT / "alembic" / "versions").glob("*0070*signal_rules_contract.py"))
    spec = importlib.util.spec_from_file_location("signal_rules_contract_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def _legacy_schema(connection):
    # Match revision 008, including its extra mandatory columns and indexes.
    connection.execute(text("CREATE TABLE portfolios (id INTEGER PRIMARY KEY)"))
    connection.execute(text("INSERT INTO portfolios VALUES (1), (2), (3)"))
    connection.execute(text("""CREATE TABLE signal_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio_id INTEGER NOT NULL,
        rule_name VARCHAR(128) NOT NULL, rule_type VARCHAR(32) NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL,
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"""))
    for field in ("portfolio_id", "rule_type", "enabled"):
        connection.execute(text(
            f"CREATE INDEX ix_signal_rules_{field} ON signal_rules ({field})"
        ))
    connection.execute(text("""INSERT INTO signal_rules
        (portfolio_id,rule_name,rule_type,enabled,config_json,created_at,updated_at)
        VALUES (1,'historic MACD','MACD',1,:first,'2026-08-01','2026-08-01'),
               (2,'disabled custom','custom',0,:second,'2026-08-02','2026-08-02')"""), {
        "first": '{"fast":12,"slow":26,"quality_tolerance":8}',
        "second": '{ "unknown_extension": [1, 2], "mode": "aggressive" }',
    })


def _legacy_evidence(connection):
    return list(connection.execute(text(
        f"SELECT {LEGACY_COLUMNS} FROM signal_rules WHERE id <= 2 ORDER BY id"
    )))


def test_legacy_upgrade_preserves_evidence_and_allows_real_orm_read_upsert():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection)
        before = _legacy_evidence(connection)
        _migration(connection).upgrade()
        with Session(bind=connection) as db:
            rows = list(db.scalars(select(SignalRule).order_by(SignalRule.id)))
            assert [(r.mode, r.quality_tolerance, r.timing_tolerance,
                     r.min_sample_count, r.max_samples, r.is_active) for r in rows] == [
                ("balanced", 12.0, 12.0, 3, 60, 1),
                ("balanced", 12.0, 12.0, 3, 60, 0),
            ]
            assert all(r.same_region == r.same_asset_type == r.same_stage == r.same_action == 1
                       for r in rows)
            # Do not infer executable tolerances/mode from legacy JSON.
            assert get_active_signal_rule(db, 1).id == 1
            assert get_active_signal_rule(db, 2).id is None
            created = upsert_active_signal_rule(db, 3, SignalRuleUpsert(
                rule_name="new ORM rule", mode="expert", quality_tolerance=9,
                same_action=False,
            ), commit=False)
            updated = upsert_active_signal_rule(db, 3, SignalRuleUpsert(
                rule_name="edited ORM rule", quality_tolerance=10,
            ), commit=False)
            assert updated.id == created.id
            assert get_active_signal_rule(db, 3).quality_tolerance == 10
        assert _legacy_evidence(connection) == before
        assert connection.execute(text(
            "SELECT rule_type, config_json FROM signal_rules WHERE portfolio_id=3"
        )).one() == (None, None)
        columns = {c["name"]: c for c in inspect(connection).get_columns("signal_rules")}
        assert all(columns[name]["nullable"] for name in ("rule_type", "enabled", "config_json"))
        assert {i["name"] for i in inspect(connection).get_indexes("signal_rules")} == {
            "ix_signal_rules_portfolio_id", "ix_signal_rules_rule_type", "ix_signal_rules_enabled",
        }
    engine.dispose()


def test_replay_and_non_destructive_downgrade_preserve_explicit_new_values():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection)
        before = _legacy_evidence(connection)
        migration = _migration(connection)
        migration.upgrade()
        connection.execute(text("""UPDATE signal_rules SET mode='expert',
            quality_tolerance=7, is_active=0 WHERE id=1"""))
        migration.upgrade()
        migration.downgrade()
        migration.upgrade()
        assert connection.execute(text(
            "SELECT mode, quality_tolerance, is_active FROM signal_rules WHERE id=1"
        )).one() == ("expert", 7, 0)
        assert _legacy_evidence(connection) == before
    engine.dispose()


def test_interrupted_nullable_active_addition_resumes_without_reactivating_explicit_values():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection)
        # Simulate an ALTER succeeding but process stopping before its backfill.
        connection.execute(text("ALTER TABLE signal_rules ADD COLUMN is_active INTEGER"))
        connection.execute(text("UPDATE signal_rules SET is_active=0 WHERE id=1"))
        _migration(connection).upgrade()
        assert list(connection.execute(text(
            "SELECT id, is_active FROM signal_rules ORDER BY id"
        ))) == [(1, 0), (2, 0)]
        active_column = next(c for c in inspect(connection).get_columns("signal_rules")
                             if c["name"] == "is_active")
        assert active_column["nullable"] is False
    engine.dispose()


@pytest.mark.parametrize("legacy_columns", [False, True])
def test_already_new_or_partially_aligned_schema_never_overwrites_new_columns(legacy_columns):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        if legacy_columns:
            _legacy_schema(connection)
            connection.execute(text(
                "ALTER TABLE signal_rules ADD COLUMN mode VARCHAR(32) NOT NULL DEFAULT 'expert'"
            ))
            connection.execute(text(
                "ALTER TABLE signal_rules ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0"
            ))
        else:
            # One existing installation was built from ORM; its shape is already current.
            connection.execute(text("CREATE TABLE portfolios (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO portfolios VALUES (1)"))
            SignalRule.__table__.create(connection)
            with Session(bind=connection) as db:
                row = SignalRule(portfolio_id=1, rule_name="current", mode="expert", is_active=0,
                                 quality_tolerance=7, timing_tolerance=8)
                db.add(row)
                db.flush()
        migration = _migration(connection)
        migration.upgrade()
        migration.upgrade()
        with Session(bind=connection) as db:
            rule = db.get(SignalRule, 1)
            assert rule.mode == "expert"
            assert rule.is_active == 0  # enabled=1 cannot reactivate an existing new column.
            if not legacy_columns:
                assert (rule.quality_tolerance, rule.timing_tolerance) == (7, 8)
        if not legacy_columns:
            assert "config_json" not in {c["name"] for c in inspect(connection).get_columns("signal_rules")}
    engine.dispose()


def test_real_fresh_alembic_head_supports_signal_orm_insert(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'fresh_migration.db').as_posix()}"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")  # Deliberately no Base.metadata.create_all.
    engine = create_engine(url)
    try:
        with Session(engine) as db:
            portfolio = Portfolio(name="fresh head rule", account_type="sim", asset_scope="mixed",
                                  total_capital=100_000, investable_ratio=1, cash_reserve_ratio=.05,
                                  buy_fee_pct=.00025, sell_fee_pct=.00025, benchmark_code="000300",
                                  default_single_position_pct=.3)
            db.add(portfolio)
            db.flush()
            saved = upsert_active_signal_rule(db, portfolio.id, SignalRuleUpsert(
                rule_name="real migrated schema", quality_tolerance=8,
            ))
            assert saved.id is not None
            assert get_active_signal_rule(db, portfolio.id).quality_tolerance == 8
    finally:
        engine.dispose()
