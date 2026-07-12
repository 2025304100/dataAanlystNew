from __future__ import annotations

import json

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import market_data, universe_sync, universe_sync_task

pytestmark = pytest.mark.whitebox


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


def test_incremental_sync_filters_pending_symbols_by_scope(monkeypatch, db_session):
    today = date.today()

    cn_stock_stale = UniverseSymbol(
        symbol="600010",
        name="CN Stock Stale",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=today - timedelta(days=1),
    )
    cn_etf_stale = UniverseSymbol(
        symbol="510300",
        name="CN ETF Stale",
        asset_type="etf",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=today - timedelta(days=1),
    )
    cn_stock_uptodate = UniverseSymbol(
        symbol="600011",
        name="CN Stock Fresh",
        asset_type="stock",
        market="sh",
        region="cn",
        is_synced=1,
        last_bar_date=today,
    )
    db_session.add_all([cn_stock_stale, cn_etf_stale, cn_stock_uptodate])
    db_session.commit()
    db_session.refresh(cn_stock_stale)
    db_session.refresh(cn_etf_stale)
    db_session.refresh(cn_stock_uptodate)

    called_ids: list[int] = []

    def fake_sync_one(universe_symbol_id: int, history_days: int) -> dict:
        called_ids.append(universe_symbol_id)
        return {"symbol": str(universe_symbol_id), "status": "ok", "inserted": 1, "updated": 0}

    monkeypatch.setattr(universe_sync, "_sync_one_concurrent", fake_sync_one)

    result = universe_sync.incremental_sync(
        max_workers=1,
        scopes=["cn-stock"],
    )

    assert called_ids == [cn_stock_stale.id]
    assert result["total"] == 1
    assert result["processed"] == 1
    assert result["ok"] == 1
    assert result["failed"] == 0



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
