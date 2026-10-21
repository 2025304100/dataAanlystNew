"""Observable preparation lifecycles through the existing public task APIs."""
from __future__ import annotations

import time
import json
import urllib.request
from datetime import date, timedelta
from threading import Event

import pytest
import requests

from app.api.routes.discovery import get_discovery_snapshot_status
from app.core.config import settings
from app.db.session import get_session_local
from app.services.async_tasks import (
    cancel_async_task, create_async_task, get_async_task,
    request_all_workers_stop, wait_workers_stopped,
)
from app.services.discovery_data_prep import start_data_prep_task


pytestmark = [pytest.mark.whitebox, pytest.mark.reap_workers]


def _wait_task(task_id, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = get_async_task(task_id)
        if task and condition(task):
            return task
        time.sleep(0.02)
    raise AssertionError(f"Task {task_id} did not reach its expected public state")


@pytest.fixture
def offline_preparation(db_session, tmp_path, monkeypatch):
    # Only the network and local runtime paths are substituted. The parent,
    # child dispatchers, real threads, database and scan implementation run.
    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline fixture: upstream unavailable")
    monkeypatch.setattr(requests.Session, "request", offline)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", offline)
    monkeypatch.setattr(settings, "factor_warehouse_path", tmp_path / "factors.duckdb")
    from app.models.akshare_api_config import AkshareApiConfig
    from app.services.macro import SPECS
    from app.services.akshare_registry import load_config_cache, snapshot_config_cache, restore_config_cache
    policy_before = snapshot_config_cache()
    for key in {spec.fetcher for spec in SPECS if not spec.fetcher.startswith("fred:")}:
        db_session.add(AkshareApiConfig(api_key=key, enabled=False))
    db_session.commit()
    load_config_cache(db_session)
    try:
        yield
    finally:
        request_all_workers_stop()
        wait_workers_stopped(5)
        restore_config_cache(policy_before)


@pytest.mark.parametrize("scope, expected_codes", [
    ("cn-stock", {"600000", "000001", "920001"}),
    ("cn-etf", {"510300"}),
])
def test_preparation_dispatches_only_active_symbols_in_its_region_and_asset(
    offline_preparation, db_session, scope, expected_codes,
):
    from app.models.symbol import Symbol

    symbols = [
        Symbol(symbol="600000", name="浦发银行", market="sh", asset_type="stock", is_active=1),
        Symbol(symbol="000001", name="平安银行", market="sz", asset_type="stock", is_active=1),
        Symbol(symbol="920001", name="北交股票", market="bj", asset_type="stock", is_active=1),
        Symbol(symbol="AAPL", name="Apple", market="us", asset_type="stock", is_active=1),
        Symbol(symbol="510300", name="沪深三百基金", market="sh", asset_type="etf", is_active=1),
        Symbol(symbol="SPY", name="SPY", market="us", asset_type="etf", is_active=1),
        Symbol(symbol="510500", name="停用基金", market="sh", asset_type="etf", is_active=0),
        Symbol(symbol="600001", name="停用股票", market="sh", asset_type="stock", is_active=0),
    ]
    db_session.add_all(symbols)
    db_session.commit()
    expected_ids = {symbol.id for symbol in symbols if symbol.symbol in expected_codes}

    parent = start_data_prep_task(scope=scope)
    waiting = _wait_task(parent.id, lambda task:
                         (task.result or {}).get("child_task_ids", {}).get("market_data"))
    child = get_async_task(waiting.result["child_task_ids"]["market_data"])
    _wait_task(child.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=30)
    payload = json.loads(child.payload_json)
    assert payload["scope"] == "symbols"
    assert set(payload["symbol_ids"]) == expected_ids
    assert payload["asset_types"] == ["stock" if scope == "cn-stock" else "etf"]
    assert payload["auto_scan"] is False


@pytest.fixture
def queued_market_child(offline_preparation, db_session):
    from app.models.symbol import Symbol

    symbol = Symbol(symbol="600000", name="浦发银行", market="sh", asset_type="stock", is_active=1)
    db_session.add(symbol)
    db_session.commit()
    return create_async_task("market_data_sync", {
        "scope": "symbols", "symbol_ids": [symbol.id], "asset_types": ["stock"], "auto_scan": False,
    })


def test_preparation_rejects_another_markets_active_child_without_cancelling_it(offline_preparation, db_session):
    from app.models.symbol import Symbol

    cn = Symbol(symbol="510300", name="沪深三百基金", market="sh", asset_type="etf", is_active=1)
    us = Symbol(symbol="SPY", name="SPY", market="us", asset_type="etf", is_active=1)
    db_session.add_all([cn, us])
    db_session.commit()
    other = create_async_task("market_data_sync", {
        "scope": "symbols", "symbol_ids": [us.id], "asset_types": ["etf"], "auto_scan": False,
    })
    try:
        parent = start_data_prep_task(scope="cn-etf")
        observed = _wait_task(parent.id, lambda task: task.status == "failed"
            or (task.result or {}).get("child_task_ids", {}).get("market_data"))
        assert observed.status == "failed"
        assert "其他范围" in str(observed.errors)
        assert get_async_task(other.id).status == "queued"
    finally:
        cancel_async_task(other.id)


def test_preparation_waits_for_existing_market_task_and_reports_its_cancellation(queued_market_child):
    child = queued_market_child
    parent = start_data_prep_task(scope="cn-stock", trigger_fast_scan_after_ready=True)
    try:
        waiting = _wait_task(parent.id, lambda task:
                   (task.result or {}).get("child_task_ids", {}).get("market_data") == child.id
                   or task.status in {"done", "partial", "failed", "cancelled"})
        assert waiting.status == "running"
        cancel_async_task(child.id)

        completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"})
        assert completed.status == "failed"
        with get_session_local()() as reader:
            snapshot = get_discovery_snapshot_status(scope="cn-stock", db=reader)
        assert snapshot["has_ready_snapshot"] is False
    finally:
        cancel_async_task(child.id)


def test_cancelled_preparation_does_not_start_a_snapshot_or_scan(queued_market_child):
    child = queued_market_child
    parent = start_data_prep_task(scope="cn-stock", trigger_fast_scan_after_ready=True)
    try:
        _wait_task(parent.id, lambda task: (task.result or {}).get("child_task_ids", {}).get("market_data") == child.id)
        duplicate = start_data_prep_task(scope="cn_stock", trigger_fast_scan_after_ready=True)
        assert duplicate.id == parent.id
        cancel_async_task(parent.id)
        cancelled = _wait_task(parent.id, lambda task: task.status == "cancelled")
        assert (cancelled.result or {}).get("snapshot_id") is None
        with get_session_local()() as reader:
            assert get_discovery_snapshot_status(scope="cn-stock", db=reader)["has_ready_snapshot"] is False
    finally:
        cancel_async_task(child.id)


@pytest.fixture
def market_preparation(offline_preparation, db_session, monkeypatch, request):
    from app.models.symbol import Symbol
    from app.models.universe import UniverseSymbol
    from app.models.score import Score
    from app.models.market_data import MarketCalendar

    history_end = getattr(request, "param", {}).get("history_end", date.today())
    while history_end.weekday() >= 5:
        history_end -= timedelta(days=1)
    symbol = Symbol(symbol="510300", name="Local ETF", asset_type="etf", market="sh", is_active=1)
    db_session.add(symbol)
    db_session.flush()
    db_session.add(UniverseSymbol(symbol="510300", name="Local ETF", asset_type="etf",
                                  region="cn", market="sh", is_synced=1, bar_count=90))
    db_session.add(Score(symbol_id=symbol.id, trade_date=history_end, quality_score=80,
                         timing_score=80, priority_score=80, quality_grade="A",
                         stage="start", action="open", weight_mode="manual"))
    db_session.add(MarketCalendar(trade_date=date.today(), is_trading_day=0))
    db_session.commit()

    # Exercise the actual AKShare adapter, sync worker and scoring on bounded
    # supplier bytes; all other external requests fail visibly as optional input warnings.
    history_requested, release_history = Event(), Event()
    def supplier_response(self, method, url, **kwargs):
        if "push2his.eastmoney.com/api/qt/stock/kline/get" not in str(url):
            raise requests.ConnectionError("offline fixture: optional upstream unavailable")
        history_requested.set()
        if not release_history.wait(5):
            raise requests.Timeout("History fixture was not released")
        klines = []
        for offset in range(119, -1, -1):
            day = history_end - timedelta(days=offset)
            if day.weekday() < 5:
                klines.append(f"{day.isoformat()},10,10,10.2,9.8,10000,10000000,4,0,0,1")
        response = requests.Response()
        response.status_code = 200
        response.url = str(url)
        response._content = json.dumps({"data": {"code": "510300", "klines": klines}}).encode()
        return response
    monkeypatch.setattr(requests.Session, "request", supplier_response)
    yield history_requested, release_history
    release_history.set()


def test_preparation_finishes_only_after_ready_snapshot_and_real_scan(market_preparation):
    history_requested, release_history = market_preparation
    parent = start_data_prep_task(scope="cn-etf")
    try:
        assert history_requested.wait(5)
        upgraded = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                       fast_scan_params={"min_score": 0})
        assert upgraded.id == parent.id
    finally:
        release_history.set()
    completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)

    assert completed.status in {"done", "partial"}, completed.errors
    assert completed.result["fast_scan"]["status"] == "ok"
    assert completed.result["fast_scan"]["scan_run_id"] is not None
    with get_session_local()() as reader:
        snapshot = get_discovery_snapshot_status(scope="cn-etf", db=reader)
    assert snapshot["ready_snapshot_id"] == completed.result["snapshot_id"]
    assert snapshot["ready_snapshot_symbol_count"] == 1
    seen_task_ids = {parent.id}
    seen_snapshot_ids = {completed.result["snapshot_id"]}
    for _ in range(2):
        repeated = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                        fast_scan_params={"min_score": 0})
        assert repeated.id not in seen_task_ids
        finished = _wait_task(repeated.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
        assert finished.status in {"done", "partial"}, finished.errors
        assert finished.result["fast_scan"]["status"] == "ok"
        assert finished.result["snapshot_id"] not in seen_snapshot_ids
        seen_task_ids.add(repeated.id)
        seen_snapshot_ids.add(finished.result["snapshot_id"])
    from app.api.routes.discovery import list_scan_runs_route
    with get_session_local()() as reader:
        history = list_scan_runs_route(scope="cn-etf", trade_date=None, status=None, limit=20, offset=0, db=reader)
    assert len(history) == 3
    assert {item["snapshot_id"] for item in history} == seen_snapshot_ids
    assert {item["scope"] for item in history} == {"cn-etf"}

    invalid = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                   fast_scan_params={"unsupported_parameter": True})
    failed = _wait_task(invalid.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
    assert failed.status == "failed"
    assert failed.result["fast_scan"]["status"] == "failed"
    retry = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                 fast_scan_params={"min_score": 0})
    recovered = _wait_task(retry.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
    assert recovered.status in {"done", "partial"}
    assert recovered.result["fast_scan"]["status"] == "ok"


def test_preparation_releases_parent_connection_while_a_real_child_is_pending(market_preparation):
    from sqlalchemy import event, inspect
    from app.models.async_task import AsyncTaskRecord

    history_requested, release_history = market_preparation
    parent_connections = []
    session_class = get_session_local().class_

    # Simulate the server expiring only a connection still held across the
    # supplier wait. The real dispatcher, child, polling and publication run.
    def identify_parent(session, flush_context):
        if any(isinstance(row, AsyncTaskRecord) and row.task_type == "discovery_data_prep"
               and '"market_data"' in (row.result_json or "")
               and inspect(row).attrs.result_json.history.has_changes() for row in session.dirty):
            session.info["preparation_parent"] = True

    def observe_parent_connection(session, transaction, connection):
        if session.info.get("preparation_parent"):
            parent_connections.append(connection)

    event.listen(session_class, "after_flush", identify_parent)
    event.listen(session_class, "after_begin", observe_parent_connection)
    try:
        parent = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                      fast_scan_params={"min_score": 0})
        assert history_requested.wait(5)
        waiting = _wait_task(parent.id, lambda task: "等待 market_data" in (task.current_step_description or ""))
        assert waiting.status == "running"
        with get_session_local()() as reader:
            assert get_discovery_snapshot_status(scope="cn-etf", db=reader)["has_ready_snapshot"] is False
        assert parent_connections
        held = parent_connections[-1]
        if not held.closed:
            held.invalidate()
        release_history.set()
        completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
        assert completed.status in {"done", "partial"}, completed.errors
        assert completed.result["fast_scan"]["status"] == "ok"
    finally:
        release_history.set()
        event.remove(session_class, "after_flush", identify_parent)
        event.remove(session_class, "after_begin", observe_parent_connection)


