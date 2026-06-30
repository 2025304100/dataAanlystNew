from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import logging
from threading import Event, Lock
import os
import time

import akshare as ak
import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.services.akshare_utils import quiet_akshare_output
from app.models.daily_bar import DailyBar
from app.models.scan import ScanResult
from app.models.symbol import Symbol
from app.models.watchlist import WatchlistItem
from app.services.allocation import get_active_rule, get_default_portfolio
from app.services.analysis import calculate_symbol_score
from app.services.regions import region_from_market
from app.services.scans import run_scan
from app.services.symbol_names import refresh_symbol_name
from app.services.trade_plans import upsert_trade_setup
from app.db.session import get_session_local
from app.schemas.async_task import AsyncTaskRead
from app.services.async_tasks import _set_task, _start_worker, create_async_task, list_async_tasks


PROXY_KEYS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]


@contextmanager
def _proxy_bypass():
    previous = {key: os.environ.pop(key, None) for key in PROXY_KEYS}
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


def _resolve_sync_symbols(
    db: Session,
    scope: str,
    watchlist_id: int | None,
    symbol_ids: list[int] | None,
    asset_types: list[str] | None,
) -> list[Symbol]:
    stmt = select(Symbol).where(Symbol.is_active == 1)
    if asset_types:
        stmt = stmt.where(Symbol.asset_type.in_(asset_types))

    if scope == "symbols":
        if not symbol_ids:
            raise ValueError("symbol_ids is required when scope='symbols'")
        stmt = stmt.where(Symbol.id.in_(symbol_ids))
        return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()

    if scope == "watchlist":
        if watchlist_id is None:
            raise ValueError("watchlist_id is required when scope='watchlist'")
        stmt = stmt.join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id).where(WatchlistItem.watchlist_id == watchlist_id)
        return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()

    return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()


def _cn_prefixed_symbol(symbol: Symbol) -> str:
    market = (symbol.market or "").lower()
    prefix = "sh" if market in {"sh", "cn"} or symbol.symbol.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{symbol.symbol}"


def _standard_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    expected = ["trade_date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    normalized = frame.copy()
    for column in expected:
        if column not in normalized.columns:
            normalized[column] = None
    normalized = normalized[expected]
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    for column in ["open", "high", "low", "close", "volume", "amount", "turnover_rate"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized.dropna(subset=["trade_date", "open", "high", "low", "close"]).reset_index(drop=True)


def _normalize_cn_em_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "\u65e5\u671f": "trade_date",
            "\u5f00\u76d8": "open",
            "\u6700\u9ad8": "high",
            "\u6700\u4f4e": "low",
            "\u6536\u76d8": "close",
            "\u6210\u4ea4\u91cf": "volume",
            "\u6210\u4ea4\u989d": "amount",
            "\u6362\u624b\u7387": "turnover_rate",
        }
    )
    return _standard_history_frame(normalized)


def _normalize_us_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    normalized["amount"] = None
    normalized["turnover_rate"] = None
    return _standard_history_frame(normalized)


def _normalize_cn_stock_sina_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
            "turnover": "turnover_rate",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    return _standard_history_frame(normalized)


def _normalize_cn_stock_tx_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "amount": "amount",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    normalized["volume"] = None
    normalized["turnover_rate"] = None
    return _standard_history_frame(normalized)


