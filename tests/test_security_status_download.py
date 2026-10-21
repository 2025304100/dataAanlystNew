"""Bounded historical-source downloads through the public HTTP interface."""
from datetime import date, datetime, timezone
import hashlib
import io
import json
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import PortfolioMember
from app.models.symbol import Symbol
from uuid import uuid4

pytestmark = [pytest.mark.whitebox, pytest.mark.reap_workers]


@pytest.fixture
def download_client(db_session):
    from app.api.routes.security_status import router
    from app.db.session import get_db
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def portfolio(db_session):
    p = Portfolio(name="个人组合", account_type="simulated", total_capital=100000,
                  investable_ratio=1, cash_reserve_ratio=0)
    db_session.add(p)
    db_session.flush()
    for code, market, status, held in [
        ("600165", "SH", "active", False), ("601456", "SH", "paused", False),
        ("600000", "SH", "archived", True), ("000001", "SZ", "archived", False),
    ]:
        s = Symbol(symbol=code, market=market, asset_type="stock", name=code)
        db_session.add(s)
        db_session.flush()
        db_session.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status=status))
        if held:
            db_session.add(Position(portfolio_id=p.id, symbol_id=s.id, quantity=100, asset_type="stock"))
    db_session.commit()
    return p


def test_default_scope_includes_current_members_and_remaining_holdings(download_client, portfolio):
    response = download_client.get("/api/v1/security-status/download-scope", params={"portfolio_id": portfolio.id})
    assert response.status_code == 200, response.text
    scope = response.json()
    assert [s["symbol"] for s in scope["symbols"]] == ["600000", "600165", "601456"]
    assert scope["can_download"] is True
    assert date.fromisoformat(scope["end_date"]) < datetime.now(timezone.utc).date()
    assert 364 <= (date.fromisoformat(scope["end_date"]) - date.fromisoformat(scope["start_date"])).days <= 365
    assert scope["latest_task"] is None


def fake_public_source(request, directory, progress):
    """The external source returns literal, explicit daily flags, including a halt."""
    queries = []
    def receipt(key, endpoint, parameters, fields, rows):
        value = {"endpoint": endpoint, "parameters": parameters, "fetched_at": "2026-10-20T08:00:00+00:00",
                 "raw": {"fields": fields, "rows": rows, "error_code": "0", "error_msg": "success"}}
        data = json.dumps(value, ensure_ascii=False).encode()
        (directory / (key + ".json")).write_bytes(data)
        queries.append({"key": key, "file": key + ".json", "sha256": hashlib.sha256(data).hexdigest()})
        (directory / "capture.json").write_text(json.dumps({"queries": queries, "complete": False}), encoding="utf-8")
        progress(len(queries), key)
    receipt("calendar", "query_trade_dates", {"start_date": request["start_date"], "end_date": request["end_date"]},
            ["calendar_date", "is_trading_day"], [["2024-04-25", "1"], ["2024-04-26", "1"],
            ["2024-04-27", "0"], ["2024-04-28", "0"], ["2024-04-29", "1"], ["2024-04-30", "1"]])
    for symbol in request["symbols"]:
        code = symbol["provider_code"]
        receipt("basic_" + symbol["symbol"], "query_stock_basic", {"code": code},
                ["code", "code_name", "ipoDate", "outDate", "type", "status"],
                [[code, "今天的名称", "1998-01-22", "2025-01-01" if symbol["symbol"] == "600000" else "", "1", "0"]])
        rows = [[day, code, "10.0000", "", "" if day == "2024-04-26" else "1000", "3",
                 "0" if symbol["symbol"] == "601456" and day == "2024-04-26" else "1",
                 "1" if symbol["symbol"] == "600165" else "0"]
                for day in ["2024-04-25", "2024-04-26", "2024-04-29", "2024-04-30"]]
        receipt("daily_" + symbol["symbol"], "query_history_k_data_plus",
                {"code": code, "start_date": request["start_date"], "end_date": request["end_date"], "adjustflag": "3", "frequency": "d"},
                ["date", "code", "close", "amount", "volume", "adjustflag", "tradestatus", "isST"], rows)
    (directory / "capture.json").write_text(json.dumps({"queries": queries, "complete": True}), encoding="utf-8")