@pytest.mark.parametrize("market_preparation", [{"history_end": date.today() - timedelta(days=30)}], indirect=True)
def test_preparation_publishes_the_actual_score_date_instead_of_the_build_day(market_preparation):
    _, release_history = market_preparation
    release_history.set()
    parent = start_data_prep_task(scope="cn-etf")
    completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
    assert completed.status in {"done", "partial"}, completed.errors
    child = get_async_task(completed.result["child_task_ids"]["market_data"])
    assert child.result["scored_count"] == 1
    with get_session_local()() as reader:
        status = get_discovery_snapshot_status(scope="cn-etf", db=reader)
    expected = date.today() - timedelta(days=30)
    while expected.weekday() >= 5:
        expected -= timedelta(days=1)
    assert status["ready_snapshot_trade_date"] == expected
    assert status["ready_snapshot_data_cutoff_at"].date() == expected
    assert completed.result["score_date_min"] == expected.isoformat()
    assert completed.result["score_date_max"] == expected.isoformat()


def test_optional_child_registration_disconnect_rolls_back_before_recording_warnings(market_preparation, db_session):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    _, release_history = market_preparation
    release_history.set()
    interrupted = Event()
    engine = db_session.get_bind()

    def disconnect_hot_rank_registration(conn, cursor, statement, parameters, context, executemany):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if (not interrupted.is_set() and statement.lstrip().lower().startswith("update async_tasks")
                and '"external_data.hot_rank"' in " ".join(str(value) for value in values)):
            interrupted.set()
            raise OperationalError(statement, parameters, RuntimeError("Simulated Lost connection 2013"), connection_invalidated=True)

    event.listen(engine, "before_cursor_execute", disconnect_hot_rank_registration)
    try:
        parent = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                      fast_scan_params={"min_score": 0})
        completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
        assert interrupted.is_set()
        assert completed.status == "partial", completed.errors
        assert completed.result["fast_scan"]["status"] == "ok"
        assert "external_data.lhb_institution" in completed.result["child_task_ids"]
        assert "Simulated Lost connection 2013" in str(completed.errors)
        assert "PendingRollback" not in str(completed.errors)
    finally:
        event.remove(engine, "before_cursor_execute", disconnect_hot_rank_registration)


