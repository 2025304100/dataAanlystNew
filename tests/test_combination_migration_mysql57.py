"""Real MySQL 5.7 migration contracts; only explicitly named isolated databases.

MYSQL57_TEST_URL must name an empty codex_combination_* database. Additional
test databases use the same validated prefix and are retained as evidence.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import re
from threading import Barrier
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.whitebox


@pytest.fixture(scope="module")
def isolated_url():
    raw = os.environ.get("MYSQL57_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_TEST_URL is required for the real MySQL 5.7 lane")
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or not re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", url.database or ""):
        pytest.fail("Refusing a non-isolated migration database")
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT VERSION()").scalar().startswith("5.7."), "This lane requires MySQL 5.7"
        assert conn.exec_driver_sql("SELECT DATABASE()").scalar() == url.database
        assert not sa.inspect(conn).get_table_names(), "Supply a fresh empty isolated database"
    engine.dispose()
    return url


def upgrade(url, target="head"):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    prior = os.environ.get("ALEMBIC_DATABASE_URL")
    try:
        os.environ["ALEMBIC_DATABASE_URL"] = url.render_as_string(hide_password=False)
        command.upgrade(cfg, target)
    finally:
        if prior is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = prior


@pytest.fixture(scope="module")
def migrated_engine(isolated_url):
    upgrade(isolated_url)
    engine = sa.create_engine(isolated_url)
    yield engine
    engine.dispose()


def sibling_database(base, label):
    name = base.database[:44] + "_" + label[:8] + "_" + uuid.uuid4().hex[:6]
    assert re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", name) and len(name) <= 64
    admin = sa.create_engine(base._replace(database=None))
    with admin.begin() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    admin.dispose()
    print("retained_test_database", name)
    return base.set(database=name)


def test_pure_migration_build_is_transactional_and_supports_orm_writes(migrated_engine):
    from app.models.portfolio import Portfolio
    from app.models.symbol import Symbol
    from app.services.idempotency import IdempotencyService
    with migrated_engine.connect() as conn:
        assert not conn.exec_driver_sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND ENGINE<>'InnoDB'").all()
        assert "active_member_slot" in {c["name"] for c in sa.inspect(conn).get_columns("portfolio_members")}
        assert next(i for i in sa.inspect(conn).get_indexes("scores") if i["name"] == "idx_score_fmr_sym_dt")["column_names"] == ["factor_model_run_id", "symbol_id", "trade_date", "factor_data_cutoff_at", "id"]
    with Session(migrated_engine) as db:
        p = Portfolio(name="migration-contract", account_type="simulated", total_capital=100000,
                      investable_ratio=0.8, cash_reserve_ratio=0.2)
        s = Symbol(symbol="MIGR000001", name="migration-contract", asset_type="stock", market="CN")
        db.add_all([p, s])
        db.commit()
        record = IdempotencyService.record_response(db, "mysql-contract", "known-hash", {"accepted": True},
            entity_type="order_plan", entity_id="kept-plan", portfolio_id=p.id, auto_ttl_hours=None)
        db.commit()
        assert record.id is not None and record.resource_type == "order_plan" and record.resource_id == "kept-plan"
        replay = IdempotencyService.try_acquire_or_get(db, "mysql-contract", "known-hash")
        assert replay.status == "REPLAY" and replay.replay_response == {"accepted": True}
        assert IdempotencyService.try_acquire_or_get(db, "mysql-contract", "other-hash").status == "CONFLICT"


def test_current_members_unique_under_concurrency_and_after_restart(migrated_engine):
    from app.db.init_db import _auto_align_all_schema
    from app.models.portfolio_member import PortfolioMember
    with migrated_engine.connect() as conn:
        p = conn.exec_driver_sql("SELECT id FROM portfolios WHERE name='migration-contract'").scalar_one()
        s = conn.exec_driver_sql("SELECT id FROM symbols WHERE symbol='MIGR000001'").scalar_one()
    barrier = Barrier(8)
    def insert_current(_):
        with Session(migrated_engine) as db:
            barrier.wait(timeout=20)
            db.add(PortfolioMember(portfolio_id=p, symbol_id=s, execution_mode="auto"))
            try:
                db.commit()
                return "created"
            except sa.exc.IntegrityError:
                db.rollback()
                return "duplicate"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(insert_current, range(8)))
    assert results.count("created") == 1
    with migrated_engine.begin() as conn:
        conn.execute(sa.text("UPDATE portfolio_members SET status='archived',effective_to=:now WHERE portfolio_id=:p AND symbol_id=:s"),
                     {"now": datetime(2026, 10, 25, 12), "p": p, "s": s})
    _auto_align_all_schema(migrated_engine)
    _auto_align_all_schema(migrated_engine)
    with Session(migrated_engine) as db:
        db.add(PortfolioMember(portfolio_id=p, symbol_id=s))
        db.commit()
    with migrated_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM portfolio_members WHERE portfolio_id=:p AND symbol_id=:s"), {"p": p, "s": s}).scalar_one() == 2


def test_nonempty_legacy_and_missing_primary_key_resume_without_lost_fields(isolated_url):
    from app.db.migration_contracts_v83 import ensure_idempotency_contract
    url = sibling_database(isolated_url, "legacy")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TABLE idempotency_records (
          idempotency_key VARCHAR(128) PRIMARY KEY,resource_type VARCHAR(64) NOT NULL,
          resource_id VARCHAR(128),request_hash VARCHAR(128),status VARCHAR(32) NOT NULL,
          response_json TEXT,created_at DATETIME(6) NOT NULL,expires_at DATETIME(6)) ENGINE=InnoDB""")
        conn.exec_driver_sql("""INSERT INTO idempotency_records VALUES
          ('kept','order_plan','entity-7','hash-7','SUCCEEDED','{"n":7}',
          '2026-10-24 01:02:03.126864','2026-10-25 01:02:03.126864')""")
        before = dict(conn.exec_driver_sql("SELECT * FROM idempotency_records").mappings().one())
        # Reproduce a stopped older migration after DROP PK, without deletion.
        conn.exec_driver_sql("ALTER TABLE idempotency_records DROP PRIMARY KEY")
        ensure_idempotency_contract(conn)
        ensure_idempotency_contract(conn)
        after = dict(conn.exec_driver_sql("SELECT * FROM idempotency_records").mappings().one())
        assert {k: after[k] for k in before} == before
        assert after["entity_id"] == "entity-7"
        assert after["executed_at"] == before["created_at"]
        assert after["expire_at"] == before["expires_at"]
        assert after["id"] > 0
    engine.dispose()