@pytest.fixture
def public_source(monkeypatch, tmp_path):
    from app.services.security_status import baostock_source, history_download
    monkeypatch.setattr(history_download, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(baostock_source, "capture_history", fake_public_source)


def start_download(client, pid, **overrides):
    scope = client.get("/api/v1/security-status/download-scope", params={"portfolio_id": pid}).json()
    body = {"portfolio_id": pid, "request_id": str(uuid4()), "start_date": "2024-04-25", "end_date": "2024-04-30",
            "expected_scope_sha256": scope["scope_sha256"], **overrides}
    response = client.post("/api/v1/security-status/downloads", json=body)
    return response, body


def wait_download(client, task_id):
    from app.services.async_tasks import is_worker_running
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/security-status/downloads/{task_id}")
        assert response.status_code == 200, response.text
        value = response.json()
        if value["task"]["status"] not in {"queued", "running"} and not is_worker_running(task_id):
            return value
        time.sleep(.02)
    pytest.fail("download did not finish")


def test_download_retains_raw_halt_and_st_but_does_not_certify_incomplete_history(download_client, portfolio, public_source):
    response, _ = start_download(download_client, portfolio.id)
    assert response.status_code == 202, response.text
    result = wait_download(download_client, response.json()["task"]["id"])
    assert result["task"]["status"] == "done", result
    report = result["report"]
    assert report["can_import"] is False
    assert report["backtest_ready"] is False
    assert report["source_updated_at"] is None
    assert report["missing_fields"] == ["is_delisting_period", "daily_price_limits", "source_updated_at"]
    assert report["valid_daily_rows"] == 12
    st = next(s for s in report["symbols"] if s["symbol"] == "600165")
    halted = next(s for s in report["symbols"] if s["symbol"] == "601456")
    assert st["st_days"] == 4
    assert halted["suspended_days"] == 1
    halt_day = next(d for d in halted["days"] if d["trade_date"] == "2024-04-26")
    assert halt_day["is_suspended"] is True
    assert halt_day["is_delisting_period"] is None
    assert halt_day["upper_limit"] is None
    # Future outDate and today's status/name must not be projected backward.
    first = next(s for s in report["symbols"] if s["symbol"] == "600000")["days"][0]
    assert first["is_listed"] is True
    assert first["delisting_date"] is None
    assert "name" not in first
    assert download_client.get("/api/v1/security-status/coverage").json()["verified_records"] == 0
    bundle = download_client.get(f'/api/v1/security-status/downloads/{result["task"]["id"]}/files')
    assert bundle.status_code == 200, bundle.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        raw = json.loads(archive.read("daily_601456.json"))["raw"]
        assert raw["rows"][1][raw["fields"].index("volume")] == ""
        assert raw["rows"][1][raw["fields"].index("tradestatus")] == "0"


@pytest.fixture
def sdk_at_external_boundary(tmp_path, monkeypatch):
    """A stand-in remote SDK, exercised by the real bounded child process."""
    module = tmp_path / "sdk"
    module.mkdir()
    (module / "baostock.py").write_text('''
class Result:
    error_code = "0"
    error_msg = "success"
    def __init__(self, fields=(), rows=()):
        self.fields, self.rows, self.i = list(fields), list(rows), -1
    def next(self):
        self.i += 1
        return self.i < len(self.rows)
    def get_row_data(self):
        return self.rows[self.i]
def login():
    print("login success!")
    return Result()
def logout():
    return Result()
def query_trade_dates(start_date, end_date):
    return Result(["calendar_date", "is_trading_day"], [
        ["2024-04-25", "1"], ["2024-04-26", "1"], ["2024-04-27", "0"],
        ["2024-04-28", "0"], ["2024-04-29", "1"], ["2024-04-30", "1"]])
def query_stock_basic(code):
    return Result(["code", "ipoDate", "outDate"], [[code, "1998-01-22", ""]])
def query_history_k_data_plus(code, fields, start_date, end_date, frequency, adjustflag):
    assert frequency == "d" and adjustflag == "3"
    columns = fields.split(",")
    rows = []
    for day in ["2024-04-25", "2024-04-26", "2024-04-29", "2024-04-30"]:
        values = {"date": day, "code": code, "tradestatus": "0" if day == "2024-04-26" else "1",
                  "isST": "1", "adjustflag": "3", "volume": "", "amount": ""}
        rows.append([values.get(c, "10.0000") for c in columns])
    return Result(columns, rows)
''', encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(module))
    return module


def test_real_download_process_saves_sdk_responses_before_reading_flags(download_client, portfolio, sdk_at_external_boundary, tmp_path, monkeypatch):
    from app.services.security_status import history_download
    monkeypatch.setattr(history_download, "ARTIFACT_ROOT", tmp_path / "receipts")
    response, _ = start_download(download_client, portfolio.id)
    assert response.status_code == 202, response.text
    result = wait_download(download_client, response.json()["task"]["id"])
    assert result["task"]["status"] == "done", result
    assert result["report"]["valid_daily_rows"] == 12
    assert len(result["report"]["raw_files"]) == 7
    assert result["report"]["symbols"][0]["suspended_days"] == 1


def test_replaying_a_lost_request_keeps_its_frozen_scope_and_finished_report(download_client, portfolio, public_source, db_session):
    from sqlalchemy import select
    response, body = start_download(download_client, portfolio.id)
    task_id = response.json()["task"]["id"]
    wait_download(download_client, task_id)
    member = db_session.scalar(select(PortfolioMember).where(PortfolioMember.portfolio_id == portfolio.id,
        PortfolioMember.status == "active"))
    member.status = "archived"
    db_session.commit()
    replay = download_client.post("/api/v1/security-status/downloads", json=body)
    assert replay.status_code == 202, replay.text
    assert replay.json()["task"]["id"] == task_id
    assert replay.json()["report"]["valid_daily_rows"] == 12
    assert len(replay.json()["report"]["symbols"]) == 3
    found = download_client.get(f'/api/v1/security-status/downloads/requests/{body["request_id"]}',
                                params={"portfolio_id": portfolio.id}).json()
    assert found["task"]["id"] == task_id
    changed = download_client.post("/api/v1/security-status/downloads", json={**body, "end_date": "2024-04-29"})
    assert changed.status_code == 409
    stale = download_client.post("/api/v1/security-status/downloads", json={**body, "request_id": str(uuid4())})
    assert stale.status_code == 409


def test_malformed_source_response_is_reported_with_other_raw_receipts_retained(download_client, portfolio, public_source, monkeypatch):
    from app.services.security_status import baostock_source
    def malformed(request, directory, progress):
        fake_public_source(request, directory, progress)
        path = directory / "daily_600165.json"
        raw = json.loads(path.read_bytes())
        raw["raw"] = None
        data = json.dumps(raw).encode()
        path.write_bytes(data)
        capture = json.loads((directory / "capture.json").read_bytes())
        next(q for q in capture["queries"] if q["key"] == "daily_600165")["sha256"] = hashlib.sha256(data).hexdigest()
        (directory / "capture.json").write_text(json.dumps(capture), encoding="utf-8")
    monkeypatch.setattr(baostock_source, "capture_history", malformed)
    response, _ = start_download(download_client, portfolio.id)
    result = wait_download(download_client, response.json()["task"]["id"])
    assert result["task"]["status"] == "partial", result
    assert result["report"]["valid_daily_rows"] == 8
    assert len(result["report"]["raw_files"]) == 7
    assert next(s for s in result["report"]["symbols"] if s["symbol"] == "600165")["errors"]


@pytest.mark.parametrize("change,expected_rows", [
    ("all_empty", 0), ("provider_error", 8), ("unknown_st", 11), ("unknown_suspension", 11),
    ("missing_day", 11), ("wrong_code", 11), ("adjusted", 11), ("missing_calendar_day", 12),
])
def test_bad_or_missing_daily_inputs_remain_visible_gaps(download_client, portfolio, public_source, monkeypatch, change, expected_rows):
    from app.services.security_status import baostock_source
    def altered_source(request, directory, progress):
        fake_public_source(request, directory, progress)
        capture = json.loads((directory / "capture.json").read_bytes())
        for receipt in capture["queries"]:
            if (change == "missing_calendar_day" and receipt["key"] == "calendar"
                or change == "all_empty" and receipt["key"].startswith("daily_")
                or change not in {"missing_calendar_day", "all_empty"} and receipt["key"] == "daily_600165"):
                path = directory / receipt["file"]
                value = json.loads(path.read_bytes())
                raw = value["raw"]
                if change == "all_empty": raw["rows"] = []
                elif change in {"missing_day", "missing_calendar_day"}: raw["rows"].pop(1)
                elif change == "provider_error": raw["error_code"] = "remote_failure"
                else:
                    field, value_text = {"unknown_st": ("isST", ""), "unknown_suspension": ("tradestatus", ""),
                                         "wrong_code": ("code", "sz.000001"), "adjusted": ("adjustflag", "2")}[change]
                    raw["rows"][1][raw["fields"].index(field)] = value_text
                data = json.dumps(value).encode()
                path.write_bytes(data)
                receipt["sha256"] = hashlib.sha256(data).hexdigest()
        (directory / "capture.json").write_text(json.dumps(capture), encoding="utf-8")
    monkeypatch.setattr(baostock_source, "capture_history", altered_source)
    response, _ = start_download(download_client, portfolio.id)
    result = wait_download(download_client, response.json()["task"]["id"])
    assert result["task"]["status"] == ("failed" if change == "all_empty" else "partial"), result
    assert result["report"]["valid_daily_rows"] == expected_rows
    assert result["report"]["backtest_ready"] is False
    assert result["report"]["errors"] or any(s["errors"] for s in result["report"]["symbols"])
    if change == "missing_day":
        assert next(s for s in result["report"]["symbols"] if s["symbol"] == "600165")["missing_trade_dates"] == ["2024-04-26"]
    if change == "missing_calendar_day":
        assert result["report"]["calendar_complete"] is False
        assert all(s["missing_trade_dates"] is None for s in result["report"]["symbols"])
    assert download_client.get("/api/v1/security-status/coverage").json()["verified_records"] == 0


def test_connection_failure_is_not_reported_as_empty_history(download_client, portfolio, public_source, monkeypatch):
    from app.services.security_status import baostock_source
    def unavailable(*_args):
        raise baostock_source.SourceDownloadError("历史资料来源连接失败，请稍后重试。")
    monkeypatch.setattr(baostock_source, "capture_history", unavailable)
    response, _ = start_download(download_client, portfolio.id)
    result = wait_download(download_client, response.json()["task"]["id"])
    assert result["task"]["status"] == "failed"
    assert result["report"]["source_error"] == "历史资料来源连接失败，请稍后重试。"
    assert result["report"]["calendar_complete"] is False


def test_cancelling_a_blocked_remote_query_stops_the_child_and_retains_received_calendar(download_client, portfolio, sdk_at_external_boundary, tmp_path, monkeypatch):
    from app.services.security_status import history_download
    from app.services.async_tasks import cancel_async_task
    monkeypatch.setattr(history_download, "ARTIFACT_ROOT", tmp_path / "receipts")
    with (sdk_at_external_boundary / "baostock.py").open("a", encoding="utf-8") as source:
        source.write('\ndef query_stock_basic(code):\n    import time\n    time.sleep(30)\n')
    response, _ = start_download(download_client, portfolio.id)
    task_id = response.json()["task"]["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = download_client.get(f"/api/v1/security-status/downloads/{task_id}").json()
        if value["task"]["processed"] >= 1:
            break
        time.sleep(.02)
    else:
        pytest.fail("source did not deliver its calendar")
    started = time.monotonic()
    cancel_async_task(task_id)
    result = wait_download(download_client, task_id)
    assert time.monotonic() - started < 3
    assert result["task"]["status"] == "cancelled"
    assert [r["file"] for r in result["report"]["raw_files"]] == ["calendar.json"]
    bundle = download_client.get(f"/api/v1/security-status/downloads/{task_id}/files")
    assert bundle.status_code == 200


def test_remote_sdk_cannot_hold_the_process_past_the_download_deadline(sdk_at_external_boundary, tmp_path):
    from app.services.security_status.baostock_source import capture_history, SourceDownloadError
    with (sdk_at_external_boundary / "baostock.py").open("a", encoding="utf-8") as source:
        source.write('\ndef login():\n    import time\n    time.sleep(30)\n')
    started = time.monotonic()
    with pytest.raises(SourceDownloadError, match="等待"):
        capture_history({"symbols": [], "start_date": "2024-04-25", "end_date": "2024-04-30"},
                        tmp_path / "timeout", lambda *_: None, timeout_seconds=.6)
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("dates", [
    {"start_date": "2024-04-30", "end_date": "2024-04-25"},
    {"start_date": "2024-01-01", "end_date": "2025-02-01"},
    {"start_date": "2099-01-01", "end_date": "2099-01-02"},
])
def test_out_of_bounds_dates_are_rejected_before_creating_a_task(download_client, portfolio, dates):
    response, _ = start_download(download_client, portfolio.id, **dates)
    assert response.status_code == 409
    assert download_client.get("/api/v1/security-status/download-scope", params={"portfolio_id": portfolio.id}).json()["latest_task"] is None


def test_unsupported_market_is_explicit_and_does_not_silently_shrink_the_portfolio(download_client, portfolio, db_session):
    symbol = Symbol(symbol="920001", name="北交所成员", market="BJ", asset_type="stock")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id, status="active"))
    db_session.commit()
    scope = download_client.get("/api/v1/security-status/download-scope", params={"portfolio_id": portfolio.id}).json()
    assert scope["can_download"] is False
    assert len(scope["symbols"]) == 4
    assert scope["reasons"]
    response, _ = start_download(download_client, portfolio.id)
    assert response.status_code == 409


