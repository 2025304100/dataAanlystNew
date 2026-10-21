"""Historical status preparation through the public API and PIT read boundary."""
from datetime import date, datetime, timedelta
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol
from app.models.market_data import MarketCalendar
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.models.score import Score
from app.services.security_status.pit_service import SecurityStatusPitService
from app.schemas.bfg_precheck import BacktestPrecheckRequest
from app.services.bfg_precheck_service import run_backtest_precheck
from app.models.audit import DataGovernanceAuditEvent
from sqlalchemy import select
from app.services.security_status.local_import import (
    get_imported_execution_constraints, get_imported_execution_constraints_for_range,
)

pytestmark = pytest.mark.whitebox


@pytest.fixture
def status_client(db_session):
    from app.api.routes.security_status import router
    from app.db.session import get_db

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def source_file(**overrides):
    return {
        "csv_text": (
            "symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date\n"
            "600000,2025-09-01,0,0,0,1,1999-11-10,\n"
            "600000,2025-09-02,1,0,0,1,1999-11-10,\n"
        ),
        "filename": "provider-history.csv", "source_name": "provider_archive",
        "source_reference": "archive://securities/status-daily/20250902",
        "source_version": "release-20250902", "source_updated_at": "2025-09-02T18:00:00+08:00",
        **overrides,
    }


@pytest.fixture
def security_symbol(db_session):
    symbol = Symbol(symbol="600000", name="浦发银行", asset_type="stock", market="SH")
    db_session.add(symbol)
    db_session.commit()
    return symbol


@pytest.mark.parametrize("source", [
    "unknown", "missing_historical", "inferred_current_snapshot",
    "raw_universe_snapshot", "T26_FIXTURE", "T30_FIXTURE", "test",
    "current", "realtime", "raw_universe",
])
def test_placeholder_rows_do_not_certify_historical_listing(db_session, security_symbol, source):
    db_session.add(SecurityStatusDaily(
        symbol_id=security_symbol.id, trade_date=date(2025, 9, 1),
        listing_date=date(1999, 11, 10), status_source=source,
    ))
    db_session.commit()

    status = SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1))
    assert status.status == "UNKNOWN"
    assert SecurityStatusPitService.status_batch(
        db_session, [security_symbol.id], date(2025, 9, 1),
    )[security_symbol.id].status == "UNKNOWN"


def test_daily_file_preview_import_and_read_preserve_historical_changes(
    status_client, db_session, security_symbol,
):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_import"] is True
    assert preview.json()["insert_count"] == 2
    # Preview must leave the database untouched.
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "UNKNOWN"

    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview.json()["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 200, result.text
    assert result.json()["inserted"] == 2
    assert result.json()["replaced_unknown"] == 0
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "LISTED"
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 2)).status == "ST"
    # The next unprovided day remains unknown; no forward fill is allowed.
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 3)).status == "UNKNOWN"

    receipt = status_client.get(f"/api/v1/security-status/imports/{result.json()['batch_id']}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["source_reference"] == payload["source_reference"]
    assert receipt.json()["source_version"] == "release-20250902"
    assert receipt.json()["content_sha256"] == preview.json()["content_sha256"]


def test_coverage_counts_each_symbol_day_and_leaves_missing_days_unknown(
    status_client, db_session, security_symbol,
):
    other = Symbol(symbol="600001", name="其他股票", asset_type="stock", market="SH")
    db_session.add(other)
    db_session.add_all([
        MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1),
        MarketCalendar(trade_date=date(2025, 9, 2), is_trading_day=1),
    ])
    db_session.commit()
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    assert status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
    }).status_code == 200

    overview = status_client.get("/api/v1/security-status/coverage")
    assert overview.status_code == 200, overview.text
    assert overview.json()["total_records"] == 2
    assert overview.json()["verified_records"] == 2
    assert overview.json()["coverage_ratio"] is None

    result = status_client.get("/api/v1/security-status/coverage", params=[
        ("start_date", "2025-09-01"), ("end_date", "2025-09-02"),
        ("symbol_ids", security_symbol.id), ("symbol_ids", other.id),
    ])
    assert result.status_code == 200, result.text
    assert result.json()["total_expected"] == 4
    assert result.json()["verified"] == 2
    assert result.json()["missing_or_unverified"] == 2
    assert result.json()["coverage_ratio"] == 0.5
    assert result.json()["missing_sample"] == [
        {"symbol_id": other.id, "symbol": "600001", "trade_date": "2025-09-01", "reason": "未提供当天资料"},
        {"symbol_id": other.id, "symbol": "600001", "trade_date": "2025-09-02", "reason": "未提供当天资料"},
    ]


