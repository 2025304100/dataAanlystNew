"""Data-preserving migration contracts, independent of application bootstrap."""
from datetime import datetime
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex

import pytest

pytestmark = pytest.mark.whitebox


ROOT = Path(__file__).resolve().parents[1]


def revision(part):
    path = next((ROOT / "alembic" / "versions").glob(f"*{part}*.py"))
    spec = importlib.util.spec_from_file_location(part, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_revision(connection, part):
    with Operations.context(MigrationContext.configure(connection)):
        revision(part).upgrade()


def test_member_mysql_index_has_generated_current_slot():
    from app.models.portfolio_member import PortfolioMember
    index = next(i for i in PortfolioMember.__table__.indexes if i.name == "idx_portfolio_members_active")
    assert [c.name for c in index.columns] == ["portfolio_id", "symbol_id", "active_member_slot"]
    assert PortfolioMember.__table__.c.active_member_slot.computed is not None
    assert "active_member_slot" in str(CreateIndex(index).compile(dialect=mysql.dialect()))


def test_member_startup_preserves_history_and_rejects_two_current_rows():
    from app.db import init_db
    from app.db.base import Base
    from app.models.portfolio_member import PortfolioMember
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        PortfolioMember.__table__.create(conn)
        conn.exec_driver_sql("DROP INDEX idx_portfolio_members_active")
    # Exercise the real aligner while restricting its model inventory, not its SQL.
    old_metadata, old_refresh = Base.metadata, init_db._refresh_model_metadata
    try:
        metadata = sa.MetaData()
        PortfolioMember.__table__.to_metadata(metadata)
        Base.metadata = metadata
        init_db._refresh_model_metadata = lambda: None
        init_db._auto_align_all_schema(engine)
        init_db._auto_align_all_schema(engine)
    finally:
        Base.metadata, init_db._refresh_model_metadata = old_metadata, old_refresh
    with engine.begin() as conn:
        values = dict(portfolio_id=1, symbol_id=2, effective_from=datetime(2026, 1, 1))
        conn.execute(PortfolioMember.__table__.insert().values(**values, effective_to=datetime(2026, 2, 1), status="archived"))
        conn.execute(PortfolioMember.__table__.insert().values(**values, status="active"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(PortfolioMember.__table__.insert().values(**values, status="active"))
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM portfolio_members").scalar() == 2


def legacy_idempotency(conn):
    conn.exec_driver_sql("""CREATE TABLE idempotency_records (
      idempotency_key VARCHAR(128) PRIMARY KEY, resource_type VARCHAR(64) NOT NULL,
      resource_id VARCHAR(128), request_hash VARCHAR(128), status VARCHAR(32) NOT NULL,
      response_json TEXT, created_at DATETIME NOT NULL, expires_at DATETIME)""")
    conn.exec_driver_sql("CREATE UNIQUE INDEX ix_idempotency_records_idempotency_key ON idempotency_records(idempotency_key)")
    conn.exec_driver_sql("""INSERT INTO idempotency_records VALUES
      ('old-key','order_plan','kept-entity','hash-evidence','SUCCEEDED','{"value":123}',
       '2026-10-24 01:02:03.126864','2026-10-25 01:02:03.126864')""")


def test_0081_nonempty_keeps_all_old_fields_and_maps_new_contract():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        legacy_idempotency(conn)
        before = dict(conn.execute(sa.text("SELECT * FROM idempotency_records")).mappings().one())
        run_revision(conn, "0081_")
        run_revision(conn, "0081_")
        after = dict(conn.execute(sa.text("SELECT * FROM idempotency_records")).mappings().one())
        assert {key: after[key] for key in before} == before
        assert after["id"] == 1
        assert after["entity_type"] == "order_plan"
        assert after["entity_id"] == "kept-entity"
        assert after["expire_at"] == before["expires_at"]
        assert after["executed_at"] == before["created_at"]
        assert after["correlation_id"].startswith("migration-0081:")
        assert sa.inspect(conn).get_pk_constraint("idempotency_records")["constrained_columns"] == ["id"]


def test_0081_does_not_accept_plain_id_as_complete_primary_key_contract():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        legacy_idempotency(conn)
        conn.exec_driver_sql("ALTER TABLE idempotency_records ADD COLUMN id INTEGER")
        run_revision(conn, "0081_")
        assert sa.inspect(conn).get_pk_constraint("idempotency_records")["constrained_columns"] == ["id"]
        assert conn.exec_driver_sql("SELECT id FROM idempotency_records").scalar() is not None


def test_0080_is_frozen_and_does_not_import_runtime_model_inventory(monkeypatch):
    from app.db import init_db
    monkeypatch.setattr(init_db, "_refresh_model_metadata", lambda: pytest.fail("historical migration read live models"))
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        run_revision(conn, "0080_")
        assert sa.inspect(conn).has_table("portfolios")


def test_add_column_compiler_preserves_business_default_and_mysql_precision():
    from app.db.init_db import _sa_type_to_ddl
    from app.models.portfolio import Portfolio
    from app.models.security_status import SecurityStatusDaily
    assert "DEFAULT" in _sa_type_to_ddl(Portfolio.__table__.c.asset_scope, True)
    assert "mixed" in _sa_type_to_ddl(Portfolio.__table__.c.asset_scope, True)
    assert "DATETIME(6)" in _sa_type_to_ddl(SecurityStatusDaily.__table__.c.source_updated_at, True)


def test_bootstrap_table_ddl_explicitly_uses_transactional_engine():
    from sqlalchemy.schema import CreateTable
    from app.models.portfolio_member import PortfolioMember
    assert "ENGINE=InnoDB" in str(CreateTable(PortfolioMember.__table__).compile(dialect=mysql.dialect()))


def test_conflicting_legacy_entity_evidence_stops_before_schema_changes():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        legacy_idempotency(conn)
        conn.exec_driver_sql("ALTER TABLE idempotency_records ADD COLUMN entity_type VARCHAR(32)")
        conn.exec_driver_sql("UPDATE idempotency_records SET entity_type='conflicting'")
        before_columns = [c['name'] for c in sa.inspect(conn).get_columns('idempotency_records')]
        before = conn.exec_driver_sql("SELECT * FROM idempotency_records").all()
        with pytest.raises(RuntimeError, match="conflicting entity_type/resource_type"):
            run_revision(conn, "0081_")
        assert [c['name'] for c in sa.inspect(conn).get_columns('idempotency_records')] == before_columns
        assert conn.exec_driver_sql("SELECT * FROM idempotency_records").all() == before


def test_0080_backfills_explicit_portfolio_default_without_changing_old_fields():
    from app.models.portfolio import Portfolio
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        Portfolio.__table__.create(conn)
        conn.execute(Portfolio.__table__.insert().values(name='kept-portfolio', account_type='simulated',
            total_capital=100000, investable_ratio=0.8, cash_reserve_ratio=0.2))
        for index in sa.inspect(conn).get_indexes('portfolios'):
            if 'asset_scope' in index['column_names']:
                conn.exec_driver_sql(f'DROP INDEX "{index["name"]}"')
        conn.exec_driver_sql("ALTER TABLE portfolios DROP COLUMN asset_scope")
        before = dict(conn.exec_driver_sql("SELECT * FROM portfolios").mappings().one())
        run_revision(conn, "0080_")
        after = dict(conn.exec_driver_sql("SELECT * FROM portfolios").mappings().one())
        assert after['asset_scope'] == 'mixed'
        assert {key: after[key] for key in before} == before


def test_0083_legacy_backtest_required_columns_do_not_block_new_lots():
    from app.db.migration_contracts_v83 import ensure_backtest_legacy_write_columns
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE backtest_trades (
          id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,symbol_id INTEGER NOT NULL,
          entry_date DATE NOT NULL,entry_price FLOAT NOT NULL,quantity FLOAT NOT NULL,
          backtest_run_id INTEGER NOT NULL,trade_date DATE NOT NULL,side VARCHAR(8) NOT NULL,
          price FLOAT NOT NULL,decision_at DATETIME NOT NULL,updated_at DATETIME NOT NULL)""")
        conn.exec_driver_sql("""INSERT INTO backtest_trades VALUES
          (1,7,8,'2026-10-24',12.5,100,7,'2026-10-24','BUY',12.5,
           '2026-10-24 01:02:03.126864','2026-10-24 01:02:03.126864')""")
        before = dict(conn.exec_driver_sql("SELECT * FROM backtest_trades").mappings().one())
        ensure_backtest_legacy_write_columns(conn)
        ensure_backtest_legacy_write_columns(conn)
        assert dict(conn.exec_driver_sql("SELECT * FROM backtest_trades WHERE id=1").mappings().one()) == before
        conn.exec_driver_sql("INSERT INTO backtest_trades(id,run_id,symbol_id,entry_date,entry_price,quantity) VALUES (2,7,8,'2026-10-25',13,100)")


def test_real_startup_creates_schema_without_claiming_unexecuted_migrations(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.db import init_db
    from sqlalchemy.orm import sessionmaker
    url = f"sqlite:///{(tmp_path / 'startup.db').as_posix()}"
    engine = sa.create_engine(url)
    manager = SimpleNamespace(engine=engine, is_sqlite=True, is_mysql=False,
                              session_factory=sessionmaker(engine))
    monkeypatch.setattr(init_db.DatabaseManager, "get", lambda: manager)
    monkeypatch.setattr(init_db.settings, "database_url", url)
    # External/config seeding is outside this schema contract. Every CREATE,
    # ALTER, index check, and actual init_db orchestration still executes.
    for name in ("_migrate_ai_config_json_to_profiles", "_seed_system_scoring_configs",
                 "_seed_factor_definitions", "_seed_factor_runtime_state", "_seed_scheduled_tasks",
                 "_load_akshare_api_config_cache"):
        monkeypatch.setattr(init_db, name, lambda: None)
    init_db.init_db()
    inspector = sa.inspect(engine)
    assert inspector.has_table("portfolios")
    assert inspector.has_table("portfolio_members")
    assert not inspector.has_table("alembic_version"), "Only an executed Alembic migration may claim its revision"
    engine.dispose()


def test_0083_review_status_preserves_historical_evidence_and_allows_unreviewed_orders():
    from app.db.migration_contracts_v83 import ensure_order_review_contract
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE sim_orders (
          id INTEGER PRIMARY KEY,portfolio_id INTEGER NOT NULL,quantity FLOAT NOT NULL,
          review_status VARCHAR(32) NOT NULL DEFAULT 'NONE',note TEXT,
          CONSTRAINT ck_sim_orders_review_status_values CHECK
          (review_status IN ('NONE','PENDING','APPROVED','REJECTED','ESCALATED')))""")
        conn.exec_driver_sql("""INSERT INTO sim_orders VALUES
          (1,7,100,'NONE','原始未审核'),(2,7,200,'APPROVED','原始审批证据'),
          (3,7,300,'REJECTED','原始拒绝证据')""")
        before = conn.exec_driver_sql("SELECT * FROM sim_orders ORDER BY id").all()
        ensure_order_review_contract(conn)
        ensure_order_review_contract(conn)
        assert conn.exec_driver_sql("SELECT * FROM sim_orders ORDER BY id").all() == before
        conn.exec_driver_sql("INSERT INTO sim_orders VALUES (4,7,400,NULL,'无需审核的新手工单')")
        conn.exec_driver_sql("INSERT INTO sim_orders VALUES (5,7,500,'PENDING_REVIEW','等待真实审核')")
        assert conn.exec_driver_sql("SELECT review_status FROM sim_orders WHERE id=4").scalar_one() is None


def test_0083_retired_order_account_keeps_values_and_fk_without_blocking_new_orders():
    from app.db.migration_contracts_v83 import ensure_retired_combination_columns
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE sim_accounts (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO sim_accounts VALUES (17)")
        conn.exec_driver_sql("""CREATE TABLE sim_orders (
            id INTEGER PRIMARY KEY,sim_account_id INTEGER NOT NULL,
            quantity FLOAT NOT NULL,note TEXT,
            FOREIGN KEY(sim_account_id) REFERENCES sim_accounts(id) ON DELETE CASCADE)""")
        conn.exec_driver_sql("INSERT INTO sim_orders VALUES (1,17,100,'原始账户关联')")
        before = conn.exec_driver_sql("SELECT * FROM sim_orders").all()
        before_fk = sa.inspect(conn).get_foreign_keys("sim_orders")
        ensure_retired_combination_columns(conn)
        ensure_retired_combination_columns(conn)
        assert conn.exec_driver_sql("SELECT * FROM sim_orders").all() == before
        assert sa.inspect(conn).get_foreign_keys("sim_orders") == before_fk
        conn.exec_driver_sql("INSERT INTO sim_orders(id,quantity,note) VALUES (2,200,'当前组合账户契约')")
        assert conn.exec_driver_sql("SELECT sim_account_id FROM sim_orders WHERE id=2").scalar_one() is None


@pytest.mark.parametrize("kind", ["portfolio_reviews", "tail_accumulation_snapshots"])
def test_0083_retired_review_and_tail_columns_preserve_old_fields_and_allow_current_orm(kind):
    from app.db.migration_contracts_v83 import ensure_retired_combination_columns
    from app.models.review import Review
    from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
    from datetime import date
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        model = Review if kind == "portfolio_reviews" else TailAccumulationSnapshot
        if kind == "portfolio_reviews":
            conn.exec_driver_sql("CREATE TABLE portfolios (id INTEGER PRIMARY KEY)")
            conn.exec_driver_sql("INSERT INTO portfolios VALUES (7)")
        model.__table__.create(conn)
        extras = {"review_type":"TEXT", "review_date":"TEXT"} if kind == "portfolio_reviews" else {
            "snapshot_date":"TEXT", "accumulation_days":"INTEGER", "accumulation_score":"FLOAT"}
        for name, data_type in extras.items():
            conn.exec_driver_sql(f"ALTER TABLE {kind} ADD COLUMN {name} {data_type} NOT NULL DEFAULT 1")
        if kind == "portfolio_reviews":
            values = dict(portfolio_id=7, start_date=date(2026,10,1), end_date=date(2026,10,24),
                report_snapshot_json='{"原始证据":true}', note="旧复盘", title="旧标题", updated_at=datetime(2026,10,24))
        else:
            values = dict(symbol="600001",trade_date=date(2026,10,24),minute_count=240,tail_minute_count=30,
                day_amount=10000,tail_amount=2000,tail_amount_share=.2,tail_activity_ratio=1.1,
                tail_return=.02,close_location=.8,proxy_score=70,source="fixture",raw_json='{"原始数据":true}')
        conn.execute(model.__table__.insert().values(**values))
        with Operations.context(MigrationContext.configure(conn)) as operations:
            with operations.batch_alter_table(kind, recreate="always") as batch:
                for name in extras:
                    batch.alter_column(name, nullable=False, server_default=None)
                if kind == "portfolio_reviews":
                    batch.alter_column("title", nullable=False)
                    batch.alter_column("updated_at", nullable=False)
        before = conn.exec_driver_sql(f"SELECT * FROM {kind}").all()
        ensure_retired_combination_columns(conn)
        ensure_retired_combination_columns(conn)
        assert conn.exec_driver_sql(f"SELECT * FROM {kind}").all() == before
        if kind == "portfolio_reviews":
            values.update(title=None, updated_at=None, note="当前复盘")
        else:
            values.update(symbol="600002")
        conn.execute(model.__table__.insert().values(**values))
        assert conn.exec_driver_sql(f"SELECT COUNT(*) FROM {kind}").scalar_one() == 2
