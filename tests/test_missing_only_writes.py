"""Missing-only writes preserve known facts and report only actual changes."""
from datetime import date
import json
from unittest.mock import Mock

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.symbol import Symbol
from app.models.stock_valuation import StockValuation
from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.services import fundamental_data as valuation, capital_flow_data as flow
from app.services import etf_basic_data as etf, financial_data as financial
from app.services import lhb_data as lhb, hot_rank_data as hot

pytestmark = pytest.mark.whitebox
DAY = date(2026, 7, 14)


def stock(db):
    symbol = Symbol(symbol="600519", name="sample", asset_type="stock", market="sh", industry="Bank")
    db.add(symbol)
    db.flush()
    return symbol


@pytest.fixture(autouse=True)
def open_calendar(monkeypatch):
    monkeypatch.setattr("app.services.market_session.is_cn_trading_day", lambda *args: True)


def test_valuation_missing_only_keeps_values_and_filters_nonmissing_dates(db_session, monkeypatch):
    symbol = stock(db_session)
    row = StockValuation(symbol_id=symbol.id, trade_date=DAY, pe_ttm=8, pb=0,
                         total_market_cap=None, circulating_market_cap=20, industry="Original",
                         pe_score=75, raw_json='{"original": true}', source="original")
    db_session.add(row)
    db_session.flush()
    observations = [{"trade_date": day, "pe_ttm": 99, "pb": 3, "total_market_cap": 100,
                     "circulating_market_cap": 90} for day in (DAY, date(2026, 7, 15))]
    fetch = Mock(return_value=observations)
    monkeypatch.setattr(valuation, "_fetch_value_history_rows", fetch)
    kwargs = dict(start_date=DAY, end_date=date(2026, 7, 15), fill_missing_only=True, allowed_dates={DAY})
    count = valuation.sync_symbol_valuation_range(db_session, symbol, **kwargs)
    assert count == 1 and count.sync_written_dates == (DAY,)
    assert (row.pe_ttm, row.pb, row.pe_score, row.industry) == (8, 0, 75, "Original")
    assert row.total_market_cap == 100 and row.circulating_market_cap == 20
    assert json.loads(row.raw_json)["original"] is True
    assert row.source == "original"
    assert valuation.sync_symbol_valuation_range(db_session, symbol, **kwargs) == 0
    assert db_session.query(StockValuation).count() == 1
    # The default refresh still revises a known fact.
    assert valuation.sync_symbol_valuation_range(db_session, symbol, start_date=DAY, end_date=DAY) == 1
    assert row.pe_ttm == 99


def test_daily_valuation_missing_only_preserves_values(db_session, monkeypatch):
    symbol = stock(db_session)
    today = date.today()
    row = StockValuation(symbol_id=symbol.id, trade_date=today, pe_ttm=8, pb=0,
                         total_market_cap=100, circulating_market_cap=None, industry="Bank", pe_score=75)
    db_session.add(row)
    db_session.flush()
    monkeypatch.setattr("app.services.valuation_provider.fetch_valuation_rows", lambda *a, **k: [{
        "symbol": "600519", "trade_date": today, "pe_ttm": 99, "pb": 5,
        "total_market_cap": 200, "circulating_market_cap": 180}])
    assert valuation.sync_market_valuation_snapshot(db_session, [symbol], today, fill_missing_only=True) == {symbol.id}
    assert (row.pe_ttm, row.pb, row.total_market_cap, row.pe_score) == (8, 0, 100, 75)
    assert row.circulating_market_cap == 180
    assert not valuation.sync_market_valuation_snapshot(db_session, [symbol], today, fill_missing_only=True)


def test_fund_flow_range_fetches_once_and_only_fills_allowed_dates(db_session, monkeypatch):
    symbol = stock(db_session)
    row = CapitalFlow(symbol_id=symbol.id, trade_date=DAY, main_net_inflow=0,
                      main_net_inflow_score=71, source="original", raw_json='{"provider_time":"old"}')
    db_session.add(row)
    db_session.flush()
    observations = [{"trade_date": day, "main_net_inflow": 999, "main_net_inflow_pct": 2,
        "super_large_net_inflow": 7, "large_net_inflow": 6,
        "medium_net_inflow": -5, "small_net_inflow": -8} for day in (DAY, date(2026, 7, 15))]
    fetch = Mock(return_value=observations)
    monkeypatch.setattr(flow, "_fetch_individual_fund_flow_history", fetch)
    kwargs = dict(start_date=DAY, end_date=date(2026, 7, 15), fill_missing_only=True, allowed_dates={DAY})
    count = flow.sync_symbol_capital_flow_range(db_session, symbol, **kwargs)
    assert count == 1 and count.sync_written_dates == (DAY,)
    assert fetch.call_count == 1
    assert row.main_net_inflow == 0 and row.main_net_inflow_score == 71
    assert row.super_large_net_inflow == 7 and row.medium_net_inflow == -5
    assert json.loads(row.raw_json)["provider_time"] == "old"
    assert flow.sync_symbol_capital_flow_range(db_session, symbol, **kwargs) == 0
    assert db_session.query(CapitalFlow).count() == 1


