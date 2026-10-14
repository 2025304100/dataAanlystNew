"""Isolated audit of Settings > Data Center > Factor Inputs.

Regression contracts use isolated databases and controlled provider responses.
No real provider requests or production database writes are performed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from unittest.mock import MagicMock
from datetime import date, timedelta

import pandas as pd
import pytest

from app.models.async_task import AsyncTaskRecord
from app.models.capital_flow import NorthboundFlow
from app.models.data_sync_plan import DataSyncPartition, DataSyncPlan
from app.models.etf_indicator import EtfIndicator
from app.models.portfolio import Portfolio, Position
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import capital_flow_data, data_quality, etf_basic_data
from app.services import external_data_sync_task as service
from app.services import fundamental_data, lhb_data, tail_proxy_data


pytestmark = pytest.mark.whitebox


class _DeferredThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass


def _symbols(db, count=2, asset_type="stock"):
    symbols = [
        Symbol(symbol=f"{600000 + i}", name=f"audit-{i}", market="sh", asset_type=asset_type)
        for i in range(count)
    ]
    db.add_all(symbols)
    db.commit()
    return symbols


def _task(db, dataset, payload, monkeypatch):
    with monkeypatch.context() as creation_patch:
        creation_patch.setattr(service.threading, "Thread", _DeferredThread)
        task = service.start_external_data_sync(dataset, payload)
    return task, json.loads(task.payload_json)


def _no_final_network(monkeypatch):
    monkeypatch.setattr(service, "_mirror_external_factor_inputs", lambda *_a: {"status": "done", "records": 0})
    monkeypatch.setattr(data_quality, "capture_field_quality_snapshots", lambda *_a, **_k: {})


def test_failed_bulk_partition_is_retryable(db_session, monkeypatch):
    task, payload = _task(db_session, "lhb", {"source": "watchlist"}, monkeypatch)
    _no_final_network(monkeypatch)

    def fail(*_args, **_kwargs):
        raise ConnectionError("audit provider unavailable")

    monkeypatch.setattr(lhb_data, "sync_lhb_institution_trades", fail)
    service._run_external_sync_task(task.id, "lhb", payload)
    details = service.get_external_sync_partitions(task.id)
    assert details["partitions"][0]["status"] == "failed"
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(service.threading, "Thread", _DeferredThread)
        retried = service.retry_external_data_sync(task.id)
    assert retried.status == "queued"


def test_public_all_capital_flow_refuses_per_symbol_fallback(db_session, monkeypatch):
    _symbols(db_session)
    task, payload = _task(db_session, "capital_flow", {"source": "all", "include_northbound": False, "max_workers": 1}, monkeypatch)

    def fail(*_args, **_kwargs):
        raise ConnectionError("audit market snapshot unavailable")

    monkeypatch.setattr(capital_flow_data, "sync_market_capital_flow_snapshot", fail)
    monkeypatch.setattr(capital_flow_data, "sync_symbol_capital_flow", lambda *_a: object())
    with pytest.raises(RuntimeError, match="未执行逐标的兜底"):
        service._run_symbol_sync(db_session, task.id, "capital_flow", payload)


def test_incremental_fallback_stops_on_cancel(db_session, monkeypatch):
    symbols = _symbols(db_session, count=3)
    watchlist = Watchlist(name="audit", list_type="manual")
    db_session.add(watchlist)
    db_session.flush()
    db_session.add_all([WatchlistItem(watchlist_id=watchlist.id, symbol_id=symbol.id) for symbol in symbols])
    db_session.commit()
    task, payload = _task(db_session, "fundamental", {"source": "watchlist", "max_workers": 1}, monkeypatch)
    seen = []

    def fail(*_args, **_kwargs):
        raise ConnectionError("audit snapshot unavailable")

    def single(worker_db, symbol, *_args):
        seen.append(symbol.id)
        if len(seen) == 1:
            row = worker_db.get(AsyncTaskRecord, task.id)
            row.cancel_requested = 1
            worker_db.commit()
        return object()

    monkeypatch.setattr(fundamental_data, "sync_market_valuation_snapshot", fail)
    monkeypatch.setattr(fundamental_data, "sync_symbol_valuation", single)
    service._run_symbol_sync(db_session, task.id, "fundamental", payload)
    assert len(seen) == 1


def test_current_valuation_snapshot_cannot_overwrite_past_day(db_session, monkeypatch):
    symbol = _symbols(db_session, count=1)[0]
    historic = date(2020, 1, 2)
    db_session.add(StockValuation(symbol_id=symbol.id, trade_date=historic, pe_ttm=8.0, source="historic"))
    db_session.commit()
    monkeypatch.setattr(fundamental_data, "call_akshare_with_retry", lambda *_a, **_k: pd.DataFrame([{"代码": symbol.symbol, "市盈率-动态": 99.0, "市净率": 8.0}]))
    with pytest.raises(ValueError, match="历史"):
        fundamental_data.sync_market_valuation_snapshot(db_session, [symbol], historic)
    db_session.commit()
    row = db_session.query(StockValuation).filter_by(symbol_id=symbol.id, trade_date=historic).one()
    assert row.pe_ttm == 8.0


def test_current_etf_snapshot_cannot_be_written_as_history(db_session, monkeypatch):
    symbol = _symbols(db_session, count=1, asset_type="etf")[0]
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_daily", lambda *_a: {"nav": 2.0, "close": 2.2})
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_fund_info", lambda *_a: {"trade_date": date(2026, 10, 13), "nav": 2.0})
    monkeypatch.setattr(etf_basic_data, "_fetch_etf_spot", lambda *_a: {})
    etf_basic_data.sync_etf_indicator(db_session, symbol, date(2020, 1, 2))
    db_session.commit()
    assert db_session.query(EtfIndicator).filter_by(trade_date=date(2020, 1, 2)).count() == 0


def test_northbound_failure_is_observable(db_session, monkeypatch):
    task, payload = _task(db_session, "capital_flow", {"source": "watchlist", "include_northbound": True}, monkeypatch)

    def fail(*_args, **_kwargs):
        raise ConnectionError("audit northbound unavailable")

    monkeypatch.setattr(capital_flow_data.ak, "stock_hsgt_north_net_flow_in", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(capital_flow_data, "call_akshare_with_retry", fail)
    monkeypatch.setattr(capital_flow_data, "sync_market_capital_flow_snapshot", lambda *_a: set())
    result = service._run_symbol_sync(db_session, task.id, "capital_flow", payload)
    assert result["failed"] or any("northbound" in error for error in result["errors"])


def test_northbound_backfill_respects_request_range(db_session, monkeypatch):
    task, payload = _task(db_session, "capital_flow", {"source": "watchlist", "mode": "backfill", "start_date": "2025-01-01", "end_date": "2025-01-03", "include_northbound": True}, monkeypatch)
    monkeypatch.setattr(capital_flow_data.ak, "stock_hsgt_north_net_flow_in", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(capital_flow_data, "call_akshare_with_retry", lambda *_a, **_k: pd.DataFrame([{"日期": "2026-10-13", "当日净流入": 1000.0}]))
    service._run_symbol_sync(db_session, task.id, "capital_flow", payload)
    assert db_session.query(NorthboundFlow).filter(NorthboundFlow.trade_date > date(2025, 1, 3)).count() == 0


def test_tail_watchlist_excludes_archived_entries(db_session):
    symbol = _symbols(db_session, count=1)[0]
    watchlist = Watchlist(name="core", list_type="manual")
    db_session.add(watchlist)
    db_session.flush()
    db_session.add(WatchlistItem(watchlist_id=watchlist.id, symbol_id=symbol.id, status="archived"))
    db_session.commit()
    assert service.resolve_external_symbols(db_session, "watchlist", "stock", watchlist.id) == []
    assert tail_proxy_data.resolve_tail_proxy_symbols(db_session, source="watchlist", limit=20) == []


def test_stale_market_task_does_not_block_creation(db_session, monkeypatch):
    stale = service._now() - timedelta(hours=1)
    db_session.add(AsyncTaskRecord(id="audit-stale", task_type="universe_sync", status="running", stage="sync", updated_at=stale))
    db_session.commit()
    monkeypatch.setattr(service.threading, "Thread", _DeferredThread)
    task = service.start_external_data_sync("hot_rank", {"source": "watchlist"})
    assert task.status == "queued"


def test_northbound_unchecked_makes_no_provider_call(db_session, monkeypatch):
    task, payload = _task(db_session, "capital_flow", {"source": "watchlist", "include_northbound": False}, monkeypatch)
    monkeypatch.setattr(capital_flow_data, "sync_market_capital_flow_snapshot", lambda *_a: set())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unchecked northbound option must not call provider")

    monkeypatch.setattr(capital_flow_data, "sync_northbound_flow", forbidden)
    result = service._run_symbol_sync(db_session, task.id, "capital_flow", payload)
    assert result["total"] == result["records"] == 0


def test_market_priority_preflight_has_bounded_retry_without_waiting(db_session, monkeypatch):
    db_session.add(AsyncTaskRecord(id="audit-priority", task_type="universe_sync", status="running", stage="sync"))
    db_session.commit()
    sleeps = []
    monkeypatch.setattr(service.time, "sleep", lambda seconds: sleeps.append(seconds))
    with pytest.raises(RuntimeError, match="行情同步优先级"):
        service.start_external_data_sync("hot_rank", {"source": "watchlist"})
    assert len(sleeps) == 24
    assert sum(sleeps) == 120


@pytest.mark.parametrize("dataset", service.EXTERNAL_DATASETS)
def test_sync_plan_rejects_inverted_dates(dataset):
    with pytest.raises(ValueError, match="start_date must be on or before"):
        service.build_external_sync_plan(dataset, {"start_date": "2026-10-14", "end_date": "2026-10-13"})


@pytest.mark.parametrize("dataset", ["hot_rank", "tail_proxy", "etf"])
def test_snapshot_datasets_reject_backfill(dataset):
    with pytest.raises(ValueError, match="does not support backfill"):
        service.build_external_sync_plan(dataset, {"mode": "backfill"})


def test_selected_watchlist_filters_active_status_and_asset(db_session):
    stock, archived, inactive = _symbols(db_session, count=3)
    inactive.is_active = 0
    etf = Symbol(symbol="510300", name="audit-etf", market="sh", asset_type="etf")
    db_session.add(etf)
    first = Watchlist(name="core", list_type="manual")
    second = Watchlist(name="other", list_type="manual")
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add_all([
        WatchlistItem(watchlist_id=first.id, symbol_id=stock.id),
        WatchlistItem(watchlist_id=first.id, symbol_id=archived.id, status="archived"),
        WatchlistItem(watchlist_id=first.id, symbol_id=inactive.id),
        WatchlistItem(watchlist_id=first.id, symbol_id=etf.id),
        WatchlistItem(watchlist_id=second.id, symbol_id=archived.id),
    ])
    db_session.commit()
    assert [item.id for item in service.resolve_external_symbols(db_session, "watchlist", "stock", first.id)] == [stock.id]
    assert [item.id for item in service.resolve_external_symbols(db_session, "watchlist", "etf", first.id)] == [etf.id]


def test_cancel_done_task_keeps_its_plan_done(db_session, monkeypatch):
    task, payload = _task(db_session, "hot_rank", {"source": "watchlist"}, monkeypatch)
    row = db_session.get(AsyncTaskRecord, task.id)
    row.status = "done"
    row.is_terminal_locked = 1
    plan = db_session.get(DataSyncPlan, payload["sync_plan_id"])
    plan.status = "done"
    partition = db_session.query(DataSyncPartition).filter_by(plan_id=plan.id).one()
    partition.status = "done"
    db_session.commit()
    assert service.cancel_external_data_sync(task.id).status == "done"
    db_session.refresh(plan)
    assert plan.status == "done"


def test_same_dataset_concurrent_creation_has_one_owner(db_session, monkeypatch):
    original_thread = threading.Thread
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def caller():
        try:
            # Align the two HTTP-equivalent calls before either can own the
            # creation lock; do not force a rendezvous inside the protected SQL.
            barrier.wait(timeout=10)
            results.append(service.start_external_data_sync("hot_rank", {"source": "watchlist"}))
        except RuntimeError as exc:
            failures.append(exc)

    monkeypatch.setattr(service.threading, "Thread", _DeferredThread)
    callers = [original_thread(target=caller) for _ in range(2)]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert len(results) == 1 and len(failures) == 1


@pytest.mark.parametrize("normalize", [fundamental_data._market_code_for_akshare, capital_flow_data._market_code_for_akshare, etf_basic_data._etf_code])
def test_provider_symbol_normalization_supports_exchange_suffix(normalize):
    symbol = Symbol(symbol="600519.SH", name="audit", market="sh", asset_type="stock")
    assert normalize(symbol) == "600519"


def _portfolios_with_position(db, symbol, quantities):
    portfolios = [Portfolio(name=f"audit-portfolio-{i}", account_type="sim", total_capital=10000, investable_ratio=0.9, cash_reserve_ratio=0.1) for i in range(len(quantities))]
    db.add_all(portfolios)
    db.flush()
    db.add_all([Position(portfolio_id=portfolio.id, symbol_id=symbol.id, quantity=quantity, asset_type=symbol.asset_type) for portfolio, quantity in zip(portfolios, quantities)])
    db.commit()


def test_public_position_scope_deduplicates_shared_instruments(db_session, monkeypatch):
    symbol = _symbols(db_session, count=1)[0]
    _portfolios_with_position(db_session, symbol, [100, 100])
    task, payload = _task(db_session, "fundamental", {"source": "positions"}, monkeypatch)
    details = service.get_external_sync_partitions(task.id)
    assert len(details["partitions"]) == 1


def test_position_scope_excludes_zero_quantity(db_session):
    symbol = _symbols(db_session, count=1)[0]
    _portfolios_with_position(db_session, symbol, [0])
    assert service.resolve_external_symbols(db_session, "positions", "stock") == []


def test_cross_process_sqlite_creation_has_one_owner(db_session, tmp_sqlite_url, tmp_path):
    script = r'''
import json, sys
from pathlib import Path
from time import monotonic, sleep
from app.db.manager import DatabaseManager
from app.services import external_data_sync_task as service
DatabaseManager.get().initialize(sys.argv[1], db_type="sqlite")
class Deferred:
    def __init__(self, **kwargs): pass
    def start(self): pass
service.threading.Thread = Deferred
ready = Path(sys.argv[2])
ready.write_text("ready")
deadline = monotonic() + 10
while not Path(sys.argv[3]).exists():
    if monotonic() > deadline: raise RuntimeError("test rendezvous timeout")
    sleep(0.01)
try:
    task = service.start_external_data_sync("hot_rank", {"source":"watchlist"})
    print(json.dumps({"created":task.id}))
except RuntimeError as exc:
    print(json.dumps({"rejected":str(exc)}, ensure_ascii=True))
'''
    markers = [tmp_path / f"ready-{index}" for index in range(2)]
    processes = [subprocess.Popen([sys.executable, "-c", script, tmp_sqlite_url, str(markers[index]), str(markers[1-index])], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for index in range(2)]
    results = []
    try:
        for process in processes:
            output, error = process.communicate(timeout=20)
            assert process.returncode == 0, error
            results.append(json.loads(output.splitlines()[-1]))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert sum("created" in result for result in results) == 1
    assert sum("rejected" in result for result in results) == 1
    assert db_session.query(AsyncTaskRecord).filter_by(task_type="external_sync_hot_rank", status="queued").count() == 1


def test_retry_only_failed_partitions_preserves_completed_rows(db_session, monkeypatch):
    symbols = _symbols(db_session, count=2)
    task, payload = _task(db_session, "financial", {"source": "all"}, monkeypatch)
    row = db_session.get(AsyncTaskRecord, task.id)
    row.status = "failed"
    row.is_terminal_locked = 1
    partitions = db_session.query(DataSyncPartition).filter_by(plan_id=payload["sync_plan_id"]).all()
    for partition in partitions:
        partition.status = "done" if partition.symbol_id == symbols[0].id else "failed"
    db_session.commit()
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(service.threading, "Thread", _DeferredThread)
        retried = service.retry_external_data_sync(task.id)
    retry_plan = service.get_external_sync_partitions(retried.id)
    assert [partition["symbol_id"] for partition in retry_plan["partitions"]] == [symbols[1].id]
    assert retry_plan["plan"]["parent_plan_id"] == payload["sync_plan_id"]
    original = service.get_external_sync_partitions(task.id)
    assert next(partition["status"] for partition in original["partitions"] if partition["symbol_id"] == symbols[0].id) == "done"


def test_mirror_warning_retry_has_no_provider_partitions(db_session, monkeypatch):
    task, payload = _task(db_session, "hot_rank", {"source": "watchlist"}, monkeypatch)
    row = db_session.get(AsyncTaskRecord, task.id)
    row.status = "failed"
    row.is_terminal_locked = 1
    row.result_json = json.dumps({"warehouse_mirror": {"status": "warning"}, "written_start_date": "2026-10-13", "written_end_date": "2026-10-13"})
    partition = db_session.query(DataSyncPartition).filter_by(plan_id=payload["sync_plan_id"]).one()
    partition.status = "done"
    db_session.commit()
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(service.threading, "Thread", _DeferredThread)
        retried = service.retry_external_data_sync(task.id)
    assert json.loads(retried.payload_json)["_mirror_only"] is True
    assert service.get_external_sync_partitions(retried.id)["partitions"] == []


def test_tail_all_scope_rejected_before_task_creation(db_session, monkeypatch):
    with pytest.raises(ValueError, match="不支持全市场"):
        service.start_external_data_sync("tail_proxy", {"source": "all"})
    assert db_session.query(AsyncTaskRecord).count() == 0


def test_positions_can_be_limited_to_selected_portfolio(db_session):
    symbols = _symbols(db_session, count=2)
    _portfolios_with_position(db_session, symbols[0], [100])
    selected = db_session.query(Portfolio).one()
    other = Portfolio(name="other", account_type="sim", total_capital=10000, investable_ratio=0.9, cash_reserve_ratio=0.1)
    db_session.add(other)
    db_session.flush()
    db_session.add(Position(portfolio_id=other.id, symbol_id=symbols[1].id, quantity=100, asset_type="stock"))
    db_session.commit()
    assert [symbol.id for symbol in service.resolve_external_symbols(db_session, "positions", "stock", portfolio_id=selected.id)] == [symbols[0].id]


def test_missing_northbound_api_is_reported_in_capabilities(monkeypatch):
    monkeypatch.delattr(capital_flow_data.ak, "stock_hsgt_north_net_flow_in", raising=False)
    availability = service.get_external_sync_capabilities()["capital_flow"]["northbound"]
    assert availability["available"] is False
    assert availability["reason"]


def test_valuation_incremental_history_rejected_at_api_without_provider(db_session, monkeypatch):
    from app.api.routes.external_data import ExternalSyncTaskCreate, create_external_data_sync_task
    from fastapi import HTTPException
    def forbidden(*_args, **_kwargs):
        raise AssertionError("rejected history must not request a current provider snapshot")
    monkeypatch.setattr(fundamental_data, "sync_market_valuation_snapshot", forbidden)
    with pytest.raises(HTTPException) as rejected:
        create_external_data_sync_task(ExternalSyncTaskCreate(dataset="fundamental", mode="incremental", end_date=date(2020, 1, 2)))
    assert rejected.value.status_code == 400
    assert db_session.query(AsyncTaskRecord).count() == 0
    assert service.build_external_sync_plan("fundamental", {"mode": "backfill", "start_date": "2020-01-01", "end_date": "2020-01-02"})["mode"] == "backfill"


def test_thread_start_failure_is_terminal_and_retryable(db_session, monkeypatch):
    class BrokenThread(_DeferredThread):
        def start(self):
            raise RuntimeError("audit no worker capacity")
    monkeypatch.setattr(service.threading, "Thread", BrokenThread)
    with pytest.raises(RuntimeError, match="可重试"):
        service.start_external_data_sync("hot_rank", {"source": "watchlist"})
    failed = db_session.query(AsyncTaskRecord).one()
    assert failed.status == "failed"
    assert failed.is_terminal_locked == 1
    assert service.get_external_sync_partitions(failed.id)["plan"]["status"] == "failed"
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(service.threading, "Thread", _DeferredThread)
        assert service.retry_external_data_sync(failed.id).status == "queued"


def _mysql_guard_db(database, outcome=1):
    connection = MagicMock()
    connection.execute.return_value.scalar.return_value = outcome
    bind = MagicMock()
    bind.dialect.name = "mysql"
    bind.url.database = database
    bind.connect.return_value = connection
    db = MagicMock()
    db.get_bind.return_value = bind
    return db, connection


def test_mysql_creation_guard_namespaced_and_released():
    first_db, first_conn = _mysql_guard_db("first_database")
    second_db, second_conn = _mysql_guard_db("second_database")
    with service._external_sync_creation_guard(first_db, "hot_rank"):
        pass
    with service._external_sync_creation_guard(second_db, "hot_rank"):
        pass
    assert first_conn.execute.call_args_list[0].args[1]["name"] != second_conn.execute.call_args_list[0].args[1]["name"]
    assert "GET_LOCK" in str(first_conn.execute.call_args_list[0].args[0])
    assert "RELEASE_LOCK" in str(first_conn.execute.call_args_list[1].args[0])
    first_conn.close.assert_called_once()


@pytest.mark.parametrize("outcome", [0, None])
def test_mysql_creation_guard_fails_closed(outcome):
    db, connection = _mysql_guard_db("test_database", outcome)
    with pytest.raises(RuntimeError, match="在创建"):
        with service._external_sync_creation_guard(db, "hot_rank"):
            pytest.fail("unowned lock must not proceed to task creation")
    connection.close.assert_called_once()
    assert service._CREATION_LOCKS["hot_rank"].acquire(blocking=False)
    service._CREATION_LOCKS["hot_rank"].release()


def test_public_start_observes_market_completion_during_wait(db_session, monkeypatch):
    row = AsyncTaskRecord(id="market-becomes-done", task_type="universe_sync", status="running", stage="sync")
    db_session.add(row)
    db_session.commit()
    waits = []
    def complete_during_wait(seconds):
        waits.append(seconds)
        modifier = service.SessionLocal()
        try:
            current = modifier.get(AsyncTaskRecord, row.id)
            current.status = "done"
            current.is_terminal_locked = 1
            modifier.commit()
        finally:
            modifier.close()
    monkeypatch.setattr(service.time, "sleep", complete_during_wait)
    monkeypatch.setattr(service.threading, "Thread", _DeferredThread)
    created = service.start_external_data_sync("hot_rank", {"source": "watchlist"})
    assert created.status == "queued"
    assert waits == [5.0]


def test_prior_day_failed_valuation_retries_real_history(db_session, monkeypatch):
    symbol = _symbols(db_session, count=1)[0]
    task, payload = _task(db_session, "fundamental", {"source": "all"}, monkeypatch)
    yesterday = date.today() - timedelta(days=1)
    row = db_session.get(AsyncTaskRecord, task.id)
    row.status = "failed"
    row.is_terminal_locked = 1
    payload["plan"]["requested_start_date"] = yesterday.isoformat()
    payload["plan"]["requested_end_date"] = yesterday.isoformat()
    row.payload_json = json.dumps(payload)
    partition = db_session.query(DataSyncPartition).filter_by(plan_id=payload["sync_plan_id"]).one()
    partition.status = "failed"
    partition.start_date = yesterday
    partition.end_date = yesterday
    db_session.commit()
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(service.threading, "Thread", _DeferredThread)
        retried = service.retry_external_data_sync(task.id)
    plan = json.loads(retried.payload_json)["plan"]
    assert plan["mode"] == "backfill"
    assert plan["requested_start_date"] == plan["requested_end_date"] == yesterday.isoformat()
    assert service.get_external_sync_partitions(retried.id)["partitions"][0]["symbol_id"] == symbol.id


def test_mysql_lock_release_error_invalidates_connection_and_preserves_failure():
    db, connection = _mysql_guard_db("test_database")
    acquired = MagicMock()
    acquired.scalar.return_value = 1
    connection.execute.side_effect = [acquired, ConnectionError("lost connection")]
    with pytest.raises(ValueError, match="creation failed"):
        with service._external_sync_creation_guard(db, "hot_rank"):
            raise ValueError("creation failed")
    connection.invalidate.assert_called_once()
    connection.close.assert_called_once()
    assert service._CREATION_LOCKS["hot_rank"].acquire(blocking=False)
    service._CREATION_LOCKS["hot_rank"].release()


def test_current_capital_flow_ranking_cannot_overwrite_prior_session(db_session, monkeypatch):
    from app.models.capital_flow import CapitalFlow
    symbol = _symbols(db_session, count=1)[0]
    yesterday = date.today() - timedelta(days=1)
    db_session.add(CapitalFlow(symbol_id=symbol.id, trade_date=yesterday, main_net_inflow=5.0, source="historic"))
    db_session.commit()
    calls = []
    monkeypatch.setattr(capital_flow_data, "_fetch_individual_fund_flow_history", lambda *_a, **_k: [{"trade_date": yesterday, "main_net_inflow": 5.0}])
    def current_ranking(*_args, **_kwargs):
        calls.append("current")
        return [{"symbol_code": symbol.symbol, "main_net_inflow": 99.0}]
    monkeypatch.setattr(capital_flow_data, "_fetch_market_fund_flow_rank", current_ranking)
    with pytest.raises(ValueError, match="历史"):
        capital_flow_data.sync_market_capital_flow_snapshot(db_session, [symbol], yesterday)
    assert calls == []
    assert db_session.query(CapitalFlow).filter_by(symbol_id=symbol.id, trade_date=yesterday).one().main_net_inflow == 5.0


def test_capital_flow_incremental_history_rejected_at_api(db_session, monkeypatch):
    from app.api.routes.external_data import ExternalSyncTaskCreate, create_external_data_sync_task
    from fastapi import HTTPException
    monkeypatch.setattr(service.threading, "Thread", _DeferredThread)
    with pytest.raises(HTTPException) as rejected:
        create_external_data_sync_task(ExternalSyncTaskCreate(dataset="capital_flow", mode="incremental", end_date=date.today() - timedelta(days=1), include_northbound=False))
    assert rejected.value.status_code == 400
    assert db_session.query(AsyncTaskRecord).count() == 0


def test_prior_day_failed_capital_flow_retries_real_history(db_session, monkeypatch):
    symbol = _symbols(db_session, count=1)[0]
    task, payload = _task(db_session, "capital_flow", {"source": "all", "include_northbound": False}, monkeypatch)
    yesterday = date.today() - timedelta(days=1)
    row = db_session.get(AsyncTaskRecord, task.id)
    row.status = "failed"
    row.is_terminal_locked = 1
    payload["plan"]["requested_start_date"] = yesterday.isoformat()
    payload["plan"]["requested_end_date"] = yesterday.isoformat()
    row.payload_json = json.dumps(payload)
    partition = db_session.query(DataSyncPartition).filter_by(plan_id=payload["sync_plan_id"]).one()
    partition.status = "failed"
    partition.start_date = yesterday
    partition.end_date = yesterday
    db_session.commit()
    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(service.threading, "Thread", _DeferredThread)
        retried = service.retry_external_data_sync(task.id)
    plan = json.loads(retried.payload_json)["plan"]
    assert plan["mode"] == "backfill"
    assert plan["requested_start_date"] == plan["requested_end_date"] == yesterday.isoformat()
    assert service.get_external_sync_partitions(retried.id)["partitions"][0]["symbol_id"] == symbol.id