def test_changed_raw_files_are_not_silently_downloaded_or_displayed_as_verified(download_client, portfolio, public_source, tmp_path):
    response, _ = start_download(download_client, portfolio.id)
    task_id = response.json()["task"]["id"]
    wait_download(download_client, task_id)
    (tmp_path / task_id / "daily_600165.json").write_text("changed", encoding="utf-8")
    assert download_client.get(f"/api/v1/security-status/downloads/{task_id}").status_code == 409
    assert download_client.get(f"/api/v1/security-status/downloads/{task_id}/files").status_code == 409


def test_startup_keeps_interrupted_download_receipts_without_silently_restarting_them(download_client, portfolio, public_source, db_session, tmp_path):
    from app.models.async_task import AsyncTaskRecord
    from app.services import async_tasks
    from app.services.security_status import history_download
    task_id = uuid4().hex
    scope = download_client.get("/api/v1/security-status/download-scope", params={"portfolio_id": portfolio.id}).json()
    payload = {"portfolio_id": portfolio.id, "portfolio_name": portfolio.name, "symbols": scope["symbols"],
               "start_date": "2024-04-25", "end_date": "2024-04-30"}
    directory = tmp_path / task_id
    directory.mkdir()
    fake_public_source(payload, directory, lambda *_: None)
    (directory / "request.json").write_text(json.dumps(payload), encoding="utf-8")
    db_session.add(AsyncTaskRecord(id=task_id, task_type="security_status_download", status="running", stage="download",
                                 payload_json=json.dumps(payload)))
    db_session.commit()
    async_tasks.interrupt_orphaned_async_tasks(db_session)
    history_download.recover_interrupted_downloads(db_session)
    result = download_client.get(f"/api/v1/security-status/downloads/{task_id}").json()
    assert result["task"]["status"] == "failed"
    assert result["task"]["message"] == "应用上次关闭时下载尚未完成，已取得的原始文件仍保留，请重新下载。"
    assert result["report"]["valid_daily_rows"] == 12
    assert result["report"]["source_error"]
    assert download_client.get(f"/api/v1/security-status/downloads/{task_id}/files").status_code == 200


