"""Real SQL and warehouse boundaries; providers are controlled, never live."""
from datetime import date, timedelta

import pandas as pd
import pytest

from app.models.daily_bar import DailyBar
from app.models.etf_indicator import EtfIndicator
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services import etf_basic_data, fundamental_data
from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.formula_catalog import _query_field_stats
from tests.test_factor_input_coverage_audit import _warehouse, _mirror, _seed_etf

pytestmark = pytest.mark.whitebox


@pytest.fixture(autouse=True)
def known_trading_calendar(monkeypatch):
    # Date-alignment contracts are independent of the calendar guard tests.
    monkeypatch.setattr("app.services.market_session.is_cn_trading_day", lambda db, day: True)


def _symbol(db, *, asset_type="etf"):
    record = Symbol(symbol="510300.SH", name="audit", asset_type=asset_type, market="sh")
    db.add(record)
    db.flush()
    return record


def test_nav_history_is_sorted_and_never_uses_future_row(db_session, monkeypatch):
    symbol = _symbol(db_session)
    frame = pd.DataFrame([
        {"净值日期": "2026-09-29", "单位净值": 1.1},
        {"净值日期": "2026-10-20", "单位净值": 9},
        {"净值日期": "2026-09-28", "单位净值": 1},
    ])
    monkeypatch.setattr(etf_basic_data, "call_akshare_with_retry", lambda *a, **k: frame)
    assert etf_basic_data._fetch_etf_fund_info(db_session, symbol, date(2026, 9, 30)) == {
        "trade_date": date(2026, 9, 29), "nav": 1.1,
    }
    assert etf_basic_data._fetch_etf_fund_info(db_session, symbol, date(2026, 1, 1)) == {}


def test_daily_nav_uses_latest_dated_column_not_column_order(db_session, monkeypatch):
    symbol = _symbol(db_session)
    prior = date.today() - timedelta(days=2)
    latest = date.today() - timedelta(days=1)
    frame = pd.DataFrame([{"基金代码": "510300", f"{prior}-单位净值": 1,
                           f"{latest}-单位净值": 2, "市价": 9}])
    monkeypatch.setattr(etf_basic_data, "call_akshare_with_retry", lambda *a, **k: frame)
    result = etf_basic_data._fetch_etf_fund_daily(db_session, symbol)
    assert result["nav"] == 2
    assert result["trade_date"] == latest


def test_current_quote_cannot_be_combined_with_prior_day_nav(db_session, monkeypatch):
    symbol = _symbol(db_session)
    prior = date.today() - timedelta(days=1)
    db_session.add(DailyBar(symbol_id=symbol.id, trade_date=prior,
                            open=1, high=1.2, low=1, close=1.1))
    db_session.flush()
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_daily", lambda *a: {
        "trade_date": prior, "nav": 1, "price_date": date.today(), "close": 9,
    })
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_info", lambda *a: {})
    monkeypatch.setattr(etf_basic_data, "fetch_etf_quotes", lambda *a, **k: {"510300": {
        "trade_date": date.today(), "close": 10, "nav": None, "total_shares": 1000,
    }})
    result = etf_basic_data.sync_etf_indicator(db_session, symbol)
    assert result.trade_date == date.today()
    assert result.close == 10 and result.nav is None
    assert result.premium_discount is None
    assert result.fund_size is None and result.tracking_error is None
    assert db_session.query(EtfIndicator).count() == 1


def test_historical_etf_uses_only_dated_nav_and_matching_close(db_session, monkeypatch):
    symbol = _symbol(db_session)
    observed = date(2020, 1, 2)
    db_session.add(DailyBar(symbol_id=symbol.id, trade_date=observed,
                            open=1, high=1.2, low=1, close=1.1))
    db_session.flush()
    def live_quote(*args):
        pytest.fail("a historical request must not query today's ETF snapshot")
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_daily", live_quote)
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_spot", live_quote)
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_info", lambda *a: {
        "trade_date": observed, "nav": 1,
    })
    result = etf_basic_data.sync_etf_indicator(db_session, symbol, date(2020, 1, 3))
    assert result.trade_date == observed
    assert result.premium_discount == pytest.approx(10)


def test_historical_valuation_does_not_query_undated_spot(db_session, monkeypatch):
    symbol = _symbol(db_session, asset_type="stock")
    def live_quote(*args):
        pytest.fail("historical valuation must not request today's live quote")
    monkeypatch.setattr(fundamental_data, "_fetch_spot_valuation", live_quote)
    monkeypatch.setattr(fundamental_data, "_fetch_value_history_rows", lambda *a, **k: [{
        "trade_date": date(2020, 1, 3), "pe_ttm": 8, "pb": 1,
    }])
    monkeypatch.setattr(fundamental_data, "_fetch_individual_info", lambda *a: {})
    result = fundamental_data.sync_symbol_valuation(db_session, symbol, date(2020, 1, 3))
    assert result.trade_date == date(2020, 1, 3)
    assert result.pe_ttm == 8


