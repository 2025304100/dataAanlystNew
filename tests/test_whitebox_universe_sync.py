from __future__ import annotations

import json
import threading

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import inspect, select

from app.api.routes import universe as universe_routes
from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import market_data, universe_sync, universe_sync_task

pytestmark = pytest.mark.whitebox


def test_universe_incremental_pending_index_covers_sync_metadata(db_session):
    indexes = {
        item["name"]: item["column_names"]
        for item in inspect(db_session.get_bind()).get_indexes("universe_symbols")
    }

    assert indexes["ix_universe_incremental_pending"] == [
        "region",
        "asset_type",
        "is_synced",
        "last_bar_date",
        "last_synced_at",
        "sync_failed",
    ]


def test_upsert_bars_dedupes_duplicate_trade_dates_within_frame(db_session):
    symbol = Symbol(symbol="TESTBAR", name="Test Bar", asset_type="stock", market="sh", board="main", is_active=1)
    db_session.add(symbol)
    db_session.commit()
    db_session.refresh(symbol)

    frame = pd.DataFrame([
        {"trade_date": date(2026, 1, 5), "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.2, "volume": 1000, "amount": 10000, "turnover_rate": 1.0},
        {"trade_date": date(2026, 1, 5), "open": 10.1, "high": 11.2, "low": 9.6, "close": 10.8, "volume": 1200, "amount": 12000, "turnover_rate": 1.1},
    ])

    inserted, updated = market_data._upsert_bars(db_session, symbol, frame)
    db_session.commit()

    rows = db_session.execute(
        select(DailyBar).where(DailyBar.symbol_id == symbol.id)
    ).scalars().all()

    assert inserted == 1
    assert updated == 0
    assert len(rows) == 1
    assert rows[0].close == pytest.approx(10.8)
    assert rows[0].volume == pytest.approx(1200.0)


def test_upsert_universe_bars_dedupes_duplicate_trade_dates_within_frame(db_session):
    universe_symbol = UniverseSymbol(symbol="600000", name="Test Universe", asset_type="stock", market="sh", region="cn")
    db_session.add(universe_symbol)
    db_session.commit()
    db_session.refresh(universe_symbol)

    frame = pd.DataFrame([
        {"trade_date": date(2026, 2, 6), "open": 20.0, "high": 21.0, "low": 19.5, "close": 20.1, "volume": 2000, "amount": 20000, "turnover_rate": 2.0},
        {"trade_date": date(2026, 2, 6), "open": 20.2, "high": 21.3, "low": 19.7, "close": 20.9, "volume": 2400, "amount": 24000, "turnover_rate": 2.4},
    ])

    inserted, updated = universe_sync._upsert_universe_bars(db_session, universe_symbol.id, frame)
    db_session.commit()

    rows = db_session.execute(
        select(UniverseDailyBar).where(UniverseDailyBar.universe_symbol_id == universe_symbol.id)
    ).scalars().all()

    assert inserted == 1
    assert updated == 0
    assert len(rows) == 1
    assert rows[0].close == pytest.approx(20.9)
    assert rows[0].volume == pytest.approx(2400.0)


def test_backfill_batch_prefilters_symbols_already_covering_target_range(monkeypatch, db_session):
    target_start = date.today() - timedelta(days=365)

    covered = UniverseSymbol(symbol="600001", name="Covered", asset_type="stock", market="sh", region="cn", is_synced=1)
    needs_extend = UniverseSymbol(symbol="600002", name="Needs Extend", asset_type="stock", market="sh", region="cn", is_synced=1)
    no_data = UniverseSymbol(symbol="600003", name="No Data", asset_type="stock", market="sh", region="cn", is_synced=1)
    db_session.add_all([covered, needs_extend, no_data])
    db_session.commit()
    db_session.refresh(covered)
    db_session.refresh(needs_extend)
    db_session.refresh(no_data)

    db_session.add_all([
        UniverseDailyBar(universe_symbol_id=covered.id, trade_date=target_start - timedelta(days=30), close=10.0),
        UniverseDailyBar(universe_symbol_id=covered.id, trade_date=target_start + timedelta(days=30), close=10.5),
        UniverseDailyBar(universe_symbol_id=needs_extend.id, trade_date=target_start + timedelta(days=30), close=11.0),
    ])
    db_session.commit()

    called_ids: list[int] = []

    def fake_backfill_one(universe_symbol_id: int, history_days: int) -> dict:
        called_ids.append(universe_symbol_id)
        return {"symbol": str(universe_symbol_id), "status": "skipped", "reason": "test"}

    monkeypatch.setattr(universe_sync, "_backfill_one_concurrent", fake_backfill_one)

    result = universe_sync.backfill_universe_bars_batch(
        "cn-stock",
        max_workers=1,
        history_days=365,
    )

    assert called_ids == [needs_extend.id, no_data.id]
    assert result["total"] == 2
    assert result["processed"] == 2
    assert result["skipped"] == 2


@pytest.mark.parametrize(
    ("as_of", "region", "expected"),
    [
        (datetime(2026, 8, 1, 12, 0), "cn", date(2026, 7, 31)),
        (datetime(2026, 8, 3, 10, 0), "cn", date(2026, 7, 31)),
        (datetime(2026, 8, 3, 18, 0), "cn", date(2026, 8, 3)),
        (datetime(2026, 8, 3, 18, 0), "us", date(2026, 7, 31)),
        (datetime(2026, 8, 4, 10, 0), "us", date(2026, 8, 3)),
    ],
)
def test_latest_completed_trading_date_uses_market_timezone(as_of, region, expected):
    assert universe_sync._latest_completed_trading_date(as_of, region) == expected


def test_incremental_sync_filters_pending_symbols_by_scope(monkeypatch, db_session):
    target_date = date(2026, 7, 31)

    cn_stock_stale = UniverseSymbol(
        symbol="600010",
        name="CN Stock Stale",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=target_date - timedelta(days=1),
    )
    cn_etf_stale = UniverseSymbol(
        symbol="510300",
        name="CN ETF Stale",
        asset_type="etf",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=target_date - timedelta(days=1),
    )
    cn_stock_uptodate = UniverseSymbol(
        symbol="600011",
        name="CN Stock Fresh",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=target_date,
    )
    db_session.add_all([cn_stock_stale, cn_etf_stale, cn_stock_uptodate])
    db_session.commit()
    db_session.refresh(cn_stock_stale)
    db_session.refresh(cn_etf_stale)
    db_session.refresh(cn_stock_uptodate)

    called_ids: list[int] = []

    def fake_sync_one(universe_symbol_id: int, incremental_target: date) -> dict:
        called_ids.append(universe_symbol_id)
        assert incremental_target == target_date
        return {
            "symbol": str(universe_symbol_id),
            "status": "ok",
            "inserted": 1,
            "updated": 0,
            "fetch_seconds": 1.25,
            "database_seconds": 0.05,
        }

    monkeypatch.setattr(universe_sync, "_sync_one_incremental_concurrent", fake_sync_one)

    result = universe_sync.incremental_sync(
        max_workers=1,
        scopes=["cn-stock"],
        as_of=datetime(2026, 8, 1, 12, 0),
    )

    assert called_ids == [cn_stock_stale.id]
    assert result["total"] == 1
    assert result["processed"] == 1
    assert result["ok"] == 1
    assert result["failed"] == 0
    assert result["attempts"] == 1
    assert result["average_fetch_seconds"] == pytest.approx(1.25)
    assert result["average_database_seconds"] == pytest.approx(0.05)


def test_incremental_sync_uses_market_specific_target_dates(monkeypatch, db_session):
    cn_stock = UniverseSymbol(
        symbol="600015",
        name="CN Monday",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date(2026, 7, 31),
    )
    us_etf = UniverseSymbol(
        symbol="SPY",
        name="US Friday",
        asset_type="etf",
        market="us",
        region="us",
        is_synced=1,
        last_bar_date=date(2026, 7, 30),
    )
    db_session.add_all([cn_stock, us_etf])
    db_session.commit()

    targets: dict[int, date] = {}

    def fake_sync_one(universe_symbol_id: int, target_date: date) -> dict:
        targets[universe_symbol_id] = target_date
        return {"symbol": str(universe_symbol_id), "status": "ok", "inserted": 1, "updated": 0}

    monkeypatch.setattr(universe_sync, "_sync_one_incremental_concurrent", fake_sync_one)

    result = universe_sync.incremental_sync(
        max_workers=1,
        scopes=["cn-stock", "us-etf"],
        as_of=datetime(2026, 8, 3, 18, 0),
    )

    assert targets == {
        cn_stock.id: date(2026, 8, 3),
        us_etf.id: date(2026, 7, 31),
    }
    assert result["ok"] == 2


def test_incremental_sync_skips_symbols_already_attempted_that_day(monkeypatch, db_session):
    attempted = UniverseSymbol(
        symbol="600012",
        name="Already Attempted",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date(2026, 7, 30),
        last_synced_at=datetime(2026, 8, 1, 1, 0),
    )
    pending = UniverseSymbol(
        symbol="600013",
        name="Pending",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date(2026, 7, 30),
        last_synced_at=datetime(2026, 7, 31, 23, 0),
    )
    db_session.add_all([attempted, pending])
    db_session.commit()
    db_session.refresh(pending)

    called_ids: list[int] = []

    def fake_sync_one(universe_symbol_id: int, target_date: date) -> dict:
        called_ids.append(universe_symbol_id)
        return {"symbol": str(universe_symbol_id), "status": "empty", "inserted": 0, "updated": 0}

    monkeypatch.setattr(universe_sync, "_sync_one_incremental_concurrent", fake_sync_one)

    result = universe_sync.incremental_sync(
        max_workers=1,
        scopes=["cn-stock"],
        as_of=datetime(2026, 8, 1, 12, 0),
    )

    assert called_ids == [pending.id]
    assert result["total"] == 1
    assert result["uptodate"] == 1
    assert result["failed"] == 0


def test_incremental_worker_uses_metadata_and_increments_cached_bar_count(monkeypatch, db_session):
    universe_symbol = UniverseSymbol(
        symbol="600014",
        name="Metadata Tail",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date(2026, 7, 30),
        bar_count=100,
    )
    db_session.add(universe_symbol)
    db_session.commit()
    db_session.refresh(universe_symbol)

    calls: list[tuple[date, date]] = []

    def fake_fetch(symbol: UniverseSymbol, start_date: date, end_date: date) -> pd.DataFrame:
        calls.append((start_date, end_date))
        return pd.DataFrame([{"trade_date": date(2026, 7, 31), "close": 10.5}])

    monkeypatch.setattr(universe_sync, "_fetch_universe_history", fake_fetch)

    result = universe_sync.sync_one_universe_symbol_incremental(
        db_session,
        universe_symbol,
        date(2026, 7, 31),
    )
    db_session.refresh(universe_symbol)

    assert result["status"] == "ok"
    assert calls == [(date(2026, 7, 31), date(2026, 7, 31))]
    assert universe_symbol.last_bar_date == date(2026, 7, 31)
    assert universe_symbol.bar_count == 101


def test_incremental_sync_replenishes_worker_before_slowest_finishes(monkeypatch, db_session):
    symbols = [
        UniverseSymbol(
            symbol=f"60002{index}",
            name=f"Sliding {index}",
            asset_type="stock",
            market="sh",
            region="cn",
            is_synced=1,
            last_bar_date=date(2026, 7, 30),
        )
        for index in range(3)
    ]
    db_session.add_all(symbols)
    db_session.commit()
    for symbol in symbols:
        db_session.refresh(symbol)

    first_can_finish = threading.Event()
    third_started = threading.Event()

    def fake_sync_one(universe_symbol_id: int, target_date: date) -> dict:
        if universe_symbol_id == symbols[0].id:
            if not first_can_finish.wait(timeout=2):
                return {"symbol": str(universe_symbol_id), "status": "failed", "error": "window stalled"}
        elif universe_symbol_id == symbols[2].id:
            third_started.set()
            first_can_finish.set()
        return {"symbol": str(universe_symbol_id), "status": "ok", "inserted": 1, "updated": 0}

    monkeypatch.setattr(universe_sync, "_sync_one_incremental_concurrent", fake_sync_one)

    result = universe_sync.incremental_sync(
        max_workers=2,
        scopes=["cn-stock"],
        as_of=datetime(2026, 8, 1, 12, 0),
    )

    assert third_started.is_set()
    assert result["processed"] == 3
    assert result["ok"] == 3


def test_incremental_route_forwards_selected_scopes(monkeypatch):
    captured: dict = {}
    sentinel = object()

    def fake_start(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        universe_routes.universe_sync_task,
        "start_universe_incremental_sync",
        fake_start,
    )

    result = universe_routes.start_incremental_sync(
        universe_routes.UniverseIncrementalRequest(
            max_workers=6,
            scopes=["cn-stock"],
        )
    )

    assert result is sentinel
    assert captured == {"max_workers": 6, "scopes": ["cn-stock"]}


def test_incremental_task_throttles_progress_persistence(monkeypatch, db_session):
    task_id = "incremental-progress-throttle"
    db_session.add(AsyncTaskRecord(
        id=task_id,
        task_type=universe_sync_task.UNIVERSE_INCREMENTAL_TASK_TYPE,
        status="queued",
        stage="queued",
    ))
    db_session.commit()

    updates: list[dict] = []
    original_set_task = universe_sync_task._set_task

    def recording_set_task(db, current_task_id: str, **kwargs):
        updates.append(dict(kwargs))
        return original_set_task(db, current_task_id, **kwargs)

    def fake_incremental(**kwargs):
        callback = kwargs["progress_callback"]
        for processed in range(1, 21):
            callback(processed, 20, processed, 0)
        return {
            "total": 20,
            "processed": 20,
            "ok": 20,
            "failed": 0,
            "skipped": 0,
            "uptodate": 0,
        }

    monkeypatch.setattr(universe_sync_task, "_set_task", recording_set_task)
    monkeypatch.setattr(universe_sync_task, "_is_cancelled", lambda task_id: False)
    monkeypatch.setattr(universe_sync_task.universe_sync, "incremental_sync", fake_incremental)

    universe_sync_task._run_universe_incremental_sync(task_id, 5, ["cn-stock"])

    progress_updates = [item for item in updates if "processed" in item]
    assert len(progress_updates) == 2
    assert progress_updates[0]["processed"] == 1
    assert progress_updates[-1]["processed"] == 20


def test_incremental_task_marks_sync_exception_failed(monkeypatch, db_session):
    task_id = "incremental-worker-failure"
    db_session.add(AsyncTaskRecord(
        id=task_id,
        task_type=universe_sync_task.UNIVERSE_INCREMENTAL_TASK_TYPE,
        status="queued",
        stage="queued",
    ))
    db_session.commit()

    def fake_incremental(**kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(universe_sync_task, "_is_cancelled", lambda task_id: False)
    monkeypatch.setattr(universe_sync_task.universe_sync, "incremental_sync", fake_incremental)

    universe_sync_task._run_universe_incremental_sync(task_id, 5, ["cn-stock"])
    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, task_id)

    assert task is not None
    assert task.status == "failed"
    assert task.stage == "failed"
    assert "upstream unavailable" in task.message



def test_repair_one_universe_symbol_clamps_range_and_upserts_chunks(monkeypatch, db_session):
    universe_symbol = UniverseSymbol(
        symbol="600020",
        name="Repair Me",
        asset_type="stock",
        market="sh",
        region="cn",
        listed_at=date(2026, 1, 1),
        is_synced=1,
    )
    db_session.add(universe_symbol)
    db_session.commit()
    db_session.refresh(universe_symbol)

    db_session.add_all([
        UniverseDailyBar(universe_symbol_id=universe_symbol.id, trade_date=date(2026, 1, 3), close=10.0),
        UniverseDailyBar(universe_symbol_id=universe_symbol.id, trade_date=date(2026, 1, 5), close=15.0),
    ])
    db_session.commit()

    calls: list[tuple[date, date]] = []

    def fake_fetch(us: UniverseSymbol, start_date: date, end_date: date) -> pd.DataFrame:
        calls.append((start_date, end_date))
        if (start_date, end_date) == (date(2026, 1, 3), date(2026, 1, 4)):
            return pd.DataFrame([
                {"trade_date": date(2026, 1, 3), "close": 30.0},
                {"trade_date": date(2026, 1, 4), "close": 40.0},
            ])
        if (start_date, end_date) == (date(2026, 1, 5), date(2026, 1, 5)):
            return pd.DataFrame([
                {"trade_date": date(2026, 1, 5), "close": 50.0},
            ])
        raise AssertionError(f"unexpected repair chunk: {(start_date, end_date)}")

    monkeypatch.setattr(universe_sync, "_fetch_universe_history", fake_fetch)

    result = universe_sync.repair_one_universe_symbol(
        db_session,
        universe_symbol,
        start_date=date(2025, 12, 1),
        end_date=date(2026, 2, 1),
        chunk_days=2,
    )

    rows = db_session.execute(
        select(UniverseDailyBar)
        .where(UniverseDailyBar.universe_symbol_id == universe_symbol.id)
        .order_by(UniverseDailyBar.trade_date.asc())
    ).scalars().all()

    assert calls == [
        (date(2026, 1, 3), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 5)),
    ]
    assert result["status"] == "ok"
    assert result["range_start"] == "2026-01-03"
    assert result["range_end"] == "2026-01-05"
    assert result["chunks"] == 2
    assert result["inserted"] == 1
    assert result["updated"] == 2
    assert [row.trade_date for row in rows] == [
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
    ]
    assert [row.close for row in rows] == [pytest.approx(30.0), pytest.approx(40.0), pytest.approx(50.0)]