def test_metadata_first_upgrade_matches_migration_contracts(isolated_url):
    from app.db.base import Base
    import app.models  # noqa: F401
    url = sibling_database(isolated_url, "metadata")
    engine = sa.create_engine(url)
    Base.metadata.create_all(engine)
    upgrade(url)
    with engine.connect() as conn:
        assert not conn.exec_driver_sql("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND ENGINE<>'InnoDB'").all()
        index = next(i for i in sa.inspect(conn).get_indexes("portfolio_members") if i["name"] == "idx_portfolio_members_active")
        assert index["unique"] and index["column_names"] == ["portfolio_id", "symbol_id", "active_member_slot"]
        assert sa.inspect(conn).get_pk_constraint("idempotency_records")["constrained_columns"] == ["id"]
    engine.dispose()


def test_applied_0082_receives_0083_compensation_without_changing_member_history(isolated_url):
    from app.models.portfolio import Portfolio
    from app.models.symbol import Symbol
    from app.models.sim_account import SimOrder
    url = sibling_database(isolated_url, "old82")
    upgrade(url, "wps_0024_082_status_dt6")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        p = conn.execute(Portfolio.__table__.insert().values(name="kept-82", account_type="simulated",
            total_capital=100000, investable_ratio=0.8, cash_reserve_ratio=0.2)).inserted_primary_key[0]
        s = conn.execute(Symbol.__table__.insert().values(symbol="LEGACY82", name="kept-symbol",
            asset_type="stock", market="CN")).inserted_primary_key[0]
        # Reproduce the old 0032 review contract while retaining known reviews.
        conn.exec_driver_sql("ALTER TABLE sim_orders MODIFY review_status VARCHAR(32) NOT NULL DEFAULT '''NONE'''")
        conn.exec_driver_sql("ALTER TABLE sim_orders MODIFY sim_account_id INTEGER NOT NULL")
        legacy_accounts = sa.Table("sim_accounts", sa.MetaData(), autoload_with=conn)
        account_id = conn.execute(legacy_accounts.insert().values(name="kept-real-legacy-account", portfolio_id=p,
            initial_capital=100000,current_capital=100000,cash=100000,status="active",
            created_at=datetime(2026,10,24),updated_at=datetime(2026,10,24))).inserted_primary_key[0]
        before_accounts = conn.exec_driver_sql("SELECT * FROM sim_accounts ORDER BY id").all()
        legacy_orders = sa.Table("sim_orders", sa.MetaData(), autoload_with=conn)
        for review in ("NONE", "APPROVED", "REJECTED"):
            conn.execute(legacy_orders.insert().values(sim_account_id=account_id,portfolio_id=p, symbol_id=s,
                side="buy",order_type="market",quantity=100,submitted_price=10,review_status=review,
                status="filled",filled_quantity=100,filled_price=10,filled_amount=1000,fee=0,created_at=datetime(2026,10,24),
                note=f"kept original review {review}"))
        before_orders = conn.exec_driver_sql("SELECT * FROM sim_orders ORDER BY id").all()
        # Emulate the older drop-only 0079 as found on a database at 0082.
        conn.exec_driver_sql("DROP INDEX idx_portfolio_members_active ON portfolio_members")
        conn.exec_driver_sql("ALTER TABLE portfolio_members DROP COLUMN active_member_slot")
        sql = sa.text("""INSERT INTO portfolio_members
          (portfolio_id,symbol_id,status,execution_mode,source_type,effective_from,effective_to,
           manual_lock,priority,note,created_at) VALUES
          (:p,:s,:status,'manual','manual',:start,:end,0,3,:note,:start)""")
        conn.execute(sql, [dict(p=p,s=s,status="archived",start=datetime(2026,1,1),end=datetime(2026,2,1),note="kept historical text"),
                           dict(p=p,s=s,status="active",start=datetime(2026,2,1),end=None,note="kept current text")])
        before = [dict(r) for r in conn.exec_driver_sql("SELECT * FROM portfolio_members ORDER BY id").mappings()]
    upgrade(url)
    with engine.begin() as conn:
        after = [dict(r) for r in conn.exec_driver_sql("SELECT * FROM portfolio_members ORDER BY id").mappings()]
        assert len(after) == len(before)
        assert [{key:r[key] for key in before[0]} for r in after] == before
        assert [r['active_member_slot'] for r in after] == [None, 1]
        assert conn.exec_driver_sql("SELECT * FROM sim_orders ORDER BY id").all() == before_orders
        assert conn.exec_driver_sql("SELECT * FROM sim_accounts ORDER BY id").all() == before_accounts
        account_fk = next(fk for fk in sa.inspect(conn).get_foreign_keys("sim_orders") if fk['constrained_columns'] == ['sim_account_id'])
        assert account_fk['referred_table'] == 'sim_accounts'
        review_column = next(c for c in sa.inspect(conn).get_columns("sim_orders") if c["name"] == "review_status")
        assert review_column["nullable"] and review_column["default"] is None
        inserted = conn.execute(SimOrder.__table__.insert().values(portfolio_id=p, symbol_id=s,
            side="buy", quantity=100, submitted_price=10, review_status=None)).inserted_primary_key[0]
        assert conn.execute(sa.text("SELECT review_status FROM sim_orders WHERE id=:id"), {"id": inserted}).scalar_one() is None
        assert conn.execute(sa.text("SELECT sim_account_id FROM sim_orders WHERE id=:id"), {"id": inserted}).scalar_one() is None
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "wps_0024_085_legacy_dates"
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sql, dict(p=p,s=s,status="active",start=datetime(2026,3,1),end=None,note="duplicate must fail"))
    engine.dispose()
