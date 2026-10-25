from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.models.market_data import MarketCalendar
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.source_field_integrity import get_source_field_integrity
from app.services.task_outcomes import external_sync_result

import pytest

pytestmark = pytest.mark.whitebox



def task(**changes):
    return SimpleNamespace(**(dict(task_type="external_sync_fundamental", status="done",
        ok_count=0, failed_count=0, total=0, processed=0) | changes))


@pytest.mark.parametrize("reason", ["market_closed", "already_current", "no_new_data", "no_events"])
def test_explicit_zero_unit_skips_keep_original_counters_and_retry_state(reason):
    original = dict(total=0, success=0, failed=0, skipped=0, records=0, skip_reason=reason)
    before = deepcopy(original)
    record = task()
    result = external_sync_result(record, original)
    assert result["outcome"] == "skipped" and result["skip_ratio"] == 0
    assert result["total"] == result["skipped"] == result["records"] == 0
    assert original == before and record.status == "done"


@pytest.mark.parametrize("state,extra,outcome", [
    ("cancelled", {}, "cancelled"),
    ("failed", {}, "failed"),
    ("done", {"failed": 1}, "failed"),
    ("done", {"warehouse_mirror": {"status": "warning"}}, "failed"),
    ("done", {"quality_snapshot": {"status": "warning"}}, "failed"),
    ("done", {"mirror_retry": True, "warehouse_mirror": {"status": "done"}}, "success"),
    ("done", {"records": 3, "success": 1}, "success"),
    ("failed", {"records": 3, "success": 1}, "partial_success"),
])
def test_skip_reason_does_not_hide_failure_cancel_or_completed_mirror_work(state, extra, outcome):
    result = external_sync_result(task(status=state), {"skip_reason": "market_closed", **extra})
    assert result["outcome"] == outcome


def test_known_unavailable_response_is_not_a_successful_zero_row_skip():
    record = task()
    assert external_sync_result(record, {"skip_reason": "data_unavailable", "records": 0})["outcome"] == "failed"
    assert record.status == "done"
    # No source inserts is valid for a successful mirror-only retry.
    assert external_sync_result(record, {"mirror_retry": True, "records": 0})["outcome"] == "success"


NOW = datetime(2026, 10, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


def seed(db):
    etf = UniverseSymbol(symbol="510300", asset_type="etf", market="sh", region="cn",
                         last_bar_date=date(2026, 10, 1), bar_count=1)
    unrelated = UniverseSymbol(symbol="UNRELATED", asset_type="stock", market="us", region="us",
                               last_bar_date=date(2099, 1, 1))
    stock = Symbol(symbol="600000", name="test", market="sh", asset_type="stock")
    db.add_all([etf, unrelated, stock]); db.flush()
    for day in [date(2026, 9, 25), date(2026, 10, 21), date(2026, 10, 23)]:
        db.add(UniverseDailyBar(universe_symbol_id=etf.id, trade_date=day, open=4, high=5,
                               low=3, close=4, source="original"))
    db.add(StockValuation(symbol_id=stock.id, trade_date=date(2026, 10, 20), pe_ttm=10, source="original"))
    for i in range(7):
        day = date(2026, 10, 19) + timedelta(days=i)
        db.add(MarketCalendar(trade_date=day, is_trading_day=int(day.weekday() < 5)))
    db.commit()


def test_audit_fixed_window_independent_sources_and_cache_drift_are_read_only(db_session):
    seed(db_session)
    models = (UniverseSymbol, UniverseDailyBar, StockValuation, MarketCalendar)
    before = {m.__name__: db_session.execute(select(m.__table__)).all() for m in models}
    def reject_write(conn, cursor, statement, params, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT"), statement
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", reject_write)
    try:
        report = get_source_field_integrity(db_session, now=NOW)
        historical = get_source_field_integrity(db_session, as_of=date(2026, 10, 22), now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", reject_write)
    assert report["as_of"] == date(2026, 10, 25)
    assert report["window_start"] == date(2026, 9, 26)
    assert report["expected_latest_date"] == date(2026, 10, 23)
    assert report["window_basis"] == "asia_shanghai_today"
    etf, valuation = report["sources"]
    assert etf["latest_date"] == date(2026, 10, 23)
    assert etf["cache_latest_date"] == date(2026, 10, 1)
    assert etf["warnings"] == ["cache_date_mismatch"]
    assert valuation["latest_date"] == date(2026, 10, 20)
    assert valuation["warnings"] == ["source_behind_calendar"]
    rows = {(r["table"], r["field"]): r for r in report["fields"]}
    assert rows["universe_daily_bars", "trade_date"]["total"] == 2
    assert historical["as_of"] == date(2026, 10, 22)
    assert historical["sources"][0]["latest_date"] == date(2026, 10, 23)
    assert historical["sources"][0]["latest_date_as_of"] == date(2026, 10, 21)
    assert "source_behind_calendar" in historical["sources"][0]["warnings"]
    assert {m.__name__: db_session.execute(select(m.__table__)).all() for m in models} == before


def test_unknown_calendar_and_empty_sources_are_not_reported_complete(db_session):
    report = get_source_field_integrity(db_session, now=NOW)
    assert report["as_of"] == NOW.date() and report["expected_latest_date"] is None
    assert all(s["warnings"] == ["calendar_unverified", "no_rows_as_of"] for s in report["sources"])
    assert all(row["coverage_pct"] is None for row in report["fields"])


def test_mysql_conditional_date_aggregate_strings_are_normalized(db_session, monkeypatch):
    from unittest.mock import Mock
    seed(db_session)
    execute = db_session.execute
    def driver_result(statement, *args, **kwargs):
        result = execute(statement, *args, **kwargs)
        if "CASE WHEN" in str(statement):
            wrapped = Mock(wraps=result)
            wrapped.one.return_value = tuple(v.isoformat() if isinstance(v, date) else v for v in result.one())
            return wrapped
        return result
    monkeypatch.setattr(db_session, "execute", driver_result)
    report = get_source_field_integrity(db_session, now=NOW)
    assert report["sources"][0]["latest_date_as_of"] == date(2026, 10, 23)
    assert report["sources"][1]["warnings"] == ["source_behind_calendar"]


def test_intraday_audit_does_not_expect_an_unfinished_trading_session(db_session):
    seed(db_session)
    report = get_source_field_integrity(db_session, now=NOW.replace(day=23, hour=14))
    assert report["as_of"] == date(2026, 10, 23)
    assert report["expected_latest_date"] == date(2026, 10, 22)


def test_audit_http_explicit_date_and_invalid_dates(db_session, monkeypatch):
    from app.api.routes.external_data import router
    from app.db.session import get_db
    from app.services import source_field_integrity
    seed(db_session)
    actual = get_source_field_integrity
    monkeypatch.setattr(source_field_integrity, "get_source_field_integrity",
                        lambda db, as_of=None: actual(db, as_of=as_of, now=NOW))
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.get("/external-data/source-field-integrity?as_of=2026-10-22")
        assert response.status_code == 200
        assert response.json()["as_of"] == "2026-10-22"
        assert response.json()["window_basis"] == "explicit_as_of"
        assert client.get("/external-data/source-field-integrity?as_of=2026-10-26").status_code == 422
        assert client.get("/external-data/source-field-integrity?as_of=bad-date").status_code == 422
