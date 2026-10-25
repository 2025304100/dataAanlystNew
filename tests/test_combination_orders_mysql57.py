"""Real O06/O04 transaction contract. Opt in with MYSQL57_TEST_URL.

The URL must target a dedicated codex_combination_* database. Tables are
created as InnoDB explicitly and existing tables are never dropped/truncated.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import MetaData, create_engine, func, inspect, select, text
from sqlalchemy.schema import AddConstraint
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.init_db import _refresh_model_metadata
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.sim_accounts import cash_balance, ensure_sim_account_seed, place_sim_order

pytestmark = pytest.mark.whitebox


@pytest.fixture(scope="module")
def mysql_engine():
    raw = os.environ.get("MYSQL57_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_TEST_URL is required for real MySQL transaction tests")
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or not (url.database or "").startswith("codex_combination_"):
        pytest.fail("MYSQL57_TEST_URL must target a dedicated codex_combination_* MySQL database")
    engine = create_engine(url, hide_parameters=True)
    _refresh_model_metadata()
    names = {"portfolios", "positions", "symbols", "portfolio_members", "portfolio_rules",
             "cash_ledger", "sim_orders", "sim_trades", "daily_bars", "notification_policies"}
    # Include FK parents; copy metadata so test-only engine choices do not
    # mutate production mappings or another test's schema.
    while True:
        parents = {fk.column.table.name for name in names for fk in Base.metadata.tables[name].foreign_keys}
        if parents <= names:
            break
        names |= parents
    metadata = MetaData()
    for name in names:
        table = Base.metadata.tables[name].to_metadata(metadata)
        table.dialect_options["mysql"]["engine"] = "InnoDB"
        table.dialect_options["mysql"]["charset"] = "utf8mb4"
    metadata.create_all(engine)
    with engine.connect() as conn:
        engines = conn.execute(text("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"))
        choices = dict(engines.all())
        assert all(choices[name] == "InnoDB" for name in names), choices
        assert conn.scalar(text("SELECT VERSION()")), "server version must be recorded by the runner"
    yield engine
    engine.dispose()


def seed_account(engine):
    with Session(engine) as db:
        token = uuid.uuid4().hex[:10]
        p = Portfolio(name=f"O06-{token}", account_type="simulated", total_capital=100000,
                      investable_ratio=.8, cash_reserve_ratio=.2)
        s = Symbol(symbol=f"Q{token}", name="Order regression", asset_type="stock", market="cn")
        db.add_all([p, s]); db.flush()
        ensure_sim_account_seed(db, p)
        db.add(Position(portfolio_id=p.id, symbol_id=s.id, quantity=200, avg_cost=10,
                        latest_price=10, market_value=2000, position_pct=.02, asset_type="stock"))
        for days in (1, 2):
            db.add(DailyBar(symbol_id=s.id, trade_date=date.today()-timedelta(days=days),
                            open=10, high=10.2, low=9.8, close=10, volume=100000))
        db.commit()
        return p.id, s.id


def account_facts(db, portfolio_id):
    return (
        cash_balance(db, portfolio_id),
        tuple(db.execute(select(Position.symbol_id, Position.quantity, Position.avg_cost)
                         .where(Position.portfolio_id == portfolio_id)).all()),
        db.scalar(select(func.count()).select_from(SimOrder).where(SimOrder.portfolio_id == portfolio_id)),
        db.scalar(select(func.count()).select_from(SimTrade).where(SimTrade.portfolio_id == portfolio_id)),
        db.scalar(select(func.count()).select_from(CashLedger).where(CashLedger.portfolio_id == portfolio_id)),
    )


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "READ COMMITTED"])
def test_o06_buy_observes_block_committed_after_its_initial_read(mysql_engine, isolation):
    portfolio_id, symbol_id = seed_account(mysql_engine)
    with Session(mysql_engine.execution_options(isolation_level=isolation)) as a:
        p, s = a.get(Portfolio, portfolio_id), a.get(Symbol, symbol_id)
        assert p.status_reconciliation == "READY"
        before = account_facts(a, portfolio_id)
        with Session(mysql_engine) as b:
            blocked = b.get(Portfolio, portfolio_id)
            blocked.status_reconciliation = "RECONCILIATION_BLOCKED"
            b.commit()
        try:
            with pytest.raises(HTTPException) as exc:
                place_sim_order(a, p, s, "buy", 100, price=10, enforce_rules=True)
            assert exc.value.status_code == 409
            assert exc.value.detail["error_code"] == "COMPOSITE_BLOCK_LEVEL_TOO_HIGH"
            assert account_facts(a, portfolio_id) == before
        finally:
            a.rollback()
    with Session(mysql_engine) as verify:
        assert account_facts(verify, portfolio_id) == before


def test_o04_blocked_account_can_sell_a_real_position_under_market_rules(mysql_engine):
    portfolio_id, symbol_id = seed_account(mysql_engine)
    with Session(mysql_engine) as db:
        p, s = db.get(Portfolio, portfolio_id), db.get(Symbol, symbol_id)
        p.status_reconciliation = "RECONCILIATION_BLOCKED"; db.commit()
        order, trade = place_sim_order(db, p, s, "sell", 100, price=10, enforce_rules=True, apply_fees=False)
        db.commit()
        assert order.filled_quantity == trade.quantity == 100
        assert cash_balance(db, portfolio_id) == 101000
        assert db.scalar(select(Position.quantity).where(Position.portfolio_id == portfolio_id)) == 100


def test_orders_fail_closed_on_nontransactional_cash_ledger(mysql_engine):
    portfolio_id, symbol_id = seed_account(mysql_engine)
    # MyISAM cannot retain FK constraints. Capture and restore this dedicated
    # test table's FK along with its engine; every existing row stays intact.
    metadata = MetaData()
    metadata.reflect(mysql_engine, only=["cash_ledger"])
    table = metadata.tables["cash_ledger"]
    constraints = list(table.foreign_key_constraints)
    with Session(mysql_engine) as db:
        before = account_facts(db, portfolio_id)
    try:
        with mysql_engine.begin() as conn:
            for fk in constraints:
                conn.execute(text(f"ALTER TABLE cash_ledger DROP FOREIGN KEY `{fk.name}`"))
            conn.execute(text("ALTER TABLE cash_ledger ENGINE=MyISAM"))
        with Session(mysql_engine) as db:
            p, s = db.get(Portfolio, portfolio_id), db.get(Symbol, symbol_id)
            with pytest.raises(HTTPException) as exc:
                place_sim_order(db, p, s, "buy", 100, price=10, enforce_rules=True)
            assert exc.value.status_code == 503
            assert exc.value.detail["error_code"] == "ORDER_STORAGE_ENGINE_UNSAFE"
            assert account_facts(db, portfolio_id) == before
            db.rollback()
    finally:
        with mysql_engine.begin() as conn:
            conn.execute(text("ALTER TABLE cash_ledger ENGINE=InnoDB"))
            for fk in constraints:
                conn.execute(AddConstraint(fk))
    assert inspect(mysql_engine).get_table_options("cash_ledger")["mysql_engine"] == "InnoDB"


@pytest.mark.parametrize("side,first_quantity,second_quantity,expected_cash,expected_quantity", [
    ("buy", 6000, 6000, 40000, 6200),
    ("sell", 100, 200, 101000, 100),
])
def test_o09_locked_order_uses_latest_cash_and_position(mysql_engine, side, first_quantity, second_quantity, expected_cash, expected_quantity):
    portfolio_id, symbol_id = seed_account(mysql_engine)
    with Session(mysql_engine.execution_options(isolation_level="REPEATABLE READ")) as a:
        p, s = a.get(Portfolio, portfolio_id), a.get(Symbol, symbol_id)
        account_facts(a, portfolio_id)  # Establish the same pre-lock read view as HTTP.
        with Session(mysql_engine) as b:
            place_sim_order(b, b.get(Portfolio, portfolio_id), b.get(Symbol, symbol_id),
                            side, first_quantity, price=10, enforce_rules=True, apply_fees=False)
            b.commit()
        try:
            with pytest.raises(HTTPException) as exc:
                place_sim_order(a, p, s, side, second_quantity, price=10, enforce_rules=True, apply_fees=False)
            assert exc.value.status_code == 400
        finally:
            a.rollback()
    with Session(mysql_engine) as verify:
        facts = account_facts(verify, portfolio_id)
        assert facts[0] == expected_cash
        assert facts[1][0][1] == expected_quantity
        assert facts[2:] == (1, 1, 2)


def test_o09_current_position_does_not_make_new_t0_buys_sellable(mysql_engine):
    portfolio_id, symbol_id = seed_account(mysql_engine)
    with Session(mysql_engine.execution_options(isolation_level="REPEATABLE READ")) as a:
        p, s = a.get(Portfolio, portfolio_id), a.get(Symbol, symbol_id)
        account_facts(a, portfolio_id)
        with Session(mysql_engine) as b:
            place_sim_order(b, b.get(Portfolio, portfolio_id), b.get(Symbol, symbol_id),
                            "buy", 100, price=10, enforce_rules=True, apply_fees=False)
            b.commit()
        try:
            with pytest.raises(HTTPException) as exc:
                place_sim_order(a, p, s, "sell", 300, price=10, enforce_rules=True, apply_fees=False)
            assert exc.value.status_code == 400
            assert "T+1" in str(exc.value.detail)
        finally:
            a.rollback()
    with Session(mysql_engine) as verify:
        facts = account_facts(verify, portfolio_id)
        assert facts[0] == 99000
        assert facts[1][0][1] == 300
        assert facts[2:] == (1, 1, 2)