def test_fund_flow_missing_main_in_response_can_fill_other_fields(db_session):
    symbol = stock(db_session)
    row = CapitalFlow(symbol_id=symbol.id, trade_date=DAY, main_net_inflow=-10, main_net_inflow_score=40)
    db_session.add(row)
    db_session.flush()
    assert flow._upsert_capital_flow(db_session, symbol, {"trade_date": DAY,
        "main_net_inflow": None, "large_net_inflow": 12}, fill_missing_only=True) is row
    assert row.main_net_inflow == -10 and row.large_net_inflow == 12 and row.main_net_inflow_score == 40


def test_daily_fund_flow_propagates_missing_only(db_session, monkeypatch):
    symbol = stock(db_session)
    today = date.today()
    row = CapitalFlow(symbol_id=symbol.id, trade_date=today, main_net_inflow=0, main_net_inflow_score=70)
    db_session.add(row)
    db_session.flush()
    monkeypatch.setattr("app.services.capital_flow_provider.fetch_capital_flow_quotes", lambda *a, **k: {
        "600519": {"trade_date": today, "main_net_inflow": 999, "large_net_inflow": 10}})
    assert flow.sync_market_capital_flow_snapshot(db_session, [symbol], today, fill_missing_only=True) == {symbol.id}
    assert row.main_net_inflow == 0 and row.main_net_inflow_score == 70 and row.large_net_inflow == 10
    assert not flow.sync_market_capital_flow_snapshot(db_session, [symbol], today, fill_missing_only=True)


def test_etf_missing_derivatives_use_retained_facts_and_preserve_provenance(db_session, monkeypatch):
    symbol = Symbol(symbol="510300", name="etf", asset_type="etf", market="sh")
    db_session.add(symbol)
    db_session.flush()
    today = date.today()
    row = EtfIndicator(symbol_id=symbol.id, trade_date=today, nav=2, close=2,
                       premium_discount_score=55, source="original",
                       raw_json='{"quote":{"updated_at":"original_time"}}')
    db_session.add(row)
    db_session.flush()
    quote = {"trade_date": today, "updated_at": f"{today}T15:01:00+08:00", "nav": 4, "close": 10, "total_shares": 100}
    monkeypatch.setattr(etf, "fetch_etf_quotes", lambda *a, **k: {"510300": quote})
    result = etf.sync_etf_snapshots(db_session, [symbol], today, fill_missing_only=True)
    assert result["records"] == 1
    assert (row.nav, row.close, row.total_shares, row.fund_size) == (2, 2, 100, 200)
    assert row.premium_discount == 0 and row.premium_discount_score == 55
    evidence = json.loads(row.raw_json)
    assert evidence["quote"]["updated_at"] == "original_time"
    assert evidence["missing_fields_fills"][-1]["observation"]["quote"]["updated_at"] == quote["updated_at"]
    assert row.source == "original" and row.tracking_error is None
    assert etf.sync_etf_snapshots(db_session, [symbol], today, fill_missing_only=True)["records"] == 0


@pytest.mark.parametrize("daily", [False, True])
def test_financial_missing_only_uses_full_pit_identity(db_session, monkeypatch, daily):
    symbol = stock(db_session)
    initial = pd.DataFrame([dict(SECUCODE="600519.SH", REPORT_DATE="2026-06-30", NOTICE_DATE="2026-08-15",
        ROEJQ=5, PARENTNETPROFIT=10, TOTALOPERATEREVE=None,
        PARENTNETPROFITTZ=2, TOTALOPERATEREVETZ=3)])
    monkeypatch.setattr("app.services.financial_provider.fetch_financial_rows", lambda *a, **k: initial.to_dict("records"))
    assert financial.sync_symbol_financial_reports(db_session, symbol) == 1
    original = db_session.scalar(select(StockFinancialReport))
    revised = initial.copy()
    revised.loc[0, "ROEJQ"] = 99
    revised.loc[0, "TOTALOPERATEREVE"] = 20
    new_version = revised.copy()
    new_version.loc[0, "NOTICE_DATE"] = "2026-08-16"
    payload = pd.concat([revised, new_version]).to_dict("records")
    monkeypatch.setattr("app.services.financial_provider.fetch_financial_rows", lambda *a, **k: payload)
    def sync():
        if daily:
            return financial.sync_financial_announcements(db_session, [symbol], date(2026, 8, 15),
                date(2026, 8, 16), fill_missing_only=True)["records"]
        return financial.sync_symbol_financial_reports(db_session, symbol, fill_missing_only=True)
    count = sync()
    assert count == 2
    if not daily:
        assert count.sync_written_dates == (date(2026, 8, 15), date(2026, 8, 16))
    assert original.roe_ttm == 5 and original.revenue == 20
    assert db_session.query(StockFinancialReport).count() == 2
    assert sync() == 0


