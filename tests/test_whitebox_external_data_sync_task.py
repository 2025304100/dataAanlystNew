from __future__ import annotations

import json
from datetime import date

import pytest

from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.daily_bar import DailyBar
from app.models.async_task import AsyncTaskRecord
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import external_data_sync_task as service
from app.services import capital_flow_data


class _DeferredThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True


def test_all_scope_materializes_all_synced_universe_symbols(db_session):
    """The external-data all scope must not be limited to promoted symbols."""
    db_session.add(
        UniverseSymbol(
            symbol="600000",
            name="Test Bank",
            asset_type="stock",
            market="sh",
            region="cn",
            board="main",
            industry="Banking",
            is_synced=1,
        )
    )
    db_session.commit()

    resolved = service.resolve_external_symbols(db_session, "all", "stock")

    assert [item.symbol for item in resolved] == ["600000"]
    materialized = db_session.query(Symbol).filter(Symbol.symbol == "600000").one()
    assert materialized.is_active == 1
    assert materialized.industry == "Banking"


def test_overview_reads_real_external_tables(db_session):
    symbol = Symbol(symbol="600000", name="浦发银行", asset_type="stock", market="cn")
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all([
        StockValuation(symbol_id=symbol.id, trade_date=date(2026, 8, 1), source="test"),
        CapitalFlow(symbol_id=symbol.id, trade_date=date(2026, 8, 2), source="test"),
        NorthboundFlow(trade_date=date(2026, 8, 2), source="test"),
    ])
    db_session.commit()

    overview = service.get_external_data_overview(db_session)
    by_dataset = {item["dataset"]: item for item in overview["datasets"]}

    assert len(overview["datasets"]) == 7
    assert by_dataset["fundamental"]["records"] == 1
    assert by_dataset["fundamental"]["symbols"] == 1
    assert by_dataset["fundamental"]["latest_date"] == date(2026, 8, 1)
    assert by_dataset["capital_flow"]["records"] == 2
    assert by_dataset["capital_flow"]["symbols"] == 1
    assert by_dataset["capital_flow"]["latest_date"] == date(2026, 8, 2)
    assert overview["total_records"] == 3
    assert overview["available_datasets"] == 2


def test_start_creates_persisted_observable_task(db_session, monkeypatch):
    created_threads: list[_DeferredThread] = []
    symbol = Symbol(symbol="600001", name="Plan Test", asset_type="stock", market="sh")
    watchlist = Watchlist(name="sync-test", list_type="manual")
    db_session.add_all([symbol, watchlist])
    db_session.flush()
    db_session.add(WatchlistItem(watchlist_id=watchlist.id, symbol_id=symbol.id))
    db_session.commit()

    def make_thread(**kwargs):
        thread = _DeferredThread(**kwargs)
        created_threads.append(thread)
        return thread

    monkeypatch.setattr(service.threading, "Thread", make_thread)

    task = service.start_external_data_sync(
        "fundamental",
        {"source": "watchlist", "include_northbound": True},
    )

    assert task.task_type == "external_sync_fundamental"
    assert task.status == "queued"
    assert task.percent == 0
    assert created_threads and created_threads[0].started is True
    persisted = service.get_external_sync_task(task.id)
    assert persisted is not None
    assert persisted.id == task.id
    plan_details = service.get_external_sync_partitions(task.id)
    assert plan_details is not None
    assert plan_details["plan"]["dataset"] == "fundamental"
    assert plan_details["plan"]["status"] == "queued"
    assert len(plan_details["partitions"]) == 1
    assert plan_details["partitions"][0]["symbol"] == "600001"


def test_prevents_duplicate_external_sync_tasks(db_session, monkeypatch):
    monkeypatch.setattr(service.threading, "Thread", lambda **kwargs: _DeferredThread(**kwargs))
    service.start_external_data_sync("hot_rank", {"source": "watchlist"})

    with pytest.raises(RuntimeError, match="already running"):
        service.start_external_data_sync("financial", {"source": "watchlist"})


def test_market_data_task_has_priority_over_new_external_task(db_session, monkeypatch):
    db_session.add(
        AsyncTaskRecord(
            id="market-priority-test",
            task_type="universe_incremental_sync",
            status="running",
            stage="sync",
            percent=25,
            message="market sync",
        )
    )
    db_session.commit()
    monkeypatch.setattr(service.threading, "Thread", lambda **kwargs: _DeferredThread(**kwargs))

    with pytest.raises(RuntimeError, match="Market-data synchronization has priority"):
        service.start_external_data_sync("hot_rank", {"source": "watchlist"})