def test_range_repair_batch_prefilters_by_actual_existing_range(monkeypatch, db_session):
    target_start = date.today() - timedelta(days=365)

    overlap_missing_meta = UniverseSymbol(
        symbol="600030",
        name="Overlap Missing Meta",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=None,
    )
    outside_window = UniverseSymbol(
        symbol="600031",
        name="Outside Window",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date.today(),
    )
    metadata_only = UniverseSymbol(
        symbol="600032",
        name="Metadata Only",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date.today(),
    )
    other_scope = UniverseSymbol(
        symbol="510500",
        name="Other Scope",
        asset_type="etf",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=date.today(),
    )
    db_session.add_all([overlap_missing_meta, outside_window, metadata_only, other_scope])
    db_session.commit()
    db_session.refresh(overlap_missing_meta)
    db_session.refresh(outside_window)
    db_session.refresh(metadata_only)
    db_session.refresh(other_scope)

    db_session.add_all([
        UniverseDailyBar(
            universe_symbol_id=overlap_missing_meta.id,
            trade_date=target_start + timedelta(days=10),
            close=10.0,
        ),
        UniverseDailyBar(
            universe_symbol_id=overlap_missing_meta.id,
            trade_date=target_start + timedelta(days=20),
            close=10.5,
        ),
        UniverseDailyBar(
            universe_symbol_id=outside_window.id,
            trade_date=target_start - timedelta(days=60),
            close=11.0,
        ),
        UniverseDailyBar(
            universe_symbol_id=outside_window.id,
            trade_date=target_start - timedelta(days=30),
            close=11.5,
        ),
        UniverseDailyBar(
            universe_symbol_id=other_scope.id,
            trade_date=target_start + timedelta(days=5),
            close=12.0,
        ),
    ])
    db_session.commit()

    called_ids: list[int] = []

    def fake_repair_one(universe_symbol_id: int, start_date: date, end_date: date, chunk_days: int, is_cancelled=None) -> dict:
        called_ids.append(universe_symbol_id)
        return {"symbol": str(universe_symbol_id), "status": "ok", "inserted": 0, "updated": 1, "empty_chunks": 0}

    monkeypatch.setattr(universe_sync, "_repair_one_concurrent", fake_repair_one)

    result = universe_sync.repair_universe_bars_batch(
        "cn-stock",
        max_workers=1,
        history_days=365,
        chunk_days=90,
    )

    assert called_ids == [overlap_missing_meta.id]
    assert result["total"] == 1
    assert result["processed"] == 1
    assert result["ok"] == 1
    assert result["failed"] == 0
    assert result["updated"] == 1

