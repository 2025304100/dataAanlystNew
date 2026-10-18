"""Public range repair preserves dates and the availability of unchanged bars.

SQLite is real and isolated; only provider SDK I/O is replaced.
MySQL-specific statement checks below are SQL contracts, not live-DB claims.
"""
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import Insert
from sqlalchemy.orm import Session

from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import akshare_registry, market_data, universe_sync

pytestmark = pytest.mark.whitebox


DAY = date(2026, 8, 7)
OLD = datetime(2026, 8, 8, 1, 24, 4)
EM_TAG = "akshare:em:cn-stock:v1:qfq"


@pytest.fixture
def isolated_history(monkeypatch):
    engine = create_engine("sqlite://")
    UniverseSymbol.__table__.create(engine)
    UniverseDailyBar.__table__.create(engine)
    monkeypatch.setattr(akshare_registry, "_config_cache_loaded", True)
    monkeypatch.setattr(akshare_registry, "_config_cache", {
        key: {"enabled": True, "delay_min_ms": 0, "delay_max_ms": 0, "max_retries": 1}
        for key in ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx")
    })
    with Session(engine) as db:
        symbol = UniverseSymbol(symbol="601168", name="Isolated availability", asset_type="stock",
            market="sh", region="cn", is_synced=1, sync_failed=0)
        db.add(symbol); db.flush()
        bar = UniverseDailyBar(universe_symbol_id=symbol.id, trade_date=DAY,
            open=40.13, high=43.29, low=39.44, close=43.07, volume=111038800,
            amount=4656185449, turnover_rate=4.66, source=EM_TAG, created_at=OLD)
        db.add(bar); db.commit()
        yield db, symbol, bar.id
    engine.dispose()


def provider_frame(**overrides):
    raw = {"日期": DAY, "开盘": 40.13, "最高": 43.29, "最低": 39.44,
        "收盘": 43.07, "成交量": 1110388, "成交额": 4656185449, "换手率": 4.66}
    raw.update(overrides)
    return pd.DataFrame([raw])


def run_repair(db, symbol):
    result = universe_sync.repair_one_universe_symbol(db, symbol, DAY, DAY)
    assert result["status"] == "ok" and result["inserted"] == 0 and result["updated"] == 1
    db.expire_all()
    return db.scalar(select(UniverseDailyBar).where(UniverseDailyBar.universe_symbol_id == symbol.id))


def test_changed_price_becomes_available_now_but_same_payload_keeps_its_time(isolated_history, monkeypatch):
    db, symbol, bar_id = isolated_history
    response = provider_frame()
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kwargs: response.copy(deep=True))
    row = run_repair(db, symbol)
    assert row.created_at == OLD  # Observing identical values does not rewrite history.
    response.loc[0, "收盘"] = 43.0
    row = run_repair(db, symbol)
    assert row.created_at > OLD and row.close == 43.0
    corrected_at = row.created_at
    row = run_repair(db, symbol)
    assert row.created_at == corrected_at
    assert row.id == bar_id and row.trade_date == DAY and row.source == EM_TAG


class MySQLStatementCapture:
    """Capture a non-SQLite write at the external DB boundary, never execute it."""

    def __init__(self, isolated_session):
        self.db = isolated_session
        self.writes = []

    def __getattr__(self, name):
        return getattr(self.db, name)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    def execute(self, statement, *args, **kwargs):
        if isinstance(statement, Insert):
            self.writes.append(statement)
            return SimpleNamespace(rowcount=1)
        return self.db.execute(statement, *args, **kwargs)


def test_mysql_public_repair_compares_old_payload_null_safely_before_any_assignment(
    isolated_history, monkeypatch,
):
    db, symbol, _bar_id = isolated_history
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kwargs: provider_frame())
    capture = MySQLStatementCapture(db)
    result = universe_sync.repair_one_universe_symbol(capture, symbol, DAY, DAY)
    assert result["status"] == "ok" and len(capture.writes) == 1
    # This is a compiler contract, not a claim that MySQL was contacted.
    sql = str(capture.writes[0].compile(dialect=mysql.dialect()))
    assignments = sql.split("ON DUPLICATE KEY UPDATE ", 1)[1]
    assert assignments.startswith("created_at = CASE WHEN "), sql
    comparison = assignments.split(" END", 1)[0]
    for field in ("open", "high", "low", "close", "volume", "amount", "turnover_rate", "source"):
        assert f"universe_daily_bars.{field} <=> VALUES({field})" in comparison, sql
    assert "THEN universe_daily_bars.created_at ELSE" in comparison, sql
    assert ", open = VALUES(open)" in assignments, sql
    assert "trade_date =" not in assignments and "universe_symbol_id =" not in assignments


@pytest.mark.parametrize("raw_column,field,present", [
    ("成交量", "volume", 1110388),
    ("成交额", "amount", 4656185449),
    ("换手率", "turnover_rate", 4.66),
])
@pytest.mark.parametrize("transition", ["null_to_value", "value_to_null", "null_to_null"])
def test_nullable_payload_transitions_preserve_availability_truth(
    isolated_history, monkeypatch, raw_column, field, present, transition,
):
    db, symbol, bar_id = isolated_history
    if transition != "value_to_null":
        db.get(UniverseDailyBar, bar_id).__setattr__(field, None)
        db.commit()
    incoming = present if transition == "null_to_value" else None
    response = provider_frame(**{raw_column: incoming})
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kwargs: response.copy(deep=True))
    row = run_repair(db, symbol)
    assert (row.created_at == OLD) if transition == "null_to_null" else (row.created_at > OLD)
    if incoming is None:
        assert getattr(row, field) is None
    else:
        expected = {"volume": 111038800, "amount": 4656185449, "turnover_rate": 4.66}[field]
        assert getattr(row, field) == expected
    available_at = row.created_at
    row = run_repair(db, symbol)
    assert row.created_at == available_at
    assert row.id == bar_id and row.trade_date == DAY


def test_unit_source_marker_change_alone_gets_new_availability_time(isolated_history, monkeypatch):
    db, symbol, bar_id = isolated_history
    db.get(UniverseDailyBar, bar_id).source = "akshare"
    db.commit()
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kwargs: provider_frame())
    row = run_repair(db, symbol)
    assert row.source == EM_TAG and row.created_at > OLD
    available_at = row.created_at
    row = run_repair(db, symbol)
    assert row.created_at == available_at and row.id == bar_id and row.trade_date == DAY


def test_volume_replacement_is_available_now_without_moving_its_trade_date(isolated_history, monkeypatch):
    db, symbol, bar_id = isolated_history
    monkeypatch.setattr(market_data.ak, "stock_zh_a_hist", lambda **_kwargs: provider_frame(成交量=1110389))
    row = run_repair(db, symbol)
    assert row.volume == 111038900 and row.created_at > OLD
    assert row.id == bar_id and row.trade_date == DAY