def test_missing_member_directory_record_cannot_silently_shrink_scope(download_client, portfolio, db_session):
    db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=999999, status="active"))
    db_session.commit()
    scope = download_client.get("/api/v1/security-status/download-scope", params={"portfolio_id": portfolio.id}).json()
    assert scope["can_download"] is False
    assert any("找不到" in reason for reason in scope["reasons"])


@pytest.mark.parametrize("source_st_flag", ["0", "1"])
def test_etf_source_flags_are_retained_but_not_interpreted_as_verified_stock_status(download_client, portfolio, public_source, db_session, monkeypatch, source_st_flag):
    from app.services.security_status import baostock_source
    symbol = Symbol(symbol="510300", name="沪深300ETF", market="SH", asset_type="etf")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id, status="active"))
    db_session.commit()
    def etf_source(request, directory, progress):
        fake_public_source(request, directory, progress)
        path = directory / "daily_510300.json"
        value = json.loads(path.read_bytes())
        for row in value["raw"]["rows"]:
            row[value["raw"]["fields"].index("isST")] = source_st_flag
        data = json.dumps(value).encode()
        path.write_bytes(data)
        capture = json.loads((directory / "capture.json").read_bytes())
        next(q for q in capture["queries"] if q["key"] == "daily_510300")["sha256"] = hashlib.sha256(data).hexdigest()
        (directory / "capture.json").write_text(json.dumps(capture), encoding="utf-8")
    monkeypatch.setattr(baostock_source, "capture_history", etf_source)
    response, _ = start_download(download_client, portfolio.id)
    result = wait_download(download_client, response.json()["task"]["id"])
    assert result["task"]["status"] == "partial"
    assert result["report"]["valid_daily_rows"] == 12
    etf = next(s for s in result["report"]["symbols"] if s["symbol"] == "510300")
    assert etf["raw_daily_rows"] == 4
    assert etf["valid_daily_rows"] == 0
    assert etf["st_days"] is None
    assert etf["suspended_days"] is None
    assert etf["missing_trade_dates"] is None
    assert etf["days"] == []
    assert any("ETF" in issue for issue in etf["errors"])
    bundle = download_client.get(f'/api/v1/security-status/downloads/{result["task"]["id"]}/files')
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        raw = json.loads(archive.read("daily_510300.json"))["raw"]
        assert all(row[raw["fields"].index("isST")] == source_st_flag for row in raw["rows"])