def _normalize_cn_etf_sina_history(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    normalized = frame.rename(
        columns={
            "date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "amount": "amount",
        }
    ).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized = normalized[
        (normalized["trade_date"] >= pd.Timestamp(start_date)) & (normalized["trade_date"] <= pd.Timestamp(end_date))
    ]
    normalized["turnover_rate"] = None
    return _standard_history_frame(normalized)


def _fetch_history(symbol: Symbol, start_date: date, end_date: date, adjust: str) -> pd.DataFrame:
    start = start_date.strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")
    if symbol.asset_type not in {"stock", "etf"}:
        raise ValueError(f"Unsupported asset_type for market sync: {symbol.asset_type}")
    region = region_from_market(symbol.market)

    last_error = None
    for attempt in range(3):
        try:
            with _proxy_bypass():
                with quiet_akshare_output():
                    if region == "cn" and symbol.asset_type == "stock":
                        try:
                            return _normalize_cn_em_history(
                                ak.stock_zh_a_hist(
                                    symbol=symbol.symbol,
                                    period="daily",
                                    start_date=start,
                                    end_date=end,
                                    adjust=adjust,
                                )
                            )
                        except Exception:
                            logger.debug("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
                            try:
                                return _normalize_cn_stock_sina_history(
                                    ak.stock_zh_a_daily(
                                        symbol=_cn_prefixed_symbol(symbol),
                                        start_date=start,
                                        end_date=end,
                                        adjust=adjust,
                                    ),
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                            except Exception:
                                logger.debug("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
                                return _normalize_cn_stock_tx_history(
                                    ak.stock_zh_a_hist_tx(
                                        symbol=_cn_prefixed_symbol(symbol),
                                        start_date=start,
                                        end_date=end,
                                        adjust=adjust,
                                    ),
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                    if region == "cn" and symbol.asset_type == "etf":
                        try:
                            return _normalize_cn_em_history(
                                ak.fund_etf_hist_em(
                                    symbol=symbol.symbol,
                                    period="daily",
                                    start_date=start,
                                    end_date=end,
                                    adjust=adjust,
                                )
                            )
                        except Exception:
                            logger.debug("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
                            return _normalize_cn_etf_sina_history(
                                ak.fund_etf_hist_sina(symbol=_cn_prefixed_symbol(symbol)),
                                start_date=start_date,
                                end_date=end_date,
                            )
                    if region == "us":
                        us_adjust = adjust if adjust in {"", "qfq"} else ""
                        frame = ak.stock_us_daily(symbol=symbol.symbol, adjust=us_adjust)
                        return _normalize_us_history(frame=frame, start_date=start_date, end_date=end_date)
        except Exception as exc:
            last_error = exc
            logger.debug("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
            if attempt < 2:
                time.sleep(1 + attempt)
            continue
    if last_error is not None:
        logger.warning("All AKShare sources failed for %s", symbol.symbol, exc_info=True)
        raise last_error
    raise RuntimeError("AKShare fetch failed without an explicit exception")


def _normalize_trade_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.to_datetime(value).date()


def _upsert_bars(db: Session, symbol: Symbol, frame: pd.DataFrame) -> tuple[int, int]:
    inserted = 0
    updated = 0

    for row in frame.to_dict(orient="records"):
        trade_date = _normalize_trade_date(row["trade_date"])
        existing = db.execute(
            select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date == trade_date)
        ).scalars().first()
        if existing is None:
            existing = DailyBar(symbol_id=symbol.id, trade_date=trade_date)
            db.add(existing)
            inserted += 1
        else:
            updated += 1

        existing.open = float(row["open"])
        existing.high = float(row["high"])
        existing.low = float(row["low"])
        existing.close = float(row["close"])
        existing.volume = float(row["volume"]) if row.get("volume") is not None and not pd.isna(row.get("volume")) else None
        existing.amount = float(row["amount"]) if row.get("amount") is not None and not pd.isna(row.get("amount")) else None
        existing.turnover_rate = (
            float(row["turnover_rate"])
            if row.get("turnover_rate") is not None and not pd.isna(row.get("turnover_rate"))
            else None
        )
        existing.source = "akshare"

    return inserted, updated


def sync_symbol_daily_bars(
    db: Session,
    symbol: Symbol,
    start_date: date | None = None,
    end_date: date | None = None,
    adjust: str = "qfq",
) -> dict:
    latest_bar = db.execute(
        select(DailyBar).where(DailyBar.symbol_id == symbol.id).order_by(DailyBar.trade_date.desc())
    ).scalars().first()

    resolved_end = end_date or date.today()
    if start_date is not None:
        resolved_start = start_date
    elif latest_bar is not None:
        resolved_start = latest_bar.trade_date - timedelta(days=10)
    else:
        resolved_start = resolved_end - timedelta(days=365)

    frame = _fetch_history(symbol=symbol, start_date=resolved_start, end_date=resolved_end, adjust=adjust)
    if frame.empty:
        return {
            "symbol_id": symbol.id,
            "symbol": symbol.symbol,
            "asset_type": symbol.asset_type,
            "status": "empty",
            "inserted": 0,
            "updated": 0,
            "rows": 0,
            "start_date": resolved_start.isoformat() if hasattr(resolved_start, 'isoformat') else str(resolved_start),
            "end_date": resolved_end.isoformat() if hasattr(resolved_end, 'isoformat') else str(resolved_end),
        }

    inserted, updated = _upsert_bars(db=db, symbol=symbol, frame=frame)
    db.flush()
    return {
        "symbol_id": symbol.id,
        "symbol": symbol.symbol,
        "asset_type": symbol.asset_type,
        "status": "ok",
        "inserted": inserted,
        "updated": updated,
        "rows": len(frame),
        "start_date": resolved_start.isoformat() if hasattr(resolved_start, 'isoformat') else str(resolved_start),
        "end_date": resolved_end.isoformat() if hasattr(resolved_end, 'isoformat') else str(resolved_end),
    }


def sync_market_data(
    db: Session,
    scope: str,
    watchlist_id: int | None,
    symbol_ids: list[int] | None,
    asset_types: list[str] | None,
    start_date: date | None,
    end_date: date | None,
    adjust: str,
    auto_scan: bool,
    portfolio_id: int | None,
    portfolio_rule_id: int | None,
) -> dict:
    symbols = _resolve_sync_symbols(
        db=db,
        scope=scope,
        watchlist_id=watchlist_id,
        symbol_ids=symbol_ids,
        asset_types=asset_types,
    )

    results = []
    scored_count = 0
    synced_symbol_ids: list[int] = []
    resolved_portfolio_id = portfolio_id
    resolved_portfolio_rule_id = portfolio_rule_id
    if resolved_portfolio_id is None:
        default_portfolio = get_default_portfolio(db)
        if default_portfolio is not None:
            resolved_portfolio_id = default_portfolio.id
    if resolved_portfolio_id is not None and resolved_portfolio_rule_id is None:
        active_rule = get_active_rule(db, resolved_portfolio_id)
        if active_rule is not None:
            resolved_portfolio_rule_id = active_rule.id

    for symbol in symbols:
        try:
            refresh_symbol_name(symbol)
            result = sync_symbol_daily_bars(
                db=db,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            if result["status"] == "ok":
                latest_bar = db.execute(
                    select(DailyBar)
                    .where(DailyBar.symbol_id == symbol.id)
                    .order_by(DailyBar.trade_date.desc())
                ).scalars().first()
                if latest_bar is not None:
                    score = calculate_symbol_score(db=db, symbol=symbol, trade_date=latest_bar.trade_date)
                    result["score_refreshed"] = True
                    result["latest_score"] = {
                        "trade_date": latest_bar.trade_date.isoformat() if hasattr(latest_bar.trade_date, 'isoformat') else str(latest_bar.trade_date),
                        "quality_score": score.quality_score,
                        "timing_score": score.timing_score,
                        "stage": score.stage,
                        "action": score.action,
                    }
                    scored_count += 1
                    if resolved_portfolio_id is not None:
                        upsert_trade_setup(
                            db=db,
                            portfolio_id=resolved_portfolio_id,
                            symbol=symbol,
                            score=score,
                        )
                synced_symbol_ids.append(symbol.id)
        except Exception as exc:
            result = {
                "symbol_id": symbol.id,
                "symbol": symbol.symbol,
                "asset_type": symbol.asset_type,
                "status": "failed",
                "error": str(exc),
            }
        results.append(result)

    auto_scan_result = None
    if auto_scan and synced_symbol_ids:
        scope_snapshot = {
            "asset_types": asset_types,
            "symbol_ids": synced_symbol_ids,
        }
        if scope == "watchlist" and watchlist_id is not None:
            scope_snapshot["watchlist_id"] = watchlist_id
        scan_run = run_scan(
            db=db,
            scope_snapshot=scope_snapshot,
            filters_snapshot={
                "source": "market-data.update",
                "start_date": start_date.isoformat() if start_date and hasattr(start_date, 'isoformat') else str(start_date) if start_date else None,
                "end_date": end_date.isoformat() if end_date and hasattr(end_date, 'isoformat') else str(end_date) if end_date else None,
            },
            portfolio_id=resolved_portfolio_id,
            portfolio_rule_id=resolved_portfolio_rule_id,
            run_name="post-sync-auto-scan",
            preset_id=None,
        )
        executable_count = db.execute(
            select(ScanResult).where(
                ScanResult.scan_run_id == scan_run.id,
                ScanResult.result_type == "executable",
            )
        ).scalars().all()
        auto_scan_result = {
            "triggered": True,
            "scan_run_id": scan_run.id,
            "portfolio_id": resolved_portfolio_id,
            "portfolio_rule_id": resolved_portfolio_rule_id,
            "executable_count": len(executable_count),
        }
    elif auto_scan:
        auto_scan_result = {
            "triggered": False,
            "reason": "no_symbols_synced",
        }

    db.commit()

    return {
        "scope": scope,
        "symbols_total": len(symbols),
        "ok_count": sum(1 for item in results if item["status"] == "ok"),
        "failed_count": sum(1 for item in results if item["status"] == "failed"),
        "empty_count": sum(1 for item in results if item["status"] == "empty"),
        "scored_count": scored_count,
        "auto_scan": auto_scan_result,
        "results": results,
    }

HISTORY_INIT_PRESET_DAYS = {
    "1m": 30,
    "1q": 90,
    "1y": 365,
    "3y": 365 * 3,
}
HISTORY_INIT_STAGE_KEYS = ("prepare", "sync_bars", "calc_scores", "finalize")
HISTORY_INIT_TASK_TYPE = "history_initialization"
HISTORY_INIT_RECENT_LIMIT = 8
_HISTORY_INIT_LOCK = Lock()
_HISTORY_INIT_CANCEL_EVENT: Event = Event()
_HISTORY_INIT_TASK: dict = {
    "task_id": None,
    "status": "idle",
    "preset": "1y",
    "adjust": "qfq",
    "start_date": None,
    "end_date": None,
    "progress_pct": 0,
    "message": None,
    "started_at": None,
    "finished_at": None,
    "duration_seconds": None,
    "stages": [],
    "summary": {
        "symbols_total": 0,
        "sync_ok_count": 0,
        "sync_failed_count": 0,
        "empty_count": 0,
        "bars_rows": 0,
        "score_days_total": 0,
        "score_days_completed": 0,
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _history_db_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _history_datetime_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _build_history_stage(key: str) -> dict:
    return {
        "key": key,
        "status": "pending",
        "percent": 0,
        "done": 0,
        "total": 0,
        "message": None,
    }


def _history_duration_seconds(started_at: str | None, finished_at: str | None = None) -> int | None:
    if not started_at:
        return None
    try:
        started_dt = datetime.fromisoformat(started_at)
        finished_dt = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
    except ValueError:
        return None
    return max(0, int((finished_dt - started_dt).total_seconds()))


def _history_task_result_payload(task: dict) -> dict:
    payload = deepcopy(task)
    payload["duration_seconds"] = _history_duration_seconds(
        payload.get("started_at"),
        payload.get("finished_at") if payload.get("status") != "running" else None,
    )
    payload.pop("recent_runs", None)
    return payload


def _history_status_to_async_status(status: str | None) -> str:
    if status == "completed":
        return "done"
    if status == "failed":
        return "failed"
    if status == "running":
        return "running"
    return "queued"


def _history_active_stage(task: dict) -> dict | None:
    stages = task.get("stages") or []
    for stage in stages:
        if stage.get("status") == "running":
            return stage
    for stage in reversed(stages):
        if stage.get("status") in {"failed", "completed"}:
            return stage
    return stages[0] if stages else None


def _history_async_stage(task: dict) -> str:
    status = task.get("status")
    if status == "completed":
        return "done"
    if status == "failed":
        failed_stage = _history_active_stage(task)
        return (failed_stage or {}).get("key") or "failed"
    active_stage = _history_active_stage(task)
    return (active_stage or {}).get("key") or status or "queued"


def _persist_history_task_snapshot(task: dict) -> None:
    task_id = task.get("task_id")
    if not task_id:
        return

    snapshot = _history_task_result_payload(task)
    active_stage = _history_active_stage(snapshot) or {}
    summary = snapshot.get("summary") or {}

    try:
        SessionLocal = get_session_local()
    except RuntimeError:
        return

    db = SessionLocal()
    try:
        _set_task(
            db,
            task_id,
            status=_history_status_to_async_status(snapshot.get("status")),
            stage=_history_async_stage(snapshot),
            percent=float(snapshot.get("progress_pct") or 0),
            message=snapshot.get("message") or "",
            total=int(active_stage.get("total") or 0),
            processed=int(active_stage.get("done") or 0),
            ok_count=int(summary.get("sync_ok_count") or 0),
            failed_count=int(summary.get("sync_failed_count") or 0),
            current_item=None,
            started_at=_history_db_datetime(snapshot.get("started_at")),
            finished_at=_history_db_datetime(snapshot.get("finished_at")),
            result_json=json.dumps(snapshot, ensure_ascii=False, default=str),
        )
    except ValueError:
        logger.warning("History initialization async task missing: %s", task_id)
    finally:
        db.close()


def _history_run_record(task: AsyncTaskRead) -> dict:
    snapshot = deepcopy(task.result or {})
    status = snapshot.get("status")
    if not status:
        if task.status == "done":
            status = "completed"
        elif task.status in {"running", "failed"}:
            status = task.status
        else:
            status = "idle"

    started_at = snapshot.get("started_at") or _history_datetime_iso(task.started_at)
    finished_at = snapshot.get("finished_at") or _history_datetime_iso(task.finished_at)
    return {
        "task_id": snapshot.get("task_id") or task.id,
        "status": status,
        "preset": snapshot.get("preset") or "1y",
        "adjust": snapshot.get("adjust") or "qfq",
        "start_date": snapshot.get("start_date"),
        "end_date": snapshot.get("end_date"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": snapshot.get("duration_seconds")
        or _history_duration_seconds(started_at, finished_at),
        "message": snapshot.get("message") or task.message,
        "summary": deepcopy(snapshot.get("summary") or {}),
    }


def _history_recent_runs(limit: int = HISTORY_INIT_RECENT_LIMIT) -> list[dict]:
    try:
        tasks = list_async_tasks(task_type=HISTORY_INIT_TASK_TYPE, limit=limit)
    except RuntimeError:
        return []
    return [_history_run_record(task) for task in tasks]


def _clone_history_task() -> dict:
    with _HISTORY_INIT_LOCK:
        payload = deepcopy(_HISTORY_INIT_TASK)
    payload["duration_seconds"] = _history_duration_seconds(
        payload.get("started_at"),
        payload.get("finished_at") if payload.get("status") != "running" else None,
    )
    payload["recent_runs"] = _history_recent_runs()
    return payload

def _set_history_task(task: dict) -> dict:
    global _HISTORY_INIT_TASK
    with _HISTORY_INIT_LOCK:
        _HISTORY_INIT_TASK = task
        payload = deepcopy(_HISTORY_INIT_TASK)
    payload["duration_seconds"] = _history_duration_seconds(
        payload.get("started_at"),
        payload.get("finished_at") if payload.get("status") != "running" else None,
    )
    _persist_history_task_snapshot(payload)
    payload["recent_runs"] = _history_recent_runs()
    return payload


def _mutate_history_task(task_id: str | None, mutator) -> dict:
    global _HISTORY_INIT_TASK
    with _HISTORY_INIT_LOCK:
        if task_id is not None and _HISTORY_INIT_TASK.get("task_id") != task_id:
            payload = deepcopy(_HISTORY_INIT_TASK)
        else:
            mutator(_HISTORY_INIT_TASK)
            payload = deepcopy(_HISTORY_INIT_TASK)
    payload["duration_seconds"] = _history_duration_seconds(
        payload.get("started_at"),
        payload.get("finished_at") if payload.get("status") != "running" else None,
    )
    if task_id is None or _HISTORY_INIT_TASK.get("task_id") == task_id:
        _persist_history_task_snapshot(payload)
    payload["recent_runs"] = _history_recent_runs()
    return payload


def _history_stage(task: dict, key: str) -> dict | None:
    for stage in task.get("stages", []):
        if stage.get("key") == key:
            return stage
    return None


def _recompute_history_progress(task: dict) -> None:
    stages = task.get("stages") or []
    if not stages:
        task["progress_pct"] = 0
        return
    task["progress_pct"] = round(sum(int(stage.get("percent", 0)) for stage in stages) / len(stages))


def get_history_initialization_status() -> dict:
    return _clone_history_task()

def start_history_initialization_task(
    preset: str = "1y",
    adjust: str = "qfq",
    asset_types: list[str] | None = None,
) -> dict:
    preset_key = preset if preset in HISTORY_INIT_PRESET_DAYS else "1y"
    resolved_end = date.today()
    resolved_start = resolved_end - timedelta(days=HISTORY_INIT_PRESET_DAYS[preset_key])

    with _HISTORY_INIT_LOCK:
        if _HISTORY_INIT_TASK.get("status") == "running":
            raise ValueError("A history initialization task is already running")
        _HISTORY_INIT_CANCEL_EVENT.clear()

    task = {
        "status": "running",
        "preset": preset_key,
        "adjust": adjust,
        "asset_types": asset_types or ["stock", "etf"],
        "start_date": resolved_start,
        "end_date": resolved_end,
        "progress_pct": 0,
        "message": "Task created. Preparing symbol universe.",
        "started_at": _now_iso(),
        "finished_at": None,
        "stages": [_build_history_stage(key) for key in HISTORY_INIT_STAGE_KEYS],
        "summary": {
            "symbols_total": 0,
            "sync_ok_count": 0,
            "sync_failed_count": 0,
            "empty_count": 0,
            "bars_rows": 0,
            "score_days_total": 0,
            "score_days_completed": 0,
        },
    }
    prepare_stage = _history_stage(task, "prepare")
    if prepare_stage is not None:
        prepare_stage.update({"status": "running", "total": 1, "message": "Collecting symbols"})
    task_read = create_async_task(
        HISTORY_INIT_TASK_TYPE,
        {
            "preset": preset_key,
            "adjust": adjust,
            "asset_types": asset_types or ["stock", "etf"],
            "start_date": resolved_start.isoformat(),
            "end_date": resolved_end.isoformat(),
        },
    )
    task["task_id"] = task_read.id
    return _set_history_task(task)


def create_history_initialization_task(
    preset: str = "1y",
    adjust: str = "qfq",
    asset_types: list[str] | None = None,
) -> dict:
    task = start_history_initialization_task(preset=preset, adjust=adjust, asset_types=asset_types)
    _start_worker(task["task_id"], run_history_initialization_task)
    return task


def _update_history_stage(
    task_id: str,
    key: str,
    *,
    status: str | None = None,
    done: int | None = None,
    total: int | None = None,
    message: str | None = None,
) -> dict:
    def _apply(task: dict) -> None:
        stage = _history_stage(task, key)
        if stage is None:
            return
        if status is not None:
            stage["status"] = status
        if total is not None:
            stage["total"] = total
        if done is not None:
            stage["done"] = done
        if stage.get("total", 0) > 0:
            stage["percent"] = max(0, min(100, round(stage.get("done", 0) * 100 / stage["total"])))
        elif stage.get("status") == "completed":
            stage["percent"] = 100
        else:
            stage["percent"] = 0
        if message is not None:
            stage["message"] = message
            task["message"] = message
        _recompute_history_progress(task)

    return _mutate_history_task(task_id, _apply)


def _complete_history_task(task_id: str, message: str) -> dict:
    def _apply(task: dict) -> None:
        task["status"] = "completed"
        task["message"] = message
        task["finished_at"] = _now_iso()
        finalize_stage = _history_stage(task, "finalize")
        if finalize_stage is not None:
            finalize_stage.update({
                "status": "completed",
                "done": 1,
                "total": 1,
                "percent": 100,
                "message": message,
            })
        _recompute_history_progress(task)
        task["progress_pct"] = 100

    return _mutate_history_task(task_id, _apply)


def _fail_history_task(task_id: str, message: str) -> dict:
    def _apply(task: dict) -> None:
        task["status"] = "failed"
        task["message"] = message
        task["finished_at"] = _now_iso()
        for key in reversed(HISTORY_INIT_STAGE_KEYS):
            stage = _history_stage(task, key)
            if stage is not None and stage.get("status") == "running":
                stage["status"] = "failed"
                stage["message"] = message
                break
        _recompute_history_progress(task)

    return _mutate_history_task(task_id, _apply)


def run_history_initialization_task(task_id: str) -> None:
    snapshot = get_history_initialization_status()
    if snapshot.get("task_id") != task_id:
        return

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        start_date = snapshot.get("start_date")
        end_date = snapshot.get("end_date")
        adjust = snapshot.get("adjust") or "qfq"
        asset_types = snapshot.get("asset_types") or ["stock", "etf"]

        symbols = db.execute(
            select(Symbol)
            .where(Symbol.is_active == 1, Symbol.asset_type.in_(asset_types))
            .order_by(Symbol.id.asc())
        ).scalars().all()

        _update_history_stage(
            task_id,
            "prepare",
            status="completed",
            done=1,
            total=1,
            message=f"Universe ready: {len(symbols)} symbols",
        )

        def _set_symbol_total(task: dict) -> None:
            task["summary"]["symbols_total"] = len(symbols)
        _mutate_history_task(task_id, _set_symbol_total)

        sync_total = len(symbols)
        _update_history_stage(task_id, "sync_bars", status="running", done=0, total=sync_total, message="Syncing historical bars")

        for index, symbol in enumerate(symbols, start=1):
            if _HISTORY_INIT_CANCEL_EVENT.is_set():
                _fail_history_task(task_id, "Task cancelled by user")
                return
            try:
                refresh_symbol_name(symbol)
                result = sync_symbol_daily_bars(
                    db=db,
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                )
                db.commit()
            except Exception as exc:
                db.rollback()
                logger.warning("History init sync failed for %s", symbol.symbol, exc_info=True)
                result = {
                    "status": "failed",
                    "rows": 0,
                    "error": str(exc),
                }

            def _sync_summary(task: dict, sync_result: dict = result) -> None:
                summary = task["summary"]
                summary["bars_rows"] += int(sync_result.get("rows") or 0)
                if sync_result.get("status") == "ok":
                    summary["sync_ok_count"] += 1
                elif sync_result.get("status") == "empty":
                    summary["empty_count"] += 1
                else:
                    summary["sync_failed_count"] += 1
            _mutate_history_task(task_id, _sync_summary)
            _update_history_stage(
                task_id,
                "sync_bars",
                done=index,
                total=sync_total,
                message=f"Syncing bars {index}/{sync_total}: {symbol.symbol}",
            )

        _update_history_stage(
            task_id,
            "sync_bars",
            status="completed",
            done=sync_total,
            total=sync_total,
            message="Bar sync complete. Calculating scores.",
        )

        symbol_id_list = [symbol.id for symbol in symbols]
        if symbol_id_list:
            bar_counts = {
                int(symbol_id): int(total)
                for symbol_id, total in db.execute(
                    select(DailyBar.symbol_id, func.count(DailyBar.id))
                    .where(
                        DailyBar.trade_date >= start_date,
                        DailyBar.trade_date <= end_date,
                        DailyBar.symbol_id.in_(symbol_id_list),
                    )
                    .group_by(DailyBar.symbol_id)
                ).all()
            }
        else:
            bar_counts = {}
        score_total = sum(bar_counts.values())

        def _set_score_total(task: dict) -> None:
            task["summary"]["score_days_total"] = score_total
        _mutate_history_task(task_id, _set_score_total)

        _update_history_stage(
            task_id,
            "calc_scores",
            status="running",
            done=0,
            total=score_total,
            message="Calculating historical scores",
        )

        score_done = 0
        for symbol in symbols:
            if _HISTORY_INIT_CANCEL_EVENT.is_set():
                _fail_history_task(task_id, "Task cancelled by user")
                return
            trade_dates = db.execute(
                select(DailyBar.trade_date)
                .where(
                    DailyBar.symbol_id == symbol.id,
                    DailyBar.trade_date >= start_date,
                    DailyBar.trade_date <= end_date,
                )
                .order_by(DailyBar.trade_date.asc())
            ).scalars().all()
            if not trade_dates:
                continue
            symbol_failed = False
            for trade_date in trade_dates:
                try:
                    calculate_symbol_score(db=db, symbol=symbol, trade_date=trade_date)
                    score_done += 1
                except Exception as exc:
                    db.rollback()
                    logger.warning("History init score calc failed for %s on %s: %s", symbol.symbol, trade_date, exc)
                    symbol_failed = True
            try:
                db.commit()
            except Exception:
                db.rollback()

            def _score_summary(task: dict) -> None:
                task["summary"]["score_days_completed"] = score_done
            _mutate_history_task(task_id, _score_summary)
            _update_history_stage(
                task_id,
                "calc_scores",
                done=score_done,
                total=score_total,
                message=f"Calculating scores {score_done}/{score_total}: {symbol.symbol}",
            )

        _update_history_stage(
            task_id,
            "calc_scores",
            status="completed",
            done=score_done,
            total=score_total,
            message="Score calculation complete. Finalizing summary.",
        )
        _update_history_stage(task_id, "finalize", status="running", done=0, total=1, message="Finalizing initialization summary")
        summary = get_history_initialization_status().get("summary", {})
        _complete_history_task(
            task_id,
            f"Initialization complete: {summary.get('sync_ok_count', 0)} symbols synced, {summary.get('score_days_completed', 0)} score days refreshed.",
        )
    except Exception as exc:
        db.rollback()
        logger.exception("History initialization task failed")
        _fail_history_task(task_id, f"Initialization failed: {exc}")
    finally:
        db.close()


def cancel_history_initialization_task() -> dict:
    """取消当前正在运行的历史初始化任务。"""
    from app.services.async_tasks import cancel_async_task

    with _HISTORY_INIT_LOCK:
        task = _HISTORY_INIT_TASK
        if task.get("status") != "running":
            raise ValueError("No running history initialization task to cancel")
        task_id = task.get("task_id")
        _HISTORY_INIT_CANCEL_EVENT.set()

    if task_id:
        try:
            cancel_async_task(task_id)
        except ValueError:
            logger.warning("Async task %s not found for cancel", task_id)

    def _apply(task: dict) -> None:
        task["status"] = "failed"
        task["message"] = "Task cancelled by user"
        task["finished_at"] = _now_iso()
        for key in reversed(HISTORY_INIT_STAGE_KEYS):
            stage = _history_stage(task, key)
            if stage is not None and stage.get("status") == "running":
                stage["status"] = "failed"
                stage["message"] = "Cancelled"
                break
        _recompute_history_progress(task)

    return _mutate_history_task(task_id, _apply)


def cleanup_history_records(keep: int = 5) -> dict:
    """清理历史初始化记录，仅保留最近 keep 条。"""
    from app.models.async_task import AsyncTaskRecord
    from sqlalchemy import delete

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        stmt = (
            select(AsyncTaskRecord.id)
            .where(AsyncTaskRecord.task_type == HISTORY_INIT_TASK_TYPE)
            .order_by(desc(AsyncTaskRecord.created_at))
        )
        all_ids = db.execute(stmt).scalars().all()

        if len(all_ids) <= keep:
            return {"deleted_count": 0, "kept": len(all_ids), "total": len(all_ids)}

        ids_to_delete = all_ids[keep:]
        db.execute(
            delete(AsyncTaskRecord).where(AsyncTaskRecord.id.in_(ids_to_delete))
        )
        db.commit()
        return {"deleted_count": len(ids_to_delete), "kept": keep, "total": len(all_ids)}
    finally:
        db.close()