def test_run_universe_smart_sync_orders_init_backfill_and_incremental(monkeypatch, db_session):
    task = AsyncTaskRecord(
        id="smart-task-1",
        task_type=universe_sync_task.UNIVERSE_SMART_TASK_TYPE,
        status="queued",
        stage="queued",
        percent=0,
        message="queued",
    )
    db_session.add(task)
    db_session.commit()

    call_order: list[str] = []

    def fake_refresh(scope: str, db) -> dict:
        call_order.append(f"refresh:{scope}")
        return {"scope": scope, "seen": 1, "created": 0, "skipped_suspended": 0}

    def fake_init(scope: str, **kwargs) -> dict:
        call_order.append(f"init:{scope}")
        progress_callback = kwargs.get("progress_callback")
        if progress_callback:
            progress_callback(1, 1, 1, 0)
        return {"total": 1, "processed": 1, "ok": 1, "failed": 0, "skipped": 0}

    def fake_backfill(scope: str, **kwargs) -> dict:
        call_order.append(f"backfill:{scope}")
        progress_callback = kwargs.get("progress_callback")
        if progress_callback:
            progress_callback(1, 1, 1, 0)
        return {"total": 1, "processed": 1, "ok": 1, "failed": 0, "skipped": 0}

    def fake_incremental(**kwargs) -> dict:
        scopes = kwargs.get("scopes") or []
        call_order.append(f"incremental:{','.join(scopes)}")
        progress_callback = kwargs.get("progress_callback")
        if progress_callback:
            progress_callback(1, 1, 1, 0)
        return {"total": 1, "processed": 1, "ok": 1, "failed": 0, "skipped": 0, "uptodate": 0}

    monkeypatch.setattr(universe_sync_task.universe_sync, "refresh_universe_symbols", fake_refresh)
    monkeypatch.setattr(universe_sync_task.universe_sync, "sync_universe_bars_batch", fake_init)
    monkeypatch.setattr(universe_sync_task.universe_sync, "backfill_universe_bars_batch", fake_backfill)
    monkeypatch.setattr(universe_sync_task.universe_sync, "incremental_sync", fake_incremental)

    universe_sync_task._run_universe_smart_sync(
        task.id,
        max_workers=2,
        history_days=3650,
        scopes=["cn-stock", "us-etf"],
        sync_limit=300,
    )

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)

    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.stage == "done"
    assert call_order == [
        "refresh:cn-stock",
        "refresh:us-etf",
        "init:cn-stock",
        "init:us-etf",
        "backfill:cn-stock",
        "backfill:us-etf",
        "incremental:cn-stock,us-etf",
    ]