def test_duplicate_inflight_request_reuses_the_job_and_another_job_waits(download_client, portfolio, public_source, monkeypatch):
    from threading import Event
    from app.services.security_status import baostock_source
    entered, release = Event(), Event()
    calls = []
    def waiting_source(request, directory, progress):
        calls.append(request)
        entered.set()
        assert release.wait(5)
        fake_public_source(request, directory, progress)
    monkeypatch.setattr(baostock_source, "capture_history", waiting_source)
    response, body = start_download(download_client, portfolio.id)
    task_id = response.json()["task"]["id"]
    try:
        assert entered.wait(3)
        replay = download_client.post("/api/v1/security-status/downloads", json=body)
        assert replay.status_code == 202
        assert replay.json()["task"]["id"] == task_id
        another = download_client.post("/api/v1/security-status/downloads", json={**body, "request_id": str(uuid4())})
        assert another.status_code == 409
        assert len(calls) == 1
    finally:
        release.set()
        wait_download(download_client, task_id)


def test_symbol_budget_is_checked_before_creating_a_job(download_client, portfolio, db_session):
    for code in range(600200, 600218):
        symbol = Symbol(symbol=str(code), name=str(code), market="SH", asset_type="stock")
        db_session.add(symbol)
        db_session.flush()
        db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id, status="active"))
    db_session.commit()
    scope = download_client.get("/api/v1/security-status/download-scope", params={"portfolio_id": portfolio.id}).json()
    assert len(scope["symbols"]) == 21
    assert scope["can_download"] is False
    response, _ = start_download(download_client, portfolio.id)
    assert response.status_code == 409
    assert scope["latest_task"] is None