def _precheck(db_session, ids):
    return run_backtest_precheck(db_session, BacktestPrecheckRequest(
        portfolio_id=1, symbol_ids=ids, start_date=date(2025, 9, 1),
        end_date=date(2025, 9, 1), minimum_trade_days=1,
    ))


def test_precheck_does_not_treat_one_symbol_as_a_complete_market_day(db_session, security_symbol):
    other = Symbol(symbol="600001", name="其他股票", asset_type="stock", market="SH")
    db_session.add(other)
    db_session.add(MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1))
    db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, 1),
        listing_date=date(1999, 11, 10), status_source="provider_archive"))
    db_session.commit()
    result = _precheck(db_session, [security_symbol.id, other.id])
    assert "STATUS_DATA_UNAVAILABLE" in {blocker.code for blocker in result.blocking_reasons}
    assert result.excluded_symbol_days.status_unknown == 1


@pytest.mark.parametrize("source", ["T26_FIXTURE", "unknown", "inferred_current_snapshot"])
def test_precheck_does_not_accept_placeholder_rows_as_coverage(db_session, security_symbol, source):
    db_session.add(MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1))
    db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, 1),
        listing_date=date(1999, 11, 10), status_source=source))
    db_session.commit()
    result = _precheck(db_session, [security_symbol.id])
    assert "STATUS_DATA_UNAVAILABLE" in {blocker.code for blocker in result.blocking_reasons}
    assert result.excluded_symbol_days.status_unknown == 1
    assert result.data_cutoff.status_date is None


