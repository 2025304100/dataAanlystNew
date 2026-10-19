"""2026-10-14 data-center input regression contracts.

These tests never use the production SQL database, live DuckDB, or providers.
Previously reproduced defects must pass ordinary assertions after repair.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.models.etf_indicator import EtfIndicator
from app.models.data_quality_snapshot import DataQualitySnapshot
from app.models.financial_report import StockFinancialReport
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.services.factors.data_sync import mirror_factor_inputs
from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.formula_catalog import _coverage_summary, _query_field_stats
from app.services.factors.store import FactorWarehouse
from app.services.factors.tail_proxy import calculate_tail_proxy


pytestmark = pytest.mark.whitebox


def _warehouse(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "audit.duckdb")
    warehouse.initialize()
    with warehouse.connection() as conn:
        conn.execute("""
            INSERT INTO raw_asset_universe VALUES
                ('000001', 'stock', 'stock', 'sz', 'cn', TRUE, 'test', 1, CURRENT_TIMESTAMP),
                ('510300', 'etf', 'etf', 'sh', 'cn', TRUE, 'test', 2, CURRENT_TIMESTAMP)
        """)
        conn.execute("""
            INSERT INTO raw_daily_bars
                (symbol,trade_date,adjust,close,amount,source,source_origin,source_row_id,ingested_at,batch_id)
            VALUES
                ('000001','2026-10-13','qfq',10,1000,'test','test',1,CURRENT_TIMESTAMP,'b'),
                ('510300','2026-10-13','qfq',4,1000,'test','test',2,CURRENT_TIMESTAMP,'b')
        """)
    return warehouse


def _mirror(db, warehouse, **selected):
    flags = dict(include_valuations=False, include_financial_reports=False,
                 include_fund_flows=False, include_sentiment=False,
                 include_tail_proxy=False, include_etf_indicators=False,
                 include_macro=False)
    flags.update(selected)
    return mirror_factor_inputs(db, warehouse=warehouse, **flags)


def test_no_coverage_is_not_a_continuous_day():
    result = _coverage_summary(
        [(date(2026, 10, 13), 5555, 0), (date(2026, 10, 12), 5555, 0)],
        minimum_coverage=0,
    )
    assert result["latest_daily_coverage"] == 0
    assert result["trailing_coverage_days"] == 0


def test_null_daily_input_is_not_reported_as_available(tmp_path):
    stats = _query_field_stats(_warehouse(tmp_path))
    assert stats["turnover_rate"]["nonnull_rows"] == 0


def _seed_etf(db):
    symbol = Symbol(symbol="510300", name="ETF", asset_type="etf", market="sh")
    db.add(symbol)
    db.flush()
    db.add(EtfIndicator(symbol_id=symbol.id, trade_date=date(2026, 10, 13),
                        premium_discount=-0.5, tracking_error=0.2, fund_size=100_000_000))
    db.commit()
    return symbol


def test_real_etf_values_are_visible_to_input_coverage(db_session, tmp_path):
    _seed_etf(db_session)
    warehouse = _warehouse(tmp_path)
    assert _mirror(db_session, warehouse, include_etf_indicators=True).etf_indicator_rows == 1
    stats = _query_field_stats(warehouse)
    assert stats["etf_premium_discount"]["table_rows"] == 1
    assert stats["etf_premium_discount"]["nonnull_rows"] == 1


def test_real_etf_values_reach_formula_executor(db_session, tmp_path):
    _seed_etf(db_session)
    warehouse = _warehouse(tmp_path)
    _mirror(db_session, warehouse, include_etf_indicators=True)
    compilation = compile_formula(formula="etf_premium_discount", strict_fields=True)
    assert compilation.is_valid
    result = FactorExecutor(warehouse).preview(compilation.execution_plan, trade_date=date(2026, 10, 13))
    assert result.errors == []
    assert result.coverage == 1
    assert result.values[0]["raw_value"] == -0.5


def test_repaired_sql_valuation_updates_the_factor_input(db_session, tmp_path):
    symbol = Symbol(symbol="000001", name="stock", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    value = StockValuation(symbol_id=symbol.id, trade_date=date(2026, 10, 13),
                           pe_ttm=20, pb=2, source="test")
    db_session.add(value)
    db_session.commit()
    warehouse = _warehouse(tmp_path)
    assert _mirror(db_session, warehouse, include_valuations=True).valuation_rows == 1
    value.pe_ttm = 10
    db_session.commit()
    _mirror(db_session, warehouse, include_valuations=True)
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT pe_ttm FROM raw_valuation_snapshots").fetchone()[0] == 10


def test_financial_mirror_keeps_prior_announcements_for_pit(db_session, tmp_path):
    """Today-only requests still mirror prior reports: this suspected gap is absent."""
    symbol = Symbol(symbol="000001", name="stock", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(StockFinancialReport(
        symbol_id=symbol.id, report_period=date(2026, 6, 30),
        announcement_date=date(2026, 8, 30), report_type="financial_analysis",
        roe_ttm=11, source="test",
    ))
    db_session.commit()
    warehouse = _warehouse(tmp_path)
    result = _mirror(db_session, warehouse, include_financial_reports=True,
                     start_date=date(2026, 10, 13), end_date=date(2026, 10, 13))
    assert result.financial_report_rows == 1
    compilation = compile_formula(formula="roe_ttm", strict_fields=True)
    preview = FactorExecutor(warehouse).preview(compilation.execution_plan, trade_date=date(2026, 10, 13))
    assert preview.errors == []
    assert preview.values[0]["raw_value"] == 11


def test_pit_financial_input_has_daily_coverage_evidence(db_session, tmp_path):
    symbol = Symbol(symbol="000001", name="stock", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(StockFinancialReport(
        symbol_id=symbol.id, report_period=date(2026, 6, 30),
        announcement_date=date(2026, 8, 30), report_type="financial_analysis",
        roe_ttm=11, source="test",
    ))
    db_session.commit()
    warehouse = _warehouse(tmp_path)
    _mirror(db_session, warehouse, include_financial_reports=True)
    assert _query_field_stats(warehouse)["roe_ttm"]["latest_daily_coverage"] == 1


def test_tail_proxy_rejects_only_after_hours_minute_rows():
    timestamps = pd.date_range("2026-10-13 00:00", periods=180, freq="1min").append(
        pd.date_range("2026-10-13 16:00", periods=20, freq="1min"))
    frame = pd.DataFrame({"时间": timestamps, "close": 10.0,
                          "high": 11.0, "low": 9.0, "amount": 100.0})
    assert calculate_tail_proxy(frame, trade_date=date(2026, 10, 13)) is None


def test_etf_future_nav_is_not_saved_as_historical_snapshot(db_session, monkeypatch):
    from app.services import etf_basic_data

    symbol = Symbol(symbol="510300", name="ETF", asset_type="etf", market="sh")
    db_session.add(symbol)
    db_session.flush()
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_daily", lambda *args: {})
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_spot", lambda *args: {})
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_info", lambda *args: {
        "trade_date": date(2026, 10, 13), "nav": 4.5,
    })
    assert etf_basic_data.sync_etf_indicator(db_session, symbol, date(2026, 1, 1)) is None


def test_etf_lookup_does_not_return_future_snapshot(db_session, monkeypatch):
    from app.services import etf_basic_data

    symbol = _seed_etf(db_session)
    record = db_session.query(EtfIndicator).one()
    record.premium_discount_score = 65
    db_session.commit()
    monkeypatch.setattr(etf_basic_data, "sync_etf_indicator", lambda *args: None)
    assert etf_basic_data.get_or_sync_etf_indicator(db_session, symbol, date(2026, 1, 1)) is None


def _tail_csv(*, tail_amount=100):
    timestamps = pd.date_range("2026-10-13 09:30", "2026-10-13 11:30", freq="1min").append(
        pd.date_range("2026-10-13 13:00", "2026-10-13 15:00", freq="1min"))
    frame = pd.DataFrame({
        "symbol": "000001", "timestamp": timestamps, "open": 10.0,
        "high": 11.0, "low": 9.0, "close": 10.0, "volume": 1,
        "amount": [tail_amount if timestamp.hour == 14 and timestamp.minute >= 30
                   or timestamp.hour == 15 else 100 for timestamp in timestamps],
    })
    return frame.to_csv(index=False)


def test_quality_snapshot_does_not_call_twenty_missing_days_light(db_session, tmp_path, monkeypatch):
    from app.services import data_quality

    warehouse = _warehouse(tmp_path)
    dates = [timestamp.date() for timestamp in pd.bdate_range(end="2026-10-13", periods=60)]
    with warehouse.connection() as conn:
        conn.execute("DELETE FROM raw_daily_bars")
        conn.executemany("""
            INSERT INTO raw_daily_bars
                (symbol,trade_date,adjust,close,amount,source,source_origin,source_row_id,ingested_at,batch_id)
            VALUES ('000001',?,'qfq',10,1000,'test','test',?,CURRENT_TIMESTAMP,'b')
        """, [(trade_date, index) for index, trade_date in enumerate(dates)])
        conn.executemany("""
            INSERT INTO raw_valuation_snapshots
                (symbol,trade_date,pe_ttm,pb,source,ingested_at,batch_id)
            VALUES ('000001',?,20,2,'test',CURRENT_TIMESTAMP,'v')
        """, [(trade_date,) for trade_date in dates[:40]])
    stats = _query_field_stats(warehouse)["pe_ttm"]
    assert stats["daily_coverage_p50"] == 1
    assert stats["latest_daily_coverage"] == 0
    monkeypatch.setattr(data_quality, "get_factor_system_config",
                        lambda *args: SimpleNamespace(warehouse_path=warehouse.path))
    monkeypatch.setattr(data_quality, "FactorWarehouse", lambda *args: warehouse)
    data_quality.capture_field_quality_snapshots(db_session, trigger="isolated_audit")
    quality = db_session.query(DataQualitySnapshot).filter_by(field="pe_ttm").one()
    assert quality.quality_level == "HEAVY"
    assert quality.missing_ratio == pytest.approx(20 / 60)
    assert quality.max_consecutive_gap_days == 20
