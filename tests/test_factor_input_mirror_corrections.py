"""Real SQLite→DuckDB correction, publication, cancellation and CSV contracts."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.models.capital_flow import CapitalFlow
from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.macro_data import MacroIndicatorValue
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.services.factors.data_sync import mirror_factor_inputs
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


CASES = [
    (StockValuation, "include_valuations", "raw_valuation_snapshots", "pe_ttm", {}),
    (CapitalFlow, "include_fund_flows", "raw_fund_flows", "main_net_inflow", {}),
    (StockFinancialReport, "include_financial_reports", "raw_financial_reports", "roe_ttm",
     {"report_period": date(2026, 6, 30), "announcement_date": date(2026, 10, 13), "report_type": "financial_analysis"}),
    (StockHotRankSnapshot, "include_sentiment", "raw_sentiment", "hot_rank_pct", {"hot_rank": 1, "hot_rank_total": 100}),
    (LhbInstitutionTrade, "include_sentiment", "raw_sentiment", "lhb_institution_net", {}),
    (EtfIndicator, "include_etf_indicators", "raw_etf_indicators", "premium_discount", {}),
    (MacroIndicatorValue, "include_macro", "raw_macro", "value",
     {"region": "cn", "category": "risk", "indicator_key": "yield", "period": "2026-10-13", "name": "yield"}),
]


def _mirror(db, warehouse, flag, **extra):
    flags = dict.fromkeys(("include_valuations", "include_fund_flows", "include_financial_reports",
                          "include_sentiment", "include_tail_proxy", "include_etf_indicators", "include_macro"), False)
    flags[flag] = True
    return mirror_factor_inputs(db, warehouse=warehouse, **flags, **extra)


@pytest.mark.parametrize("model,flag,table,column,extra", CASES)
def test_old_id_correction_changes_content_once_and_preserves_unchanged_publication(
    db_session, tmp_path, model, flag, table, column, extra,
):
    symbol = Symbol(symbol="000001", name="test", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    values = {"source": "test", **extra}
    if model is not MacroIndicatorValue:
        if model is not StockFinancialReport:
            values["trade_date"] = date(2026, 10, 13)
        values["symbol_id" if hasattr(model, "symbol_id") else "symbol"] = symbol.id if hasattr(model, "symbol_id") else symbol.symbol
    source_column = "institution_net" if model is LhbInstitutionTrade else column
    values[source_column] = 20
    record = model(**values)
    db_session.add(record)
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "mirror.duckdb")
    first = _mirror(db_session, warehouse, flag)
    assert first.rows_written == 1
    with warehouse.connection(read_only=True) as conn:
        original = conn.execute(f"SELECT {column},ingested_at,batch_id FROM {table}").fetchone()
    assert _mirror(db_session, warehouse, flag).rows_written == 0
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(f"SELECT {column},ingested_at,batch_id FROM {table}").fetchone() == original
    record_id = record.id
    setattr(record, source_column, 10)
    db_session.commit()
    changed = _mirror(db_session, warehouse, flag)
    assert record.id == record_id
    assert changed.rows_written == 1
    with warehouse.connection(read_only=True) as conn:
        revised = conn.execute(f"SELECT {column},ingested_at,batch_id FROM {table}").fetchone()
    assert revised[0] == 10 and revised[1] > original[1] and revised[2] == changed.batch_id
    assert _mirror(db_session, warehouse, flag, full_refresh=True).rows_written == 0
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(f"SELECT {column},ingested_at,batch_id FROM {table}").fetchone() == revised


def test_bounded_historical_repair_rescans_old_ids_without_forging_publication(db_session, tmp_path):
    symbol = Symbol(symbol="000001", name="test", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    old = StockValuation(symbol_id=symbol.id, trade_date=date(2019, 1, 2), pe_ttm=20, source="test")
    latest = StockValuation(symbol_id=symbol.id, trade_date=date(2026, 10, 13), pe_ttm=30, source="test")
    db_session.add_all([old, latest])
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "mirror.duckdb")
    assert _mirror(db_session, warehouse, "include_valuations").rows_written == 2
    old.pe_ttm = 10
    db_session.commit()
    # Default checks only recent source dates; explicitly requested repairs
    # reach historical corrections without re-reading millions of old rows.
    assert _mirror(db_session, warehouse, "include_valuations").rows_written == 0
    assert _mirror(db_session, warehouse, "include_valuations", start_date=date(2019, 1, 2), end_date=date(2019, 1, 2)).rows_written == 1
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT pe_ttm FROM raw_valuation_snapshots WHERE trade_date='2019-01-02'").fetchone()[0] == 10


def test_upsert_skip_unchanged_ignores_batch_metadata_and_old_hash_formats(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "mirror.duckdb")
    record = dict(symbol="000001", trade_date=date(2026, 10, 13), pe_ttm=20, pb=2,
                  source="test", source_hash="legacy", ingested_at=datetime(2026, 10, 13), batch_id="original")
    assert warehouse.upsert_records("raw_valuation_snapshots", [record], skip_unchanged=True) == 1
    retry = {**record, "source_hash": "new-hash-style", "ingested_at": datetime(2026, 10, 14), "batch_id": "retry"}
    assert warehouse.upsert_records("raw_valuation_snapshots", [retry], skip_unchanged=True) == 0
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT ingested_at,batch_id,source_hash FROM raw_valuation_snapshots").fetchone() == (record["ingested_at"], "original", "legacy")


def test_sentiment_streams_with_a_shared_provider_preserve_each_others_fields(db_session, tmp_path):
    hot = StockHotRankSnapshot(symbol="000001", trade_date=date(2026, 10, 13),
                                hot_rank=1, hot_rank_total=100, hot_rank_pct=1, source="akshare")
    lhb = LhbInstitutionTrade(symbol="000001", trade_date=date(2026, 10, 13),
                              institution_net=20, source="akshare")
    db_session.add_all([hot, lhb])
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "mirror.duckdb")
    _mirror(db_session, warehouse, "include_sentiment")
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT hot_rank_pct,has_lhb,lhb_institution_net FROM raw_sentiment").fetchone() == (1, True, 20)
    hot.hot_rank_pct = 2
    db_session.commit()
    assert _mirror(db_session, warehouse, "include_sentiment").rows_written == 1
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT hot_rank_pct,has_lhb,lhb_institution_net FROM raw_sentiment").fetchone() == (2, True, 20)