def test_changed_file_and_missing_personal_confirmation_cannot_be_imported(status_client, db_session, security_symbol):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    result = status_client.post("/api/v1/security-status/import", json={
        **source_file(csv_text=payload["csv_text"].replace("2025-09-02,1", "2025-09-02,0")),
        "preview_sha256": preview["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 400
    for confirmed in (False, None):
        body = {**payload, "preview_sha256": preview["content_sha256"]}
        if confirmed is not None:
            body["confirmed"] = confirmed
        assert status_client.post("/api/v1/security-status/import", json=body).status_code == 422
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "UNKNOWN"


def test_database_write_failure_rolls_back_every_row_and_its_receipt(status_client, db_session, security_symbol):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()

    def fail_after_database_flush(session, _context):
        raise RuntimeError("simulated disk failure after rows were flushed")

    event.listen(db_session, "after_flush", fail_after_database_flush)
    try:
        result = status_client.post("/api/v1/security-status/import", json={
            **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
        })
        assert result.status_code == 500
    finally:
        event.remove(db_session, "after_flush", fail_after_database_flush)
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "UNKNOWN"
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 2)).status == "UNKNOWN"
    assert status_client.get(f"/api/v1/security-status/imports/{preview['batch_id']}").status_code == 404


@pytest.mark.parametrize("change", [
    {"csv_text": source_file()["csv_text"].replace("2025-09-02,1,0,0,1", "2025-09-02,,0,0,1")},
    {"csv_text": source_file()["csv_text"] + "600000,2025-09-02,1,0,0,1,1999-11-10,\n"},
    {"csv_text": source_file()["csv_text"].replace("600000,2025-09-02", "不存在代码,2025-09-02")},
    {"source_name": "T26_FIXTURE"},
    {"source_name": "inferred_current_snapshot"},
    {"source_name": "unknown"},
    {"source_version": "latest"},
])
def test_invalid_file_rejects_entire_batch_without_partial_success(status_client, db_session, security_symbol, change):
    payload = source_file(**change)
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["can_import"] is False
    assert preview.json()["errors"]
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview.json()["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 400
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "UNKNOWN"


def test_unknown_rows_can_be_replaced_but_reliable_conflicts_stop_the_whole_import(
    status_client, db_session, security_symbol,
):
    db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, 1),
        listing_date=date(1999, 11, 10), status_source="T26_FIXTURE", is_st=True))
    db_session.commit()
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    assert preview["replace_unknown_count"] == 1
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 200, result.text
    assert result.json()["inserted"] == 1
    assert result.json()["replaced_unknown"] == 1
    repeated = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    assert repeated["unchanged_count"] == 2
    assert repeated["insert_count"] == 0

    conflict = source_file(csv_text=payload["csv_text"].replace("2025-09-02,1", "2025-09-02,0")
        + "600000,2025-09-03,0,0,0,1,1999-11-10,\n")
    preview = status_client.post("/api/v1/security-status/import/preview", json=conflict).json()
    assert preview["can_import"] is False
    assert status_client.post("/api/v1/security-status/import", json={
        **conflict, "preview_sha256": preview["content_sha256"], "confirmed": True,
    }).status_code == 400
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 2)).status == "ST"
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 3)).status == "UNKNOWN"


def test_exact_preview_hash_binds_even_a_file_only_whitespace_change(status_client, db_session, security_symbol):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    result = status_client.post("/api/v1/security-status/import", json={
        **source_file(csv_text=payload["csv_text"] + "\n"),
        "preview_sha256": preview["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 400


def test_non_calendar_date_spelling_is_rejected_in_the_daily_file(status_client, security_symbol):
    payload = source_file(csv_text=source_file()["csv_text"].replace("2025-09-01", "20250901"))
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.json()["can_import"] is False


def test_import_then_precheck_rechecks_the_same_database_and_cutoff(status_client, db_session, security_symbol):
    other = Symbol(symbol="600001", name="其他股票", asset_type="stock", market="SH")
    db_session.add(other)
    db_session.add(MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1))
    db_session.commit()
    ids = [security_symbol.id, other.id]
    before = _precheck(db_session, ids)
    assert "STATUS_DATA_UNAVAILABLE" in {item.code for item in before.blocking_reasons}
    assert before.excluded_symbol_days.status_unknown == 2
    assert before.data_cutoff.status_date is None

    payload = source_file(csv_text=(
        "symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date\n"
        "600000,2025-09-01,0,0,0,1,1999-11-10,\n"
        "600001,2025-09-01,0,0,0,1,2000-01-01,\n"
    ))
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 200, result.text
    after = _precheck(db_session, ids)
    assert "STATUS_DATA_UNAVAILABLE" not in {item.code for item in after.blocking_reasons}
    assert after.excluded_symbol_days.status_unknown == 0
    assert after.data_cutoff.status_date == date(2025, 9, 1)
    # A status-only file closes the status gap but cannot certify daily limits.
    assert {item.code for item in after.blocking_reasons} == {"PRICE_LIMIT_DATA_UNAVAILABLE"}


def test_high_coverage_does_not_allow_remaining_unknown_days_in_formal_backtest(db_session, security_symbol):
    symbols = [security_symbol]
    for number in range(1, 4):
        symbol = Symbol(symbol=f"60000{number}", name=f"股票{number}", asset_type="stock", market="SH")
        db_session.add(symbol)
        symbols.append(symbol)
    db_session.flush()
    db_session.add(MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1))
    for symbol in symbols[:3]:
        db_session.add(SecurityStatusDaily(symbol_id=symbol.id, trade_date=date(2025, 9, 1),
            listing_date=date(1999, 11, 10), status_source="provider_archive"))
    db_session.commit()
    result = _precheck(db_session, [symbol.id for symbol in symbols])
    assert "STATUS_DATA_UNAVAILABLE" in {blocker.code for blocker in result.blocking_reasons}
    assert result.excluded_symbol_days.status_unknown == 1


def test_imported_records_do_not_certify_missing_calendar_days(status_client, db_session, security_symbol):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    assert status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
    }).status_code == 200
    db_session.add(MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1))
    db_session.commit()
    coverage = status_client.get("/api/v1/security-status/coverage", params=[
        ("start_date", "2025-09-01"), ("end_date", "2025-09-02"), ("symbol_ids", security_symbol.id),
    ])
    assert coverage.status_code == 200
    assert coverage.json()["calendar_available"] is False
    assert coverage.json()["coverage_ratio"] is None


