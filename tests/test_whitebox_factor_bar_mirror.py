from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("duckdb")

from sqlalchemy.exc import OperationalError

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.factors import bar_mirror
from app.services.factors.bar_mirror import mirror_daily_bars
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def test_mirror_combines_business_and_universe_ids_idempotently(
    db_session, tmp_path
):
    business_symbol = Symbol(
        symbol="600519",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
    )
    universe_symbol = UniverseSymbol(
        symbol="600519",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
        region="cn",
    )
    db_session.add_all([business_symbol, universe_symbol])
    db_session.flush()
    db_session.add_all(
        [
            DailyBar(
                symbol_id=business_symbol.id,
                trade_date=date(2026, 7, 10),
                open=1400,
                high=1420,
                low=1390,
                close=1411,
                volume=100,
                amount=141100,
                turnover_rate=0.5,
                source="business-test",
            ),
            UniverseDailyBar(
                universe_symbol_id=universe_symbol.id,
                trade_date=date(2026, 7, 10),
                open=1400,
                high=1420,
                low=1390,
                close=1410,
                volume=100,
                amount=141000,
                turnover_rate=0.5,
                source="universe-test",
            ),
        ]
    )
    db_session.commit()

    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    first = mirror_daily_bars(db_session, warehouse=warehouse, batch_size=1)
    second = mirror_daily_bars(db_session, warehouse=warehouse, batch_size=1)

    assert first.rows_written == 2
    assert first.metadata_rows == 1
    assert second.rows_written == 0
    assert second.metadata_rows == 0
    assert warehouse.health().raw_daily_bars == 1

    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT business_symbol_id, universe_symbol_id, close, "
            "source_origin FROM raw_daily_bars WHERE symbol = '600519'"
        ).fetchone()

    assert row[0] == business_symbol.id
    assert row[1] == universe_symbol.id
    assert row[2] == 1411
    assert row[3] == "daily_bars"
    with warehouse.connection(read_only=True) as conn:
        metadata = conn.execute(
            "SELECT asset_type, market, region FROM raw_asset_universe "
            "WHERE symbol = '600519'"
        ).fetchone()
    assert metadata == ("stock", "sh", "cn")


def test_mirror_validates_arguments(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")

    with pytest.raises(ValueError, match="batch_size"):
        mirror_daily_bars(db_session, warehouse=warehouse, batch_size=0)
    with pytest.raises(ValueError, match="one source"):
        mirror_daily_bars(
            db_session,
            warehouse=warehouse,
            include_business=False,
            include_universe=False,
        )
    with pytest.raises(ValueError, match="start_date"):
        mirror_daily_bars(
            db_session,
            warehouse=warehouse,
            start_date=date(2026, 7, 2),
            end_date=date(2026, 7, 1),
        )


def test_mirror_excludes_non_cn_stock_assets(db_session, tmp_path):
    cn_stock = UniverseSymbol(
        symbol="600519",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
        region="cn",
    )
    cn_etf = UniverseSymbol(
        symbol="510300",
        name="沪深300ETF",
        asset_type="etf",
        market="sh",
        region="cn",
    )
    us_stock = UniverseSymbol(
        symbol="AAPL",
        name="Apple",
        asset_type="stock",
        market="us",
        region="us",
    )
    db_session.add_all([cn_stock, cn_etf, us_stock])
    db_session.flush()
    db_session.add_all(
        [
            UniverseDailyBar(
                universe_symbol_id=item.id,
                trade_date=date(2026, 7, 10),
                close=10,
            )
            for item in (cn_stock, cn_etf, us_stock)
        ]
    )
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")

    result = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        start_date=date(2026, 7, 10),
        end_date=date(2026, 7, 10),
        include_business=False,
    )
    repeated = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        start_date=date(2026, 7, 10),
        end_date=date(2026, 7, 10),
        include_business=False,
    )

    assert result.universe_rows == 1
    assert repeated.universe_rows == 1
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(
            "SELECT symbol FROM raw_daily_bars"
        ).fetchall() == [("600519",)]


def test_date_range_watermark_does_not_skip_full_incremental_sync(
    db_session, tmp_path
):
    symbol = Symbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all(
        [
            DailyBar(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 9),
                open=10,
                high=11,
                low=9,
                close=10,
                source="test",
            ),
            DailyBar(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 10),
                open=11,
                high=12,
                low=10,
                close=11,
                source="test",
            ),
        ]
    )
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")

    ranged = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        start_date=date(2026, 7, 10),
        end_date=date(2026, 7, 10),
        include_universe=False,
    )
    full = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        include_universe=False,
    )

    assert ranged.rows_written == 1
    assert full.rows_written == 2
    assert warehouse.health().raw_daily_bars == 2