def test_historical_valuation_lookup_excludes_future_snapshot(db_session, monkeypatch):
    symbol = _symbol(db_session, asset_type="stock")
    db_session.add(StockValuation(symbol_id=symbol.id, trade_date=date(2026, 10, 13),
                                  pe_ttm=8, pe_score=90))
    db_session.flush()
    monkeypatch.setattr(fundamental_data, "sync_symbol_valuation", lambda *a: None)
    assert fundamental_data.get_or_sync_valuation(db_session, symbol, date(2020, 1, 1)) is None


@pytest.mark.parametrize("field,expected", [("etf_premium_discount", -0.5),
                                            ("etf_tracking_error", 0.2),
                                            ("etf_fund_size", 100_000_000)])
def test_all_etf_fields_use_same_mapping_for_coverage_and_executor(db_session, tmp_path, field, expected):
    _seed_etf(db_session)
    warehouse = _warehouse(tmp_path)
    _mirror(db_session, warehouse, include_etf_indicators=True)
    stats = _query_field_stats(warehouse)[field]
    assert stats["nonnull_rows"] == 1
    assert stats["expected_rows"] == 1  # ETF denominator excludes the stock bar.
    assert stats["latest_daily_coverage"] == 1
    plan = compile_formula(formula=field, strict_fields=True).execution_plan
    result = FactorExecutor(warehouse).preview(plan, trade_date=date(2026, 10, 13))
    assert result.errors == []
    assert result.values[0]["raw_value"] == expected


def test_partial_cross_section_does_not_hide_a_symbol_gap(tmp_path):
    warehouse = _warehouse(tmp_path)
    dates = [value.date() for value in pd.bdate_range(end="2026-10-13", periods=60)]
    with warehouse.connection() as conn:
        conn.execute("INSERT INTO raw_asset_universe VALUES ('000002','second','stock','sz','cn',TRUE,'test',3,CURRENT_TIMESTAMP)")
        conn.execute("DELETE FROM raw_daily_bars")
        conn.executemany("""INSERT INTO raw_daily_bars
            (symbol,trade_date,adjust,close,source,source_origin,source_row_id,ingested_at,batch_id)
            VALUES (?,?,'qfq',10,'test','test',?,CURRENT_TIMESTAMP,'b')""",
            [(symbol, day, index) for symbol in ('000001', '000002') for index, day in enumerate(dates)])
        conn.executemany("""INSERT INTO raw_valuation_snapshots
            (symbol,trade_date,pe_ttm,source,ingested_at,batch_id)
            VALUES (?,?,20,'test',CURRENT_TIMESTAMP,'v')""",
            [('000001', day) for day in dates] + [('000002', day) for day in dates[:40]])
    stats = _query_field_stats(warehouse)["pe_ttm"]
    assert stats["expected_rows"] == 120
    assert stats["covered_rows"] == 100
    assert stats["missing_ratio"] == pytest.approx(1 / 6)
    assert stats["max_consecutive_gap_days"] == 20


@pytest.mark.parametrize("refresh,expected_nav,expected_close,expected_premium", [
    ("close_only", 1, 2.2, 10), ("nav_only", 2, 1.1, 10),
])
def test_partial_etf_refresh_preserves_same_day_facts(
    db_session, monkeypatch, refresh, expected_nav, expected_close, expected_premium
):
    symbol = _symbol(db_session)
    existing = EtfIndicator(symbol_id=symbol.id, trade_date=date.today(),
                            nav=1, close=1.1, premium_discount=10,
                            premium_discount_score=25, fund_size=100, tracking_error=0.2)
    db_session.add(existing)
    db_session.commit()
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_daily", lambda *a: {})
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_info", lambda *a: (
        {"trade_date": date.today(), "nav": 2} if refresh == "nav_only" else {}
    ))
    monkeypatch.setattr(etf_basic_data, "fetch_etf_quotes", lambda *a, **k: {"510300": {
        "trade_date": date.today(), "close": 2.2 if refresh == "close_only" else None,
        "nav": 2 if refresh == "nav_only" else None, "total_shares": 1000,
    }})
    record = etf_basic_data.sync_etf_indicator(db_session, symbol)
    assert record.nav == expected_nav
    assert record.close == expected_close
    assert record.premium_discount == pytest.approx(expected_premium)
    assert record.premium_discount_score is not None
    assert record.fund_size == (2000 if refresh == "nav_only" else 100)
    assert record.tracking_error == 0.2
    assert db_session.query(EtfIndicator).count() == 1


def test_numeric_market_code_matches_zero_padded_stock(db_session, monkeypatch):
    monkeypatch.setattr("app.services.market_session.is_cn_trading_day", lambda db, day: True)
    symbol = Symbol(symbol="000001", name="stock", asset_type="stock", market="sz")
    db_session.add(symbol)
    db_session.flush()
    def selected_daily(db, codes, start, end, **kwargs):
        assert codes == ["000001"]
        return [{"symbol": "000001", "trade_date": start, "pe_ttm": 10, "pb": 1,
                 "total_market_cap": 100, "circulating_market_cap": 80}]
    monkeypatch.setattr("app.services.valuation_provider.fetch_valuation_rows", selected_daily)
    synced = fundamental_data.sync_market_valuation_snapshot(db_session, [symbol])
    assert synced == {symbol.id}
    row = db_session.query(StockValuation).one()
    assert row.trade_date == date.today() and row.pe_ttm == 10