def test_import_revalidates_a_conflict_created_after_preview(status_client, db_session, security_symbol):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, 2),
        listing_date=date(1999, 11, 10), status_source="provider_archive", is_st=False))
    db_session.commit()
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 400
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "UNKNOWN"
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 2)).status == "LISTED"


def test_local_history_retains_suspension_delisting_period_and_actual_delisting(
    status_client, db_session, security_symbol,
):
    payload = source_file(csv_text=source_file()["csv_text"] + (
        "600000,2025-09-03,0,1,0,1,1999-11-10,\n"
        "600000,2025-09-04,0,0,1,1,1999-11-10,\n"
        "600000,2025-09-05,0,0,0,0,1999-11-10,2025-09-05\n"
    ), source_updated_at="2025-09-05T18:00:00+08:00")
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 200, result.text
    assert [SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, day)).status
        for day in range(1, 6)] == ["LISTED", "ST", "SUSPENDED", "DELISTING_PERIOD", "DELISTED"]
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).delisting_date is None


def test_future_delisting_date_is_not_imported_into_an_earlier_snapshot(status_client, security_symbol):
    payload = source_file(csv_text=source_file()["csv_text"].replace("1999-11-10,\n", "1999-11-10,2025-09-05\n"))
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.json()["can_import"] is False


def test_daily_execution_constraints_are_read_from_the_same_confirmed_file(status_client, db_session, security_symbol):
    from app.services.security_status.local_import import get_imported_execution_constraints

    payload = source_file(csv_text=(
        "symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,"
        "price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n"
        "600000,2025-09-01,0,0,0,1,1999-11-10,,LIMITED,11,9,0.01\n"
        "600000,2025-09-02,1,0,0,1,1999-11-10,,LIMITED,10.5,9.5,0.01\n"
    ))
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1)) is None
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_import"] is True, preview.json()
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview.json()["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 200, result.text
    constraint = get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1))
    assert constraint is not None
    assert {key: constraint[key] for key in ("price_limit_mode", "upper_limit_price", "lower_limit_price", "tick_size")} == {
        "price_limit_mode": "LIMITED", "upper_limit_price": 11.0, "lower_limit_price": 9.0, "tick_size": 0.01,
    }
    assert constraint["content_sha256"] == preview.json()["content_sha256"]
    assert constraint["batch_id"] == result.json()["batch_id"]
    assert constraint["source_reference"] == payload["source_reference"]
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 2))["upper_limit_price"] == 10.5
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 3)) is None


def execution_file(*, rows=None, **overrides):
    return source_file(csv_text=(
        "symbol,trade_date,is_st,is_suspended,is_delisting_period,is_listed,listing_date,delisting_date,"
        "price_limit_mode,upper_limit_price,lower_limit_price,tick_size\n"
        + (rows if rows is not None else "600000,2025-09-01,0,0,0,1,1999-11-10,,LIMITED,11,9,0.01\n")
    ), **overrides)