def test_cancel_and_retry_resume_from_frozen_external_plan(db_session, monkeypatch):
    monkeypatch.setattr(service.threading, "Thread", lambda **kwargs: _DeferredThread(**kwargs))
    original = service.start_external_data_sync(
        "capital_flow",
        {
            "source": "watchlist",
            "mode": "backfill",
            "start_date": "2026-06-01",
            "end_date": "2026-06-30",
        },
    )
    cancelled = service.cancel_external_data_sync(original.id)
    assert cancelled.status == "cancelled"

    persisted = db_session.get(AsyncTaskRecord, original.id)
    assert persisted is not None
    persisted.batch_recovery_json = json.dumps({
        "plan": json.loads(persisted.payload_json)["plan"],
        "last_symbol_id": 42,
        "processed": 4,
    })
    db_session.commit()

    retried = service.retry_external_data_sync(original.id)
    retry_payload = json.loads(retried.payload_json)
    assert retried.status == "queued"
    assert retry_payload["resume_after_symbol_id"] == 42
    assert retry_payload["plan"]["requested_start_date"] == "2026-06-01"
    assert retry_payload["plan"]["requested_end_date"] == "2026-06-30"


def test_external_sync_mirrors_only_the_completed_dataset(db_session, monkeypatch):
    from app.services.factors import data_sync

    captured: dict = {}

    class _MirrorResult:
        rows_written = 3
        batch_id = "factor-inputs-test"

    def fake_mirror(_db, **kwargs):
        captured.update(kwargs)
        return _MirrorResult()

    monkeypatch.setattr(data_sync, "mirror_factor_inputs", fake_mirror)
    result = service._mirror_external_factor_inputs(
        db_session,
        "missing-task-is-allowed-for-unit-test",
        "capital_flow",
        {
            "requested_start_date": "2026-06-01",
            "requested_end_date": "2026-06-30",
        },
    )

    assert result == {
        "status": "done",
        "records": 3,
        "batch_id": "factor-inputs-test",
    }
    assert captured["include_fund_flows"] is True
    assert captured["include_valuations"] is False
    assert captured["start_date"] == date(2026, 6, 1)
    assert captured["end_date"] == date(2026, 6, 30)


def test_sync_plan_rejects_unsupported_or_too_long_history():
    plan = service.build_external_sync_plan(
        "capital_flow",
        {"mode": "backfill", "lookback_days": 60},
        today=date(2026, 8, 15),
    )
    assert plan["requested_start_date"] == "2026-06-17"
    assert plan["requested_end_date"] == "2026-08-15"
    assert plan["provider_history_limit_days"] == 100

    with pytest.raises(ValueError, match="exceeds provider limit 100"):
        service.build_external_sync_plan(
            "capital_flow",
            {"mode": "backfill", "lookback_days": 101},
            today=date(2026, 8, 15),
        )
    fundamental_plan = service.build_external_sync_plan(
        "fundamental", {"mode": "backfill", "lookback_days": 60}, today=date(2026, 8, 15)
    )
    assert fundamental_plan["provider_history_limit_days"] is None
    with pytest.raises(ValueError, match="does not support backfill"):
        service.build_external_sync_plan("hot_rank", {"mode": "backfill"})


def test_capital_flow_history_range_upserts_only_provider_rows(db_session, monkeypatch):
    symbol = Symbol(symbol="600000", name="Test", asset_type="stock", market="cn")
    db_session.add(symbol)
    db_session.flush()
    provided_rows = [
        {"trade_date": date(2026, 8, 11), "main_net_inflow": 10.0},
        {"trade_date": date(2026, 8, 13), "main_net_inflow": -20.0},
    ]
    monkeypatch.setattr(
        capital_flow_data,
        "_fetch_individual_fund_flow_history",
        lambda *args, **kwargs: provided_rows,
    )

    first_written = capital_flow_data.sync_symbol_capital_flow_range(
        db_session, symbol, start_date=date(2026, 8, 10), end_date=date(2026, 8, 15)
    )
    db_session.commit()
    second_written = capital_flow_data.sync_symbol_capital_flow_range(
        db_session, symbol, start_date=date(2026, 8, 10), end_date=date(2026, 8, 15)
    )
    db_session.commit()

    rows = db_session.query(CapitalFlow).filter(CapitalFlow.symbol_id == symbol.id).all()
    assert first_written == 2
    assert second_written == 2
    assert {row.trade_date for row in rows} == {date(2026, 8, 11), date(2026, 8, 13)}


def test_gap_diagnostic_uses_existing_daily_bar_scope(db_session):
    symbol = Symbol(symbol="600000", name="Test", asset_type="stock", market="cn")
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all([
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 8, 10),
            open=1,
            high=1,
            low=1,
            close=1,
        ),
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 8, 11),
            open=1,
            high=1,
            low=1,
            close=1,
        ),
        CapitalFlow(
            symbol_id=symbol.id,
            trade_date=date(2026, 8, 10),
            source="test",
        ),
    ])
    db_session.commit()

    report = service.get_external_data_gaps(
        db_session,
        dataset="capital_flow",
        start_date=date(2026, 8, 10),
        end_date=date(2026, 8, 11),
    )

    assert report["total_missing"] == 1
    assert report["gaps"] == [{
        "symbol_id": symbol.id,
        "symbol": "600000",
        "trade_date": date(2026, 8, 11),
    }]