def test_lhb_missing_only_keeps_existing_event_values(db_session, monkeypatch):
    row = LhbInstitutionTrade(symbol="600519", trade_date=DAY, source="sample", institution_net=0,
                              raw_json='{"original":true}')
    db_session.add(row)
    db_session.flush()
    normalized = pd.DataFrame([dict(symbol="600519", trade_date=DAY, source="sample",
        institution_net=99, institution_buy=100, institution_sell=10, buyer_institution_count=1,
        seller_institution_count=2, market_amount=1000, institution_net_pct=1, turnover_rate=2, reason="test")])
    monkeypatch.setattr(lhb, "_fetch_lhb_institution", lambda *a: (normalized, {}))
    assert lhb.sync_lhb_institution_trades(db_session, start_date=DAY, end_date=DAY, fill_missing_only=True).written == 1
    assert row.institution_net == 0 and row.institution_buy == 100
    assert lhb.sync_lhb_institution_trades(db_session, start_date=DAY, end_date=DAY, fill_missing_only=True).written == 0


def test_hot_rank_missing_only_inserts_new_member_without_changing_existing_rank(db_session, monkeypatch):
    today = date.today()
    row = StockHotRankSnapshot(symbol="600519", trade_date=today, source="sample",
        hot_rank=1, hot_rank_total=100, hot_rank_pct=1, raw_json='{"old":true}')
    db_session.add(row)
    db_session.flush()
    normalized = pd.DataFrame([dict(symbol=code, hot_rank=rank, hot_rank_total=100,
        hot_rank_pct=float(rank), source="sample") for code, rank in (("600519", 2), ("000001", 3))])
    monkeypatch.setattr(hot, "_fetch_hot_rank", lambda *a: (normalized, {}))
    assert hot.sync_hot_rank_snapshot(db_session, fill_missing_only=True).written == 1
    assert row.hot_rank == 1 and row.raw_json == '{"old":true}'
    assert db_session.query(StockHotRankSnapshot).count() == 2
    assert hot.sync_hot_rank_snapshot(db_session, fill_missing_only=True).written == 0


@pytest.mark.parametrize("service,fetch_name", [(valuation, "_fetch_value_history_rows"),
                                               (flow, "_fetch_individual_fund_flow_history")])
def test_empty_allowed_date_selection_skips_remote_read(db_session, monkeypatch, service, fetch_name):
    symbol = stock(db_session)
    fetch = Mock(side_effect=AssertionError("empty gap must not use network"))
    monkeypatch.setattr(service, fetch_name, fetch)
    sync = service.sync_symbol_valuation_range if service is valuation else service.sync_symbol_capital_flow_range
    count = sync(db_session, symbol, start_date=DAY, end_date=DAY,
                 fill_missing_only=True, allowed_dates=set())
    assert count == 0 and count.sync_written_dates == ()
    fetch.assert_not_called()


def test_fund_flow_history_evidence_never_certifies_current_intraday_session(db_session, monkeypatch):
    symbol = stock(db_session)
    frame = pd.DataFrame([{"日期": day.isoformat(), "主力净流入-净额": 10}
                          for day in (DAY, date.today())])
    monkeypatch.setattr(flow, "call_akshare_with_retry", lambda *a, **k: frame)
    rows = flow._fetch_individual_fund_flow_history(db_session, symbol)
    assert rows[0]["_history_complete"] is True
    assert rows[-1]["_history_complete"] is False


def test_northbound_missing_only_preserves_existing_values(db_session, monkeypatch):
    original = NorthboundFlow(trade_date=DAY, total_net_inflow=0)
    db_session.add(original)
    db_session.flush()
    monkeypatch.setattr(flow, "northbound_capability", lambda: {"available": True})
    monkeypatch.setattr(flow.ak, "stock_hsgt_north_net_flow_in", lambda **kwargs: None, raising=False)
    monkeypatch.setattr(flow, "call_akshare_with_retry", lambda *a, **k: pd.DataFrame([
        {"日期": DAY.isoformat(), "当日净流入": 999},
        {"日期": "2026-07-15", "当日净流入": 10}]))
    assert flow.sync_northbound_flow(db_session, start_date=DAY, end_date=date(2026, 7, 15), fill_missing_only=True) == 1
    assert original.total_net_inflow == 0
    assert flow.sync_northbound_flow(db_session, start_date=DAY, end_date=date(2026, 7, 15), fill_missing_only=True) == 0