def import_public(status_client, payload):
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_import"], preview.text
    result = status_client.post("/api/v1/security-status/import", json={
        **payload, "preview_sha256": preview.json()["content_sha256"], "confirmed": True,
    })
    assert result.status_code == 200, result.text
    return result.json()


def test_old_status_only_file_does_not_provide_estimated_execution_constraints(status_client, db_session, security_symbol):
    import_public(status_client, source_file())
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1)) is None
    result = status_client.get(f"/api/v1/security-status/{security_symbol.id}/execution-constraints", params={"trade_date": "2025-09-01"})
    assert result.status_code == 200
    assert result.json()["available"] is False
    assert result.json()["constraints"] is None


def test_explicit_no_limit_is_preserved_without_inventing_prices(status_client, db_session, security_symbol):
    import_public(status_client, execution_file(rows="600000,2025-09-01,0,0,0,1,1999-11-10,,NO_LIMIT,,,0.01\n"))
    value = get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1))
    assert value["price_limit_mode"] == "NO_LIMIT"
    assert value["upper_limit_price"] is None and value["lower_limit_price"] is None
    assert value["tick_size"] == 0.01
    assert value["is_st"] is False and value["is_suspended"] is False
    assert value["is_tradable"] is True and value["is_delisted"] is False


@pytest.mark.parametrize("limits", [
    "LIMITED,,9,0.01", "LIMITED,11,,0.01", "LIMITED,11,9,", "LIMITED,NaN,9,0.01",
    "LIMITED,Infinity,9,0.01", "LIMITED,11,9,0", "LIMITED,9,11,0.01",
    "LIMITED,11.001,9,0.01", "NO_LIMIT,11,9,0.01", ",11,9,0.01",
])
def test_incomplete_or_nonfinite_daily_limits_block_the_whole_file(status_client, security_symbol, limits):
    payload = execution_file(rows=f"600000,2025-09-01,0,0,0,1,1999-11-10,,{limits}\n")
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_import"] is False
    assert preview.json()["errors"]


@pytest.mark.parametrize("mutation", ["status_value", "source", "source_time", "batch", "audit_values", "audit_missing"])
def test_limits_are_rejected_when_current_status_or_its_source_receipt_changes(
    status_client, db_session, security_symbol, mutation,
):
    result = import_public(status_client, execution_file())
    cache = {}
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache)
    row = db_session.scalar(select(SecurityStatusDaily).where(SecurityStatusDaily.symbol_id == security_symbol.id))
    if mutation == "status_value":
        row.is_st = True
    elif mutation == "source":
        row.status_source = "historical_csv:other_archive"
    elif mutation == "source_time":
        row.source_updated_at = datetime(2025, 9, 3)
    elif mutation == "batch":
        row.as_of_batch_id = "status_csv_other"
    else:
        audit = db_session.scalar(select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.business_key == f"{result['batch_id']}:execution:0"))
        if mutation == "audit_missing":
            db_session.delete(audit)
        else:
            content = json.loads(audit.after_json)
            content["rows"][0]["upper_limit_price"] = 99
            audit.after_json = json.dumps(content)
    db_session.commit()
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache) is None
    assert status_client.get(f"/api/v1/security-status/{security_symbol.id}/execution-constraints", params={"trade_date": "2025-09-01"}).json()["available"] is False