def test_run_universe_range_repair_orders_scopes_and_marks_done(monkeypatch, db_session):
    task = AsyncTaskRecord(
        id="repair-task-1",
        task_type=universe_sync_task.UNIVERSE_RANGE_REPAIR_TASK_TYPE,
        status="queued",
        stage="queued",
        percent=0,
        message="queued",
    )
    db_session.add(task)
    db_session.commit()

    call_order: list[str] = []

    def fake_repair(scope: str, **kwargs) -> dict:
        call_order.append(scope)
        progress_callback = kwargs.get("progress_callback")
        if progress_callback:
            progress_callback(1, 1, 1, 0)
        return {"total": 1, "processed": 1, "ok": 1, "failed": 0, "skipped": 0, "inserted": 2, "updated": 3}

    monkeypatch.setattr(universe_sync_task.universe_sync, "repair_universe_bars_batch", fake_repair)

    universe_sync_task._run_universe_range_repair(
        task.id,
        max_workers=2,
        history_days=365,
        chunk_days=90,
        scopes=["cn-stock", "us-etf"],
        sync_limit=300,
    )

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)

    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.stage == "done"
    assert call_order == ["cn-stock", "us-etf"]

    payload = json.loads(refreshed.result_json or "{}")
    assert payload["cn-stock_repair"]["updated"] == 3
    assert payload["us-etf_repair"]["inserted"] == 2
