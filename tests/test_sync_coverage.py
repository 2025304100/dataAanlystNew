from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.init_db import _ensure_data_sync_policy_column
from app.models.capital_flow import CapitalFlow
from app.models.data_sync_coverage import DataSyncCoverage
from app.models.data_sync_plan import DataSyncPlan
from app.models.etf_indicator import EtfIndicator
from app.models.financial_report import StockFinancialReport
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.market_data import MarketCalendar
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services.sync_coverage import (
    covered_dates, existing_dates, get_coverage_summary, record_coverage,
    record_coverage_points, source_covered_dates,
)

pytestmark = pytest.mark.whitebox


@pytest.fixture
def ledger_db():
    engine = create_engine("sqlite://")
    tables = [model.__table__ for model in (
        Symbol, DataSyncCoverage, StockValuation, CapitalFlow, EtfIndicator,
        StockFinancialReport, StockHotRankSnapshot, LhbInstitutionTrade, MarketCalendar,
    )]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine, autoflush=False) as db:
        yield db
    engine.dispose()


def test_coverage_is_registered_and_policy_adds_a_safe_legacy_default():
    assert "data_sync_coverage" in Base.metadata.tables
    assert DataSyncPlan.__table__.c.sync_policy.default.arg == "normal"
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE data_sync_plans (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO data_sync_plans(id) VALUES ('old-plan')"))
    _ensure_data_sync_policy_column(engine)
    _ensure_data_sync_policy_column(engine)
    with engine.begin() as connection:
        assert connection.execute(text("SELECT sync_policy FROM data_sync_plans")).scalar() == "normal"
        connection.execute(text("UPDATE data_sync_plans SET sync_policy='force'"))
    _ensure_data_sync_policy_column(engine)
    with engine.begin() as connection:
        assert connection.execute(text("SELECT sync_policy FROM data_sync_plans")).scalar() == "force"
    engine.dispose()


def test_exact_days_never_imply_an_interior_day_or_a_partial_point(ledger_db):
    first, middle, last = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
    assert record_coverage(ledger_db, "valuation", 1, [first, first, last]) == 2
    record_coverage(ledger_db, "fundamental", 1, [middle], fields=["pb"], complete=False)
    record_coverage(ledger_db, "capital_flow", 2, [middle])
    record_coverage(ledger_db, "lhb", 0, [middle])
    assert covered_dates(ledger_db, "fundamental", [1, 2], first, last) == {1: {first, last}, 2: set()}
    assert covered_dates(ledger_db, "valuation", [1], middle, last) == {1: {last}}
    assert covered_dates(ledger_db, "lhb", [0], first, last) == {0: {middle}}
    summary = {row["dataset"]: row for row in get_coverage_summary(ledger_db)}["fundamental"]
    assert summary["covered_points"] == 3
    assert summary["completed_points"] == 2
    assert summary["earliest_date"] == first
    assert summary["latest_completed_date"] == last


def test_repeated_observations_merge_fields_preserve_first_time_and_do_not_commit(ledger_db):
    day = date(2026, 10, 16)
    record_coverage(ledger_db, "etf", 1, [day], fields=["nav"], task_id="one")
    row = ledger_db.scalar(select(DataSyncCoverage))
    first = row.first_synced_at
    record_coverage(ledger_db, "etf", 1, [day], fields=["fund_size"], task_id="two", complete=False)
    assert row.first_synced_at == first
    assert row.last_synced_at >= first
    assert row.last_task_id == "two"
    assert row.completed is True
    assert set(json.loads(row.fields_json)) == {"nav", "fund_size"}
    assert ledger_db.scalar(select(func.count()).select_from(DataSyncCoverage)) == 1
    ledger_db.rollback()
    assert ledger_db.scalar(select(func.count()).select_from(DataSyncCoverage)) == 0


def test_partial_observation_has_execution_time_without_final_completion(ledger_db):
    day = date(2026, 10, 16)
    record_coverage(ledger_db, "etf", 1, [day], fields=["nav"], complete=False)
    summary, = get_coverage_summary(ledger_db)
    assert summary["first_synced_at"] is not None
    assert summary["last_synced_at"] is not None
    assert summary["completed_points"] == 0
    assert summary["earliest_completed_date"] is None
    assert covered_dates(ledger_db, "etf", [1], day, day) == {1: set()}


def test_batch_100_symbols_needs_one_prefetch_and_no_commit(ledger_db):
    day = date(2026, 10, 16)
    selects, commits = [], []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(ledger_db.bind, "before_cursor_execute", observe)
    event.listen(ledger_db, "after_commit", lambda *_: commits.append(True))
    points = {index: [day, day] for index in range(1, 101)}
    assert record_coverage_points(ledger_db, "etf", points, fields=["nav"]) == 100
    assert len(selects) == 1
    assert commits == []
    assert len(covered_dates(ledger_db, "etf", range(1, 101), day, day)) == 100
    ledger_db.rollback()
    assert ledger_db.scalar(select(func.count()).select_from(DataSyncCoverage)) == 0


def test_large_date_input_and_symbol_lookup_respect_parameter_budget(ledger_db):
    start = date(2020, 1, 1)
    dates = [start + timedelta(days=offset) for offset in range(1300)]
    assert record_coverage(ledger_db, "fundamental", 1, dates) == 1300
    result = covered_dates(ledger_db, "fundamental", range(1500), dates[0], dates[-1])
    assert result[1] == set(dates)
    assert result[1499] == set()


def test_empty_and_invalid_dates_cannot_accidentally_mark_a_scan_complete(ledger_db):
    assert record_coverage(ledger_db, "lhb", 0, []) == 0
    assert get_coverage_summary(ledger_db) == []
    with pytest.raises(ValueError):
        record_coverage(ledger_db, "etf", 1, [datetime(2026, 10, 16, 15)])
    with pytest.raises(ValueError):
        record_coverage(ledger_db, "etf", -1, [date(2026, 10, 16)])
    with pytest.raises(ValueError):
        record_coverage(ledger_db, "etf", 1, [date(2026, 10, 16)], fields="nav")
    with pytest.raises(ValueError):
        covered_dates(ledger_db, "etf", [1], date(2026, 10, 17), date(2026, 10, 16))


def _etf(day, *, symbol_id=1, stamp=None, **changes):
    values = dict(symbol_id=symbol_id, trade_date=day, nav=1.0, close=1.01,
                  premium_discount=1.0, fund_size=1000.0, total_shares=1000.0,
                  created_at=datetime(day.year, day.month, day.day, 23),
                  raw_json=json.dumps({"quote": {"updated_at": stamp}}))
    values.update(changes)
    return EtfIndicator(**values)


def test_etf_requires_actual_same_day_source_close_not_late_created_at(ledger_db):
    day = date(2026, 10, 16)
    stamps = {
        1: "2026-10-16T14:30:00+08:00",
        2: "2026-10-16T15:00:00+08:00",
        3: "2026-10-16T07:00:00+00:00",
        4: "2026-10-15T15:00:00+08:00",
        5: "2026-10-16T15:00:00",
        6: None,
    }
    ledger_db.add_all([_etf(day, symbol_id=symbol_id, stamp=stamp) for symbol_id, stamp in stamps.items()])
    ledger_db.flush()
    final = source_covered_dates(ledger_db, "etf", stamps, day, day)
    assert {key for key, days in final.items() if days} == {2, 3}
    complete = source_covered_dates(ledger_db, "etf", stamps, day, day, require_complete=False)
    assert all(days == {day} for days in complete.values())
    assert get_coverage_summary(ledger_db) == []  # Inspection remains read-only.


def test_missing_mode_detects_partial_metrics_but_any_existing_is_separate(ledger_db):
    day = date(2026, 10, 16)
    ledger_db.add_all([
        _etf(day, symbol_id=1, stamp="2026-10-16T15:00:00+08:00", fund_size=None),
        _etf(day, symbol_id=2, stamp="2026-10-16T15:00:00+08:00", tracking_error=None),
        _etf(day, symbol_id=3, stamp="2026-10-16T15:00:00+08:00", total_shares=0),
    ])
    ledger_db.flush()
    assert existing_dates(ledger_db, "etf", [1, 2, 3], day, day) == {1: {day}, 2: {day}, 3: {day}}
    assert source_covered_dates(ledger_db, "etf", [1, 2, 3], day, day, require_complete=False) == {
        1: set(), 2: {day}, 3: set(),
    }


def test_valuation_normal_keeps_loss_making_stock_complete_but_missing_can_fill_pe(ledger_db):
    day = date.today() - timedelta(days=5)
    ledger_db.add_all([
        StockValuation(symbol_id=1, trade_date=day, pe_ttm=None, pb=1, total_market_cap=100,
                       circulating_market_cap=None, source="eastmoney:valuation_daily",
                       raw_json=json.dumps({"_daily_complete": True})),
        StockValuation(symbol_id=2, trade_date=day, pe_ttm=10, pb=1, total_market_cap=100,
                       circulating_market_cap=90, source="akshare:stock_value_em"),
        StockValuation(symbol_id=3, trade_date=day, pe_ttm=10, pb=None, total_market_cap=100,
                       circulating_market_cap=90, source="akshare:stock_value_em"),
    ])
    ledger_db.flush()
    assert source_covered_dates(ledger_db, "fundamental", [1, 2, 3], day, day) == {
        1: {day}, 2: {day}, 3: set(),
    }
    assert source_covered_dates(ledger_db, "valuation", [1, 2, 3], day, day, require_complete=False) == {
        1: set(), 2: {day}, 3: set(),
    }


def test_historical_flow_may_be_final_but_explicit_intraday_time_stays_partial(ledger_db):
    day = date.today() - timedelta(days=5)
    values = dict(trade_date=day, main_net_inflow=0, main_net_inflow_pct=0,
                  super_large_net_inflow=0, large_net_inflow=0, medium_net_inflow=0,
                  small_net_inflow=0, source="akshare")
    ledger_db.add_all([
        CapitalFlow(symbol_id=1, **values, raw_json=json.dumps({"_history_complete": True})),
        CapitalFlow(symbol_id=2, **values, raw_json=json.dumps({"updated_at": f"{day}T14:30:00+08:00"})),
        CapitalFlow(symbol_id=3, **{**values, "main_net_inflow_pct": None}),
        CapitalFlow(symbol_id=4, **values),
    ])
    ledger_db.flush()
    assert source_covered_dates(ledger_db, "capital_flow", [1, 2, 3, 4], day, day) == {
        1: {day}, 2: set(), 3: set(), 4: set(),
    }
    assert source_covered_dates(ledger_db, "capital_flow", [1, 2, 3], day, day, require_complete=False) == {
        1: {day}, 2: {day}, 3: set(),
    }


def test_event_and_financial_rows_do_not_prove_a_complete_daily_scan(ledger_db):
    day = date(2026, 10, 16)
    ledger_db.add_all([
        StockFinancialReport(symbol_id=1, report_period=date(2026, 6, 30), announcement_date=day,
                             report_type="interim", roe_ttm=1),
        LhbInstitutionTrade(symbol="SH600519", trade_date=day, institution_buy=1),
        StockHotRankSnapshot(symbol="SH600519", trade_date=day, hot_rank=1, hot_rank_total=100, hot_rank_pct=1),
    ])
    ledger_db.flush()
    for dataset, ids in (("financial", [1]), ("lhb", [0]), ("hot_rank", [0])):
        key = ids[0]
        assert source_covered_dates(ledger_db, dataset, ids, day, day) == {key: set()}
        assert existing_dates(ledger_db, dataset, ids, day, day) == {key: {day}}
    assert get_coverage_summary(ledger_db) == []


def test_policy_subtraction_keeps_interior_holes_and_splits_at_covered_days():
    from app.services.external_sync_policy import subtract_ranges

    days = [date(2026, 10, day) for day in (12, 13, 14, 15, 16, 19)]
    assert subtract_ranges(days, {days[0], days[3], days[-1]}) == [
        (date(2026, 10, 13), date(2026, 10, 14)),
        (date(2026, 10, 16), date(2026, 10, 16)),
    ]
    assert subtract_ranges(days, set(days)) == []
    assert subtract_ranges((), set()) == []


def test_policy_calendar_excludes_closures_but_keeps_financial_announcement_dates(ledger_db):
    from app.services.external_sync_policy import requested_days

    start, end = date(2026, 9, 30), date(2026, 10, 5)
    ledger_db.add_all([
        MarketCalendar(trade_date=date(2026, 10, 1), is_trading_day=0),
        MarketCalendar(trade_date=date(2026, 10, 2), is_trading_day=0),
        MarketCalendar(trade_date=date(2026, 10, 3), is_trading_day=1),
    ])
    ledger_db.flush()
    assert requested_days(ledger_db, "fundamental", start, end) == (start, end)
    assert requested_days(ledger_db, "capital_flow", date(2026, 10, 1), date(2026, 10, 4)) == ()
    assert requested_days(ledger_db, "financial", start, end) == tuple(
        start + timedelta(days=offset) for offset in range(6)
    )


def test_policy_does_not_let_ledger_hide_missing_source_rows(ledger_db, monkeypatch):
    import app.services.external_sync_policy as policy

    days = [date(2026, 10, day) for day in (12, 13, 14, 15, 16)]
    monkeypatch.setattr(policy, "source_covered_dates", lambda *args, require_complete: {
        1: {days[-1]} if require_complete else {days[0], days[2], days[-1]}
    })
    monkeypatch.setattr(policy, "covered_dates", lambda *args: {1: {days[0], days[2], days[3]}})
    gaps = [(days[1], days[1]), (days[3], days[3])]
    # Old completion flags cannot promote the current intraday source rows.
    assert policy.resolve_policy_ranges(ledger_db, "etf", [1], days[0], days[-1], "normal") == {
        1: [(days[0], days[3])]
    }
    assert policy.resolve_policy_ranges(ledger_db, "etf", [1], days[0], days[-1], "missing") == {1: gaps}
    assert policy.resolve_policy_ranges(ledger_db, "etf", [1], days[0], days[-1], "force") == {
        1: [(days[0], days[-1])]
    }
    assert policy.resolve_policy_ranges(ledger_db, "financial", [1], days[0], days[-1], "missing") == {
        1: [(days[0], days[-1])]
    }