def test_limits_can_only_supplement_the_same_declared_status_source(status_client, db_session, security_symbol):
    import_public(status_client, source_file(csv_text=source_file()["csv_text"].splitlines(True)[0] + source_file()["csv_text"].splitlines(True)[1]))
    cache = {}
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache) is None
    different = execution_file(source_reference="archive://other-release")
    assert status_client.post("/api/v1/security-status/import/preview", json=different).json()["can_import"] is False
    result = import_public(status_client, execution_file())
    assert result["attached_constraints"] == 1
    constraint = get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache)
    assert constraint["batch_id"] == result["batch_id"]
    assert constraint["upper_limit_price"] == 11
    repeated = import_public(status_client, execution_file())
    assert repeated["inserted"] == 0
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1))["upper_limit_price"] == 11
    conflict = execution_file(rows="600000,2025-09-01,0,0,0,1,1999-11-10,,LIMITED,12,8,0.01\n")
    assert status_client.post("/api/v1/security-status/import/preview", json=conflict).json()["can_import"] is False


def test_many_original_limits_use_bounded_audit_chunks_and_range_reads(status_client, db_session, security_symbol):
    rows = "".join(f"600000,{date(2025, 1, 1) + timedelta(days=number)},0,0,0,1,1999-11-10,,LIMITED,11,9,0.01\n"
        for number in range(200))
    sizes = []
    def enforce_audit_text_capacity(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO data_governance_audit_events" not in statement:
            return
        for parameters in context.compiled_parameters:
            value = parameters.get("after_json") or ""
            if '"kind":"security_status_execution_constraints"' in value:
                size = len(value.encode("utf-8"))
                if size > 32000:
                    raise RuntimeError("external storage capacity exceeded")
                sizes.append(size)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", enforce_audit_text_capacity)
    try:
        receipt = import_public(status_client, execution_file(rows=rows))
    finally:
        event.remove(engine, "before_cursor_execute", enforce_audit_text_capacity)
    assert len(receipt["execution_constraint_hashes"]) >= 2
    assert sizes and max(sizes) <= 32000
    values = get_imported_execution_constraints_for_range(db_session, [security_symbol.id], date(2025, 1, 1), date(2025, 7, 20))
    assert len(values) == 200
    assert values[(security_symbol.id, date(2025, 1, 1))]["upper_limit_price"] == 11
    assert values[(security_symbol.id, date(2025, 7, 19))]["batch_id"] == receipt["batch_id"]
    assert (security_symbol.id, date(2025, 7, 20)) not in values


def test_receipt_failure_rolls_back_new_status_batch_pointer_and_limit_chunks(status_client, db_session, security_symbol):
    old = import_public(status_client, source_file())
    cache = {}
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache) is None
    payload = execution_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    flushes = 0
    def fail_after_limit_chunk(session, context):
        nonlocal flushes
        flushes += 1
        if flushes == 2:
            raise RuntimeError("disk failure while saving original source receipt")
    event.listen(db_session, "after_flush", fail_after_limit_chunk)
    try:
        result = status_client.post("/api/v1/security-status/import", json={
            **payload, "preview_sha256": preview["content_sha256"], "confirmed": True,
        })
        assert result.status_code == 500
    finally:
        event.remove(db_session, "after_flush", fail_after_limit_chunk)
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache) is None
    assert status_client.get(f"/api/v1/security-status/imports/{preview['batch_id']}").status_code == 404
    assert status_client.get(f"/api/v1/security-status/imports/{old['batch_id']}").status_code == 200
    receipt = import_public(status_client, payload)
    assert get_imported_execution_constraints(db_session, security_symbol.id, date(2025, 9, 1), cache=cache)["batch_id"] == receipt["batch_id"]


@pytest.mark.parametrize("confirmed", [1, "true", False, "omitted"])
def test_import_requires_json_boolean_true_for_personal_confirmation(status_client, db_session, security_symbol, confirmed):
    payload = source_file()
    preview = status_client.post("/api/v1/security-status/import/preview", json=payload).json()
    body = {**payload, "preview_sha256": preview["content_sha256"]}
    if confirmed != "omitted":
        body["confirmed"] = confirmed
    result = status_client.post("/api/v1/security-status/import", json=body)
    assert result.status_code == 422, result.text
    assert SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, 1)).status == "UNKNOWN"