def test_intraday_preparation_never_reports_a_future_data_cutoff(market_preparation, monkeypatch):
    from datetime import datetime

    source_date = date.today()
    while source_date.weekday() >= 5:
        source_date -= timedelta(days=1)
    observed_at = datetime.combine(source_date, datetime.min.time()).replace(hour=3)  # 11:00 Shanghai
    monkeypatch.setattr("app.services.discovery_data_prep._now", lambda: observed_at)
    _, release_history = market_preparation
    release_history.set()
    parent = start_data_prep_task(scope="cn-etf")
    completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
    assert completed.status in {"done", "partial"}, completed.errors
    with get_session_local()() as reader:
        status = get_discovery_snapshot_status(scope="cn-etf", db=reader)
    assert status["ready_snapshot_data_cutoff_at"] <= observed_at
    assert completed.result["source_data_status"] == "intraday"
    assert "盘中" in completed.message


def test_preparation_does_not_publish_old_scores_when_current_bars_cannot_be_scored(
    market_preparation, db_session,
):
    from datetime import datetime
    from sqlalchemy import select
    from app.api.routes.market_data import get_daily_bars
    from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot
    from app.models.score import Score
    from app.models.scoring_config import ScoringConfig
    from app.services.scoring_config_engine import invalidate_active_config_cache

    _, release_history = market_preparation
    release_history.set()
    old_score = db_session.scalar(select(Score))
    symbol_id = old_score.symbol_id
    old_date = date.today() - timedelta(days=30)
    old_score.trade_date = old_date
    previous = DiscoveryScoreSnapshot(scope="cn_etf", status="ready", symbol_count=1,
        trade_date=datetime.combine(old_date, datetime.min.time()), generated_at=datetime.combine(old_date, datetime.min.time()),
        source_task_id="previous-successful-preparation")
    db_session.add(previous)
    db_session.add(ScoringConfig(asset_type="etf", preset_key="broken-current-config", name="损坏的当前评分配置",
        version=1, config_json="{invalid", is_active=1, is_latest=1))
    db_session.commit()
    previous_id = previous.id
    invalidate_active_config_cache()
    try:
        parent = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True)
        completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
        child = get_async_task(completed.result["child_task_ids"]["market_data"])
        assert child.result["symbols_total"] == 1
        assert child.result["scored_count"] == 0
        with get_session_local()() as reader:
            assert get_daily_bars(symbol_id, limit=1, db=reader)[0].trade_date >= date.today() - timedelta(days=3)
            status = get_discovery_snapshot_status(scope="cn-etf", db=reader)
        assert completed.status == "failed"
        assert status["ready_snapshot_id"] == previous_id
    finally:
        invalidate_active_config_cache()