def test_base64_download_uses_the_same_checked_bundle(download_client, portfolio, public_source):
    import base64
    response, _ = start_download(download_client, portfolio.id)
    task_id = response.json()["task"]["id"]
    wait_download(download_client, task_id)
    files = download_client.get(f"/api/v1/security-status/downloads/{task_id}/files?encoding=base64")
    assert files.status_code == 200
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(files.json()["content_base64"]))) as archive:
        assert "daily_601456.json" in archive.namelist()
        assert json.loads(archive.read("report.json"))["backtest_ready"] is False


def test_old_report_rules_require_a_new_download_instead_of_reusing_previous_status_claims(download_client, portfolio, public_source, db_session, tmp_path):
    from app.models.async_task import AsyncTaskRecord
    response, _ = start_download(download_client, portfolio.id)
    task_id = response.json()["task"]["id"]
    wait_download(download_client, task_id)
    # A previously persisted report with an older interpretation of ETF flags.
    path = tmp_path / task_id / "report.json"
    report = json.loads(path.read_bytes())
    report.pop("report_version")
    data = json.dumps(report).encode()
    path.write_bytes(data)
    row = db_session.get(AsyncTaskRecord, task_id, populate_existing=True)
    value = json.loads(row.result_json)
    value["report_sha256"] = hashlib.sha256(data).hexdigest()
    row.result_json = json.dumps(value)
    db_session.commit()
    response = download_client.get(f"/api/v1/security-status/downloads/{task_id}")
    assert response.status_code == 409
    assert "检查规则已更新" in response.json()["detail"]