def test_coverage_overview_and_dated_pit_agree_on_complete_nonplaceholder_rows(status_client, db_session, security_symbol):
    evidence = [
        ("provider_archive", date(1999, 11, 10)),
        ("unknown", date(1999, 11, 10)),
        ("T26_FIXTURE", date(1999, 11, 10)),
        ("inferred_current_snapshot", date(1999, 11, 10)),
        ("provider_archive", None),
        (" ", date(1999, 11, 10)),
        ("provider_archive", date(2026, 1, 1)),
        ("  provider_archive  ", date(1999, 11, 10)),
    ]
    for number, (source, listed_at) in enumerate(evidence, start=1):
        db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, number),
            listing_date=listed_at, status_source=source))
    db_session.commit()
    assert [SecurityStatusPitService.status_at(db_session, security_symbol.id, date(2025, 9, number)).status
        for number in range(1, 9)] == ["LISTED", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "LISTED"]
    result = status_client.get("/api/v1/security-status/coverage")
    assert result.status_code == 200
    assert result.json()["total_records"] == 8
    assert result.json()["verified_records"] == 2
    assert result.json()["first_date"] == "2025-09-01"
    assert result.json()["latest_date"] == "2025-09-08"
    assert result.json()["coverage_ratio"] is None


@pytest.mark.parametrize("missing,expected", [
    ("bars", None), ("status", None), ("factors", None), (None, date(2025, 9, 1)),
])
def test_precheck_combines_latest_dates_only_when_all_three_datasets_are_present(db_session, security_symbol, missing, expected):
    if missing != "bars":
        universe_symbol = UniverseSymbol(symbol="600000", asset_type="stock", market="sh", region="cn")
        db_session.add(universe_symbol)
        db_session.flush()
        db_session.add(UniverseDailyBar(universe_symbol_id=universe_symbol.id, trade_date=date(2025, 9, 3),
            open=10, high=11, low=9, close=10, volume=100000, amount=1000000))
    if missing != "factors":
        db_session.add(Score(symbol_id=security_symbol.id, trade_date=date(2025, 9, 2),
            quality_score=80, quality_grade="A", timing_score=80, stage="growth", action="hold", priority_score=80))
    # A stored test row is not usable historical state, matching the actual old fixture inventory.
    db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, 1),
        listing_date=date(1999, 11, 10), status_source="T26_FIXTURE" if missing == "status" else "provider_archive"))
    db_session.add(MarketCalendar(trade_date=date(2025, 9, 1), is_trading_day=1))
    db_session.commit()
    result = _precheck(db_session, [security_symbol.id])
    assert result.data_cutoff.bars_date == (None if missing == "bars" else date(2025, 9, 3))
    assert result.data_cutoff.status_date == (None if missing == "status" else date(2025, 9, 1))
    assert result.data_cutoff.factors_date == (None if missing == "factors" else date(2025, 9, 2))
    assert result.data_cutoff.unified_earliest == expected
    if expected is None:
        assert "资料不完整" in result.cutoff_warning_zh


def test_overview_gives_a_next_step_that_exists_on_the_import_page(status_client, db_session, security_symbol):
    overview = status_client.get("/api/v1/security-status/coverage")
    assert overview.json()["message"] == "还没有可用于回测的历史股票状态资料。取得逐日原始资料后，可在这里导入。"
    db_session.add(SecurityStatusDaily(symbol_id=security_symbol.id, trade_date=date(2025, 9, 1),
        listing_date=date(1999, 11, 10), status_source="T26_FIXTURE"))
    db_session.commit()
    assert status_client.get("/api/v1/security-status/coverage").json()["message"] == "还没有可用于回测的历史股票状态资料。取得逐日原始资料后，可在这里导入。"
    import_public(status_client, source_file())
    assert status_client.get("/api/v1/security-status/coverage").json()["message"] == "这里只显示本机保存的历史资料。是否足以回测，请返回回测页重新检查所选范围。"