def test_mirror_cancel_stops_between_batches_and_releases_duckdb(
    db_session, tmp_path, monkeypatch
):
    symbol = Symbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all(
        [
            DailyBar(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, day),
                open=10 + day,
                high=11 + day,
                low=9 + day,
                close=10 + day,
                source="test",
            )
            for day in (9, 10)
        ]
    )
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    cancelled = False
    original_upsert = warehouse.upsert_daily_bars

    def cancel_after_first_batch(*args, **kwargs):
        nonlocal cancelled
        written = original_upsert(*args, **kwargs)
        cancelled = True
        return written

    monkeypatch.setattr(
        warehouse, "upsert_daily_bars", cancel_after_first_batch
    )

    result = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        batch_size=1,
        include_universe=False,
        should_cancel=lambda: cancelled,
    )

    assert result.business_rows == 1
    assert warehouse.health().available is True
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM raw_daily_bars"
        ).fetchone()[0] == 1


def test_mirror_pre_cancel_does_not_open_warehouse(db_session, tmp_path):
    path = tmp_path / "cancelled.duckdb"

    result = mirror_daily_bars(
        db_session,
        warehouse=FactorWarehouse(path),
        include_universe=False,
        should_cancel=lambda: True,
    )

    assert result.rows_written == 0
    assert path.exists() is False


def test_short_read_retries_dropped_connection_and_closes_sessions(
    db_session, monkeypatch
):
    sessions = []

    class FakeReadSession:
        def __init__(self):
            self.closed = False
            self.invalidated = False

        def invalidate(self):
            self.invalidated = True

        def close(self):
            self.closed = True

    def fake_sessionmaker(**_kwargs):
        def factory():
            session = FakeReadSession()
            sessions.append(session)
            return session

        return factory

    monkeypatch.setattr(bar_mirror, "sessionmaker", fake_sessionmaker)

    def operation(_read_db):
        if len(sessions) == 1:
            raise OperationalError(
                "SELECT 1", {}, Exception("MySQL server has gone away")
            )
        return "ok"

    assert bar_mirror._read_with_retry(db_session, operation) == "ok"
    assert len(sessions) == 2
    assert sessions[0].invalidated is True
    assert all(session.closed for session in sessions)


def test_disconnect_classifier_rejects_non_connection_errors():
    dropped = OperationalError(
        "SELECT 1", {}, Exception(2006, "MySQL server has gone away")
    )
    invalid_sql = OperationalError(
        "SELECT broken", {}, Exception(1064, "SQL syntax error")
    )

    assert bar_mirror._is_disconnect_error(dropped) is True
    assert bar_mirror._is_disconnect_error(invalid_sql) is False


def test_bounded_range_resumes_after_failed_duckdb_batch(
    db_session, tmp_path, monkeypatch
):
    symbols = [
        UniverseSymbol(
            symbol=f"00000{index}",
            name=f"测试{index}",
            asset_type="stock",
            market="sz",
            region="cn",
        )
        for index in (1, 2)
    ]
    db_session.add_all(symbols)
    db_session.flush()
    db_session.add_all(
        [
            UniverseDailyBar(
                universe_symbol_id=symbol.id,
                trade_date=date(2026, 7, 10),
                close=10 + index,
            )
            for index, symbol in enumerate(symbols)
        ]
    )
    db_session.commit()

    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    original_upsert = warehouse.upsert_daily_bars
    calls = 0

    def fail_second_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected DuckDB write failure")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(
        warehouse, "upsert_daily_bars", fail_second_batch
    )
    with pytest.raises(RuntimeError, match="injected"):
        mirror_daily_bars(
            db_session,
            warehouse=warehouse,
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 10),
            batch_size=1,
            include_business=False,
        )

    monkeypatch.setattr(
        warehouse, "upsert_daily_bars", original_upsert
    )
    resumed = mirror_daily_bars(
        db_session,
        warehouse=warehouse,
        start_date=date(2026, 7, 10),
        end_date=date(2026, 7, 10),
        batch_size=1,
        include_business=False,
    )

    assert resumed.universe_rows == 1
    assert warehouse.health().raw_daily_bars == 2
    with warehouse.connection(read_only=True) as conn:
        checkpoints = conn.execute(
            "SELECT COUNT(*) FROM warehouse_watermarks "
            "WHERE source_key LIKE '%|symbols:%'"
        ).fetchone()[0]
    assert checkpoints == 0
