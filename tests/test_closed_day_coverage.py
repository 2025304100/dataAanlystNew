from datetime import date

import pytest

from app.models.daily_bar import DailyBar
from app.models.market_data import MarketCalendar
from app.models.symbol import Symbol
from app.services.external_data_sync_task import get_external_data_gaps
from app.services.factors.formula_catalog import _query_field_stats
from app.services.factors.store import FactorWarehouse
from app.services.market_session import range_is_cn_closed


def test_coverage_excludes_closed_days_but_keeps_real_missing_trading_day(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.market_session.read_cn_calendar", lambda: [(date(2026, 10, 1), 0)])
    warehouse = FactorWarehouse(tmp_path / "calendar.duckdb")
    warehouse.initialize()
    dates = ["2026-09-30", "2026-10-01", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-12"]
    with warehouse.connection() as conn:
        conn.execute("""INSERT INTO raw_asset_universe VALUES
            ('000001','stock','stock','sz','cn',TRUE,'test',1,CURRENT_TIMESTAMP)""")
        conn.executemany("""INSERT INTO raw_daily_bars
            (symbol,trade_date,adjust,close,source,source_origin,source_row_id,ingested_at,batch_id)
            VALUES ('000001',?,'qfq',10,'test','test',?,CURRENT_TIMESTAMP,'b')""",
            [(day, i) for i, day in enumerate(dates)])
        conn.executemany("""INSERT INTO raw_valuation_snapshots
            (symbol,trade_date,pe_ttm,pb,source,ingested_at,batch_id)
            VALUES ('000001',?,10,1,'test',CURRENT_TIMESTAMP,'v')""", [(day,) for day in dates[:-1]])
        # A financial report published on a holiday is still valid evidence
        # for the following trading day, and must not be discarded.
        conn.execute("""INSERT INTO raw_financial_reports
            (symbol,report_period,announcement_date,report_type,roe_ttm,source,ingested_at,batch_id)
            VALUES ('000001','2026-06-30','2026-10-01','quarter',12,'test',CURRENT_TIMESTAMP,'f')""")
    stats = _query_field_stats(warehouse)
    pe = stats["pe_ttm"]
    assert pe["table_rows"] == pe["nonnull_rows"] == 3
    assert pe["expected_rows"] == 4
    assert pe["covered_rows"] == 3
    assert pe["missing_ratio"] == pytest.approx(.25)
    assert pe["latest_daily_coverage"] == 0
    assert pe["continuity_days"] == 0
    assert pe["max_consecutive_gap_days"] == 1
    assert stats["roe_ttm"]["nonnull_rows"] == 1
    assert stats["roe_ttm"]["latest_daily_coverage"] == 1


@pytest.mark.parametrize("dataset", ["fundamental", "capital_flow", "financial"])
def test_gap_scan_does_not_repair_holidays_or_weekends(db_session, dataset):
    symbol = Symbol(symbol="000001", name="stock", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    for day in [date(2026, 10, 1), date(2026, 10, 8), date(2026, 10, 10)]:
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=10, high=10, low=10, close=10))
    db_session.add(MarketCalendar(trade_date=date(2026, 10, 1), is_trading_day=0))
    db_session.commit()
    gaps = get_external_data_gaps(db_session, dataset=dataset,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 10))
    assert gaps["total_missing"] == 1
    assert gaps["gaps"][0]["trade_date"] == date(2026, 10, 8)


def test_backfill_only_skips_proven_closed_ranges(db_session):
    db_session.add_all([MarketCalendar(trade_date=date(2026, 10, day), is_trading_day=0)
        for day in range(1, 8)])
    db_session.commit()
    assert range_is_cn_closed(db_session, date(2026, 10, 1), date(2026, 10, 7))
    assert range_is_cn_closed(db_session, date(2026, 10, 17), date(2026, 10, 18))
    assert not range_is_cn_closed(db_session, date(2026, 10, 1), date(2026, 10, 8))
    assert not range_is_cn_closed(db_session, date(2000, 1, 3), date(2000, 1, 4))