@pytest.mark.parametrize("source", ["missing", "cancelled", "active"])
def test_preparation_recovers_abandoned_building_version_without_deleting_it(market_preparation, db_session, source):
    from datetime import datetime
    from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot

    _, release_history = market_preparation
    release_history.set()
    source_id = "legacy-missing-source"
    if source in {"cancelled", "active"}:
        old_task = create_async_task("legacy_snapshot_builder", {"scope": "cn-etf"})
        if source == "cancelled":
            cancel_async_task(old_task.id)
        source_id = old_task.id
    abandoned = DiscoveryScoreSnapshot(scope="cn_etf", trade_date=datetime.combine(date.today(), datetime.min.time()),
                                        status="building", source_task_id=source_id)
    db_session.add(abandoned)
    db_session.commit()
    abandoned_id = abandoned.id

    parent = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                  fast_scan_params={"min_score": 0})
    completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
    if source == "active":
        assert completed.status == "failed"
        db_session.expire_all()
        assert db_session.get(DiscoveryScoreSnapshot, abandoned_id).status == "building"
        cancel_async_task(old_task.id)
        return
    assert completed.status in {"done", "partial"}, completed.errors
    assert completed.result["fast_scan"]["status"] == "ok"
    db_session.expire_all()
    assert db_session.get(DiscoveryScoreSnapshot, abandoned_id).status == "failed"


