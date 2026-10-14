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
    (TailAccumulationSnapshot, "include_tail_proxy", "raw_tail_proxy", "proxy_score",
     {"minute_count": 240, "tail_minute_count": 30, "day_amount": 20000, "tail_amount": 3000,
      "tail_amount_share": .15, "tail_activity_ratio": 1, "tail_return": .01, "close_location": .8}),
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


@pytest.fixture
def tail_environment(db_session, tmp_path, monkeypatch):
    from app.services import data_quality
    from app.services.factors import data_sync

    warehouse = FactorWarehouse(tmp_path / "tail.duckdb")
    warehouse.initialize()
    monkeypatch.setattr(data_sync, "FactorWarehouse", lambda *args, **kwargs: warehouse)
    monkeypatch.setattr(data_quality, "capture_field_quality_snapshots", lambda *args, **kwargs: {})
    timestamps = pd.date_range("2026-10-13 09:30", "2026-10-13 11:30", freq="1min").append(
        pd.date_range("2026-10-13 13:00", "2026-10-13 15:00", freq="1min"))
    frame = pd.DataFrame(dict(symbol="000001", timestamp=timestamps, open=10.0, high=11.0,
                              low=9.0, close=10.0, volume=1.0, amount=100.0))
    return warehouse, frame


@pytest.mark.parametrize("field,value", [("open", 12), ("close", -1), ("amount", float("inf")),
                                          ("volume", -1), ("close", float("nan")),
                                          ("timestamp", "2026-10-13 12:00:00"),
                                          ("timestamp", "2026-10-13 09:30:30")])
def test_bad_csv_session_is_rejected_even_with_241_other_valid_minutes(
    db_session, tail_environment, field, value,
):
    from app.services.tail_proxy_import import import_tail_minute_csv

    warehouse, frame = tail_environment
    frame.loc[0, field] = value
    result = import_tail_minute_csv(db_session, csv_text=frame.to_csv(index=False), filename="bad.csv")
    assert result["written_sessions"] == 0
    assert result["rejected_sessions"] == 1 and result["invalid_rows"] == 1
    assert result["rejected"][0]["reason"] == "invalid_minute_bars"
    assert db_session.query(TailAccumulationSnapshot).count() == 0
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_tail_proxy").fetchone()[0] == 0


def test_csv_utc_timestamps_use_chinese_session_date(db_session, tail_environment):
    from app.services.tail_proxy_import import import_tail_minute_csv

    _, frame = tail_environment
    frame["timestamp"] = frame["timestamp"].dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    result = import_tail_minute_csv(db_session, csv_text=frame.to_csv(index=False), filename="utc.csv")
    assert result["written_sessions"] == 1
    assert db_session.query(TailAccumulationSnapshot).one().trade_date == date(2026, 10, 13)


def test_csv_future_dates_are_rejected_after_local_clock_normalization(db_session, tail_environment):
    from app.services.tail_proxy_import import import_tail_minute_csv

    _, frame = tail_environment
    tomorrow = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    frame["timestamp"] += pd.Timedelta(days=(tomorrow - date(2026, 10, 13)).days)
    result = import_tail_minute_csv(db_session, csv_text=frame.to_csv(index=False), filename="future.csv")
    assert result["written_sessions"] == 0 and result["rejected_sessions"] == 1
    assert result["supplier_history_verified"] is False


def test_csv_mirror_failure_is_explicit_and_reimport_recovers_without_republishing(
    db_session, tail_environment, monkeypatch,
):
    from app.services.factors import data_sync
    from app.services.tail_proxy_import import import_tail_minute_csv

    warehouse, frame = tail_environment
    csv_text = frame.to_csv(index=False)
    mirror = data_sync.mirror_factor_inputs
    monkeypatch.setattr(data_sync, "mirror_factor_inputs", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("busy warehouse")))
    first = import_tail_minute_csv(db_session, csv_text=csv_text, filename="first.csv")
    assert first["written_sessions"] == 1 and db_session.query(TailAccumulationSnapshot).count() == 1
    assert first["warehouse_mirror"]["status"] == "warning"
    assert first["quality_snapshot"]["status"] == "skipped" and len(first["warnings"]) == 1
    monkeypatch.setattr(data_sync, "mirror_factor_inputs", mirror)
    second = import_tail_minute_csv(db_session, csv_text=csv_text, filename="retry.csv")
    assert second["warehouse_mirror"]["status"] == "done"
    assert second["warehouse_mirror"]["records"] == 1
    with warehouse.connection(read_only=True) as conn:
        published = conn.execute("SELECT ingested_at,batch_id FROM raw_tail_proxy").fetchone()
    third = import_tail_minute_csv(db_session, csv_text=csv_text, filename="same-data.csv")
    assert third["warehouse_mirror"]["records"] == 0
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT ingested_at,batch_id FROM raw_tail_proxy").fetchone() == published


def test_csv_quality_failure_retains_sql_and_warehouse_data(db_session, tail_environment, monkeypatch):
    from app.services import data_quality
    from app.services.tail_proxy_import import import_tail_minute_csv

    warehouse, frame = tail_environment
    monkeypatch.setattr(data_quality, "capture_field_quality_snapshots", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("quality unavailable")))
    result = import_tail_minute_csv(db_session, csv_text=frame.to_csv(index=False), filename="quality.csv")
    assert result["warehouse_mirror"]["status"] == "done"
    assert result["quality_snapshot"]["status"] == "warning" and result["warnings"]
    assert db_session.query(TailAccumulationSnapshot).count() == 1
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_tail_proxy").fetchone()[0] == 1