def test_cancelling_during_auto_scan_does_not_persist_scan_results(market_preparation, db_session):
    from sqlalchemy import event
    from app.api.routes.discovery import list_scan_runs_route

    _, release_history = market_preparation
    release_history.set()
    parent_ids = []
    cancelled = Event()
    engine = db_session.get_bind()

    def cancel_during_coarse_query(conn, cursor, statement, parameters, context, executemany):
        if (cancelled.is_set() or not parent_ids or "FROM discovery_score_snapshot_items" not in statement
                or "ORDER BY" not in statement):
            return
        task = get_async_task(parent_ids[0])
        if task.stage == "fast_scan":
            cancelled.set()
            cancel_async_task(task.id)

    event.listen(engine, "before_cursor_execute", cancel_during_coarse_query)
    try:
        parent = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                      fast_scan_params={"min_score": 0})
        parent_ids.append(parent.id)
        completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
        assert cancelled.is_set()
        assert completed.status == "cancelled"
        with get_session_local()() as reader:
            assert list_scan_runs_route(scope=None, trade_date=None, status=None, limit=20, offset=0, db=reader) == []
        assert completed.result["fast_scan"]["status"] == "cancelled"
    finally:
        event.remove(engine, "before_cursor_execute", cancel_during_coarse_query)


@pytest.mark.parametrize("interruption", ["cancel", "commit_failure"])
def test_snapshot_publish_interruption_keeps_old_ready_and_allows_retry(market_preparation, interruption):
    from sqlalchemy import event
    from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot

    _, release_history = market_preparation
    release_history.set()
    previous = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                    fast_scan_params={"min_score": 0})
    ready = _wait_task(previous.id, lambda task: task.status in {"done", "partial", "failed"}, timeout=20)
    assert ready.status in {"done", "partial"}, ready.errors
    interrupted = Event()
    session_class = get_session_local().class_

    # Interrupt only the database commit boundary. The actual preparation,
    # snapshot builder, cancellation API and worker failure handler all run.
    def before_commit(session):
        if interrupted.is_set():
            return
        for snapshot in session.identity_map.values():
            if not isinstance(snapshot, DiscoveryScoreSnapshot) or snapshot.source_task_id == previous.id:
                continue
            if snapshot.status == "building" and interruption == "cancel":
                session.info["cancel_snapshot_source"] = snapshot.source_task_id
            if snapshot.status == "ready" and interruption == "commit_failure":
                interrupted.set()
                raise RuntimeError("Isolated fixture: snapshot publish commit failed")

    def after_commit(session):
        source = session.info.pop("cancel_snapshot_source", None)
        if source and not interrupted.is_set():
            interrupted.set()
            cancel_async_task(source)

    event.listen(session_class, "before_commit", before_commit)
    event.listen(session_class, "after_commit", after_commit)
    try:
        parent = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                      fast_scan_params={"min_score": 0})
        completed = _wait_task(parent.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
        assert interrupted.is_set()
        assert completed.status == ("cancelled" if interruption == "cancel" else "failed")
    finally:
        event.remove(session_class, "before_commit", before_commit)
        event.remove(session_class, "after_commit", after_commit)
    with get_session_local()() as reader:
        status = get_discovery_snapshot_status(scope="cn-etf", db=reader)
    assert status["has_building_snapshot"] is False
    assert status["ready_snapshot_id"] == ready.result["snapshot_id"]

    retry = start_data_prep_task(scope="cn-etf", trigger_fast_scan_after_ready=True,
                                 fast_scan_params={"min_score": 0})
    recovered = _wait_task(retry.id, lambda task: task.status in {"done", "partial", "failed", "cancelled"}, timeout=20)
    assert recovered.status in {"done", "partial"}, recovered.errors
    assert recovered.result["fast_scan"]["status"] == "ok"
