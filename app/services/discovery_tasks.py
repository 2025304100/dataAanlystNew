from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import threading
import time
import traceback
import uuid
from typing import Any

import akshare as ak
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.daily_bar import DailyBar
from app.models.discovery import DiscoveryTaskRecord
from app.models.scan import ScanResult
from app.models.symbol import Symbol
from app.schemas.discovery import DiscoveryTaskCreate
from app.services.akshare_utils import quiet_akshare_output
from app.schemas.news import NewsUpdateRequest
from app.services.allocation import get_active_rule, get_default_portfolio
from app.services.analysis import calculate_symbol_score
from app.services.market_data import sync_symbol_daily_bars
from app.services.news import update_news
from app.services.regions import markets_for_region
from app.services.scans import run_scan
from app.services.symbol_names import refresh_symbol_name
from app.services.trade_plans import upsert_trade_setup


TASK_STAGE_PERCENT = {
    "queued": 0,
    "prepare": 8,
    "sync": 72,
    "scan": 86,
    "news": 94,
    "done": 100,
    "paused": 100,
    "cancelled": 100,
    "failed": 100,
}

DISCOVERY_SCOPE_CONFIG = {
    "cn-stock": {"region": "cn", "asset_type": "stock"},
    "cn-etf": {"region": "cn", "asset_type": "etf"},
    "us-stock": {"region": "us", "asset_type": "stock"},
    "us-etf": {"region": "us", "asset_type": "etf"},
}

RESUME_DEADLINE = timedelta(days=1)
STALE_RUNNING_DEADLINE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _task_to_dict(task: DiscoveryTaskRecord) -> dict:
    return {
        "id": task.id,
        "status": task.status,
        "stage": task.stage,
        "percent": task.percent,
        "message": task.message,
        "scope": task.scope,
        "min_score": task.min_score,
        "include_news": bool(task.include_news),
        "total": task.total,
        "processed": task.processed,
        "ok_count": task.ok_count,
        "failed_count": task.failed_count,
        "empty_count": task.empty_count,
        "scored_count": task.scored_count,
        "current_symbol": task.current_symbol,
        "batch_size": task.batch_size,
        "delay_seconds": task.delay_seconds,
        "adaptive_delay_seconds": task.adaptive_delay_seconds,
        "symbol_limit": task.symbol_limit,
        "scan_run_id": task.scan_run_id,
        "executable_count": task.executable_count,
        "news_symbols_total": task.news_symbols_total,
        "errors": _json_loads(task.errors_json, [])[-20:],
        "created_at": task.created_at,
        "started_at": task.started_at,
        "paused_at": task.paused_at,
        "cancelled_at": task.cancelled_at,
        "finished_at": task.finished_at,
        "can_resume": task.status == "paused" and task.paused_at is not None and _now() - task.paused_at <= RESUME_DEADLINE,
    }


def _set_task(db: Session, task_id: str, **updates) -> DiscoveryTaskRecord:
    task = db.get(DiscoveryTaskRecord, task_id)
    if task is None:
        raise ValueError("Discovery task not found")
    for key, value in updates.items():
        setattr(task, key, value)
    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return task


def _append_error(task: DiscoveryTaskRecord, error: dict) -> None:
    errors = _json_loads(task.errors_json, [])
    errors.append(error)
    task.errors_json = json.dumps(errors[-20:], ensure_ascii=False, default=str)


def list_discovery_tasks(limit: int = 20) -> list[dict]:
    db = SessionLocal()
    try:
        _expire_stale_tasks(db)
        rows = (
            db.execute(select(DiscoveryTaskRecord).order_by(desc(DiscoveryTaskRecord.created_at)).limit(limit))
            .scalars()
            .all()
        )
        return [_task_to_dict(item) for item in rows]
    finally:
        db.close()


def get_discovery_task(task_id: str) -> dict | None:
    db = SessionLocal()
    try:
        _expire_stale_tasks(db)
        task = db.get(DiscoveryTaskRecord, task_id)
        return _task_to_dict(task) if task is not None else None
    finally:
        db.close()


def _expire_stale_tasks(db: Session) -> None:
    cutoff = _now() - STALE_RUNNING_DEADLINE
    rows = (
        db.execute(
            select(DiscoveryTaskRecord).where(
                DiscoveryTaskRecord.status.in_(("queued", "running")),
                DiscoveryTaskRecord.updated_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    for task in rows:
        task.status = "failed"
        task.stage = "failed"
        task.percent = 100
        task.message = "任务长时间无进度，已自动中断，请重新开始"
        task.finished_at = _now()
        _append_error(task, {"scope": "task", "error": "stale task auto-expired"})
    db.commit()


def create_discovery_task(payload: DiscoveryTaskCreate) -> dict:
    task_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        task = DiscoveryTaskRecord(
            id=task_id,
            status="queued",
            stage="queued",
            percent=0,
            message="任务已创建，等待后台开始",
            scope=payload.scope,
            min_score=payload.min_score,
            include_news=int(payload.include_news),
            batch_size=payload.batch_size,
            delay_seconds=payload.delay_seconds,
            adaptive_delay_seconds=payload.delay_seconds,
            symbol_limit=payload.symbol_limit,
            payload_json=payload.model_dump_json(),
            processed_symbol_ids_json="[]",
            synced_symbol_ids_json="[]",
            errors_json="[]",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        _start_worker(task_id)
        return _task_to_dict(task)
    finally:
        db.close()


def pause_discovery_task(task_id: str) -> dict:
    db = SessionLocal()
    try:
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is None:
            raise ValueError("Discovery task not found")
        if task.status not in {"queued", "running"}:
            return _task_to_dict(task)
        task.status = "paused"
        task.stage = "paused"
        task.message = "任务已暂停，1天内可继续"
        task.paused_at = _now()
        task.current_symbol = None
        db.commit()
        db.refresh(task)
        return _task_to_dict(task)
    finally:
        db.close()


def cancel_discovery_task(task_id: str) -> dict:
    db = SessionLocal()
    try:
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is None:
            raise ValueError("Discovery task not found")
        if task.status in {"done", "failed", "cancelled"}:
            return _task_to_dict(task)
        task.status = "cancelled"
        task.stage = "cancelled"
        task.percent = 100
        task.message = "任务已中止"
        task.cancelled_at = _now()
        task.finished_at = _now()
        task.current_symbol = None
        db.commit()
        db.refresh(task)
        return _task_to_dict(task)
    finally:
        db.close()


def resume_discovery_task(task_id: str) -> dict:
    db = SessionLocal()
    try:
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is None:
            raise ValueError("Discovery task not found")
        if task.status != "paused":
            return _task_to_dict(task)
        if task.paused_at is None or _now() - task.paused_at > RESUME_DEADLINE:
            task.status = "expired"
            task.stage = "failed"
            task.percent = 100
            task.message = "暂停已超过1天，请重新开始"
            task.finished_at = _now()
            db.commit()
            db.refresh(task)
            return _task_to_dict(task)
        task.status = "queued"
        task.stage = "queued"
        task.message = "任务已恢复，等待后台继续"
        task.paused_at = None
        db.commit()
        db.refresh(task)
        _start_worker(task_id)
        return _task_to_dict(task)
    finally:
        db.close()


def _start_worker(task_id: str) -> None:
    worker = threading.Thread(target=_run_discovery_task, args=(task_id,), daemon=True)
    worker.start()


def _market_for_cn_stock(code: str) -> str:
    if code.startswith(("4", "8")):
        return "bj"
    if code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def _market_for_cn_fund(code: str, prefixed_code: str | None = None) -> str:
    prefixed = (prefixed_code or "").lower()
    if prefixed.startswith("sh"):
        return "sh"
    if prefixed.startswith("sz"):
        return "sz"
    return "sh" if code.startswith("5") else "sz"


def _upsert_symbol(
    db: Session,
    *,
    code: str,
    name: str,
    asset_type: str,
    market: str,
    board: str = "main",
    theme: str | None = None,
) -> bool:
    symbol_code = code.strip().upper()
    if not symbol_code:
        return False
    symbol = db.execute(select(Symbol).where(Symbol.symbol == symbol_code)).scalars().first()
    created = symbol is None
    if symbol is None:
        symbol = Symbol(
            symbol=symbol_code,
            name=name.strip() or symbol_code,
            asset_type=asset_type,
            market=market,
            board=board,
            theme=theme,
            is_active=1,
        )
        db.add(symbol)
    else:
        if name.strip():
            symbol.name = name.strip()
        symbol.asset_type = asset_type
        symbol.market = market
        symbol.board = symbol.board or board
        symbol.theme = symbol.theme or theme
        symbol.is_active = 1
    return created


def _refresh_cn_stock_universe(db: Session) -> dict:
    with quiet_akshare_output():
        frame = ak.stock_info_a_code_name()
    created = 0
    seen = 0
    for row in frame.to_dict("records"):
        raw_code = row.get("code")
        raw_name = row.get("name")
        if not raw_code or not raw_name:
            continue
        code = str(raw_code).strip().zfill(6)
        if not code.isdigit():
            continue
        seen += 1
        if _upsert_symbol(
            db,
            code=code,
            name=str(raw_name),
            asset_type="stock",
            market=_market_for_cn_stock(code),
            theme="a-share",
        ):
            created += 1
    db.flush()
    return {"seen": seen, "created": created}


def _first_row_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _refresh_cn_etf_universe(db: Session) -> dict:
    created = 0
    seen = 0
    try:
        with quiet_akshare_output():
            frame = ak.fund_etf_spot_em()
        records = frame.to_dict("records")
        code_keys = ["代码", "基金代码", "symbol", "code"]
        name_keys = ["名称", "基金简称", "name"]
    except Exception:
        with quiet_akshare_output():
            frame = ak.fund_etf_category_sina()
        records = frame.to_dict("records")
        code_keys = ["代码", "symbol", "code"]
        name_keys = ["名称", "name"]

    for row in records:
        raw_code = _first_row_value(row, code_keys)
        raw_name = _first_row_value(row, name_keys)
        if not raw_code or not raw_name:
            continue
        prefixed = str(raw_code).strip().lower()
        code = prefixed.replace("sh", "").replace("sz", "").zfill(6)
        if not code.isdigit():
            continue
        seen += 1
        if _upsert_symbol(
            db,
            code=code,
            name=str(raw_name),
            asset_type="etf",
            market=_market_for_cn_fund(code, prefixed),
            theme="cn-etf",
        ):
            created += 1
    db.flush()
    return {"seen": seen, "created": created}


def _refresh_discovery_universe(db: Session, payload: DiscoveryTaskCreate) -> dict | None:
    if not payload.refresh_universe:
        return None
    if payload.scope == "cn-stock":
        return _refresh_cn_stock_universe(db)
    if payload.scope == "cn-etf":
        return _refresh_cn_etf_universe(db)
    return None


def _resolve_discovery_symbols(db: Session, payload: DiscoveryTaskCreate) -> list[Symbol]:
    if payload.scope not in DISCOVERY_SCOPE_CONFIG:
        raise ValueError(f"Unsupported discovery scope: {payload.scope}")
    config = DISCOVERY_SCOPE_CONFIG[payload.scope]
    stmt = select(Symbol).where(Symbol.is_active == 1)
    asset_type = config.get("asset_type")
    region = config.get("region")

    if asset_type:
        stmt = stmt.where(Symbol.asset_type == asset_type)
    markets = markets_for_region(region)
    if markets:
        stmt = stmt.where(Symbol.market.in_(markets))
    if payload.use_cached_symbols_only:
        stmt = stmt.join(DailyBar, DailyBar.symbol_id == Symbol.id).distinct()
    stmt = stmt.order_by(Symbol.id.asc())
    if payload.symbol_limit is not None:
        stmt = stmt.limit(payload.symbol_limit)
    return db.execute(stmt).scalars().all()


def get_discovery_scope_stats(scope: str, db: Session) -> dict:
    if scope not in DISCOVERY_SCOPE_CONFIG:
        raise ValueError(f"Unsupported discovery scope: {scope}")
    config = DISCOVERY_SCOPE_CONFIG[scope]
    stmt = select(func.count(Symbol.id)).where(Symbol.is_active == 1)
    cached_stmt = (
        select(func.count(func.distinct(Symbol.id)))
        .select_from(Symbol)
        .join(DailyBar, DailyBar.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1)
    )
    asset_type = config.get("asset_type")
    region = config.get("region")
    if asset_type:
        stmt = stmt.where(Symbol.asset_type == asset_type)
        cached_stmt = cached_stmt.where(Symbol.asset_type == asset_type)
    markets = markets_for_region(region)
    if markets:
        stmt = stmt.where(Symbol.market.in_(markets))
        cached_stmt = cached_stmt.where(Symbol.market.in_(markets))
    total_symbols = db.execute(stmt).scalar_one()
    cached_symbols = db.execute(cached_stmt).scalar_one()
    return {
        "scope": scope,
        "total_symbols": int(total_symbols or 0),
        "cached_symbols": int(cached_symbols or 0),
        "active_symbols": int(cached_symbols or 0),
    }


def _resolve_portfolio_refs(db: Session, portfolio_id: int | None, portfolio_rule_id: int | None) -> tuple[int | None, int | None]:
    resolved_portfolio_id = portfolio_id
    resolved_rule_id = portfolio_rule_id
    if resolved_portfolio_id is None:
        default_portfolio = get_default_portfolio(db)
        if default_portfolio is not None:
            resolved_portfolio_id = default_portfolio.id
    if resolved_portfolio_id is not None and resolved_rule_id is None:
        active_rule = get_active_rule(db, resolved_portfolio_id)
        if active_rule is not None:
            resolved_rule_id = active_rule.id
    return resolved_portfolio_id, resolved_rule_id


def _latest_bar(db: Session, symbol: Symbol) -> DailyBar | None:
    return db.execute(
        select(DailyBar).where(DailyBar.symbol_id == symbol.id).order_by(DailyBar.trade_date.desc())
    ).scalars().first()


def _sync_one_symbol(
    db: Session,
    symbol: Symbol,
    payload: DiscoveryTaskCreate,
    portfolio_id: int | None,
) -> tuple[dict, bool]:
    cached_bar = _latest_bar(db, symbol)
    if payload.use_cached_bars_first and cached_bar is not None:
        score = calculate_symbol_score(db=db, symbol=symbol, trade_date=cached_bar.trade_date)
        if portfolio_id is not None:
            upsert_trade_setup(db=db, portfolio_id=portfolio_id, symbol=symbol, score=score)
        return (
            {
                "symbol_id": symbol.id,
                "symbol": symbol.symbol,
                "asset_type": symbol.asset_type,
                "status": "ok",
                "source": "cached_bars",
                "rows": 0,
                "latest_score": {
                    "trade_date": cached_bar.trade_date.isoformat() if hasattr(cached_bar.trade_date, 'isoformat') else str(cached_bar.trade_date),
                    "quality_score": score.quality_score,
                    "timing_score": score.timing_score,
                    "stage": score.stage,
                    "action": score.action,
                },
            },
            True,
        )
    refresh_symbol_name(symbol)
    result = sync_symbol_daily_bars(
        db=db,
        symbol=symbol,
        start_date=payload.start_date,
        end_date=payload.end_date,
        adjust=payload.adjust,
    )
    scored = False
    if result["status"] == "ok":
        latest_bar = _latest_bar(db, symbol)
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
            scored = True
            if portfolio_id is not None:
                upsert_trade_setup(db=db, portfolio_id=portfolio_id, symbol=symbol, score=score)
    return result, scored


def _sync_progress(processed: int, total: int) -> float:
    if total <= 0:
        return TASK_STAGE_PERCENT["prepare"]
    return round(10 + min(1, processed / total) * 62, 1)


def _payload_from_task(task: DiscoveryTaskRecord) -> DiscoveryTaskCreate:
    return DiscoveryTaskCreate.model_validate_json(task.payload_json or "{}")


def _check_stop_state(db: Session, task_id: str) -> str | None:
    db.expire_all()
    task = db.get(DiscoveryTaskRecord, task_id)
    if task is None:
        return "cancelled"
    if task.status == "paused":
        task.message = "任务已暂停，进度已保留"
        task.current_symbol = None
        db.commit()
        return "paused"
    if task.status == "cancelled":
        task.message = "任务已中止"
        task.current_symbol = None
        task.finished_at = task.finished_at or _now()
        db.commit()
        return "cancelled"
    return None


def _run_discovery_task(task_id: str) -> None:
    db = SessionLocal()
    try:
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is None:
            return
        payload = _payload_from_task(task)
        adaptive_delay = task.adaptive_delay_seconds or payload.delay_seconds
        processed_ids = set(_json_loads(task.processed_symbol_ids_json, []))
        synced_symbol_ids: list[int] = _json_loads(task.synced_symbol_ids_json, [])
        portfolio_id, portfolio_rule_id = _resolve_portfolio_refs(db, payload.portfolio_id, payload.portfolio_rule_id)

        task.status = "running"
        task.stage = "prepare"
        task.percent = TASK_STAGE_PERCENT["prepare"]
        task.message = "正在整理扫描范围"
        task.started_at = task.started_at or _now()
        db.commit()

        if not processed_ids:
            universe = _refresh_discovery_universe(db, payload)
            if universe is not None:
                db.commit()

        symbols = _resolve_discovery_symbols(db, payload)
        task = _set_task(db, task_id, total=len(symbols), message=f"已载入 {len(symbols)} 个标的")
        if not symbols:
            _set_task(db, task_id, status="done", stage="done", percent=100, message="当前范围没有可扫描标的", finished_at=_now())
            return

        for index, symbol in enumerate(symbols, start=1):
            if symbol.id in processed_ids:
                continue
            stop_state = _check_stop_state(db, task_id)
            if stop_state:
                return

            task = db.get(DiscoveryTaskRecord, task_id)
            task.stage = "sync"
            task.percent = _sync_progress(task.processed, len(symbols))
            task.current_symbol = symbol.symbol
            task.adaptive_delay_seconds = round(adaptive_delay, 2)
            task.message = f"正在同步 {symbol.symbol} ({task.processed + 1}/{len(symbols)})"
            db.commit()

            try:
                result, scored = _sync_one_symbol(db, symbol, payload, portfolio_id)
                task = db.get(DiscoveryTaskRecord, task_id)
                if result["status"] == "ok":
                    task.ok_count += 1
                    if symbol.id not in synced_symbol_ids:
                        synced_symbol_ids.append(symbol.id)
                    if scored:
                        task.scored_count += 1
                    adaptive_delay = max(payload.delay_seconds, adaptive_delay * 0.9)
                elif result["status"] == "empty":
                    task.empty_count += 1
                    adaptive_delay = max(payload.delay_seconds, adaptive_delay * 0.95)
                else:
                    task.failed_count += 1
                    adaptive_delay = min(5, max(payload.delay_seconds, adaptive_delay * 1.4 + 0.1))
                db.flush()
            except Exception as exc:
                db.rollback()
                task = db.get(DiscoveryTaskRecord, task_id)
                task.failed_count += 1
                _append_error(task, {"symbol_id": symbol.id, "symbol": symbol.symbol, "error": str(exc)})
                adaptive_delay = min(5, max(payload.delay_seconds, adaptive_delay * 1.6 + 0.2))

            processed_ids.add(symbol.id)
            task = db.get(DiscoveryTaskRecord, task_id)
            task.processed = len(processed_ids)
            task.percent = _sync_progress(task.processed, len(symbols))
            task.adaptive_delay_seconds = round(adaptive_delay, 2)
            task.processed_symbol_ids_json = json.dumps(sorted(processed_ids))
            task.synced_symbol_ids_json = json.dumps(synced_symbol_ids)
            db.commit()
            if adaptive_delay > 0:
                time.sleep(adaptive_delay)

        stop_state = _check_stop_state(db, task_id)
        if stop_state:
            return

        _set_task(db, task_id, stage="scan", percent=TASK_STAGE_PERCENT["scan"], current_symbol=None, message="正在运行候选池扫描")
        scan_run = run_scan(
            db=db,
            scope_snapshot={"symbol_ids": synced_symbol_ids},
            filters_snapshot={
                "source": "discovery.task",
                "task_id": task_id,
                "scope": payload.scope,
                "min_score": payload.min_score,
                "global_mode": payload.global_mode,
            },
            portfolio_id=portfolio_id,
            portfolio_rule_id=portfolio_rule_id,
            run_name="opportunity-discovery-task",
            preset_id=None,
        )
        executable_rows = (
            db.execute(
                select(ScanResult)
                .where(ScanResult.scan_run_id == scan_run.id, ScanResult.result_type == "executable")
                .order_by(ScanResult.priority_score.desc(), ScanResult.created_at.desc())
            )
            .scalars()
            .all()
        )
        for row in executable_rows:
            row.warning_days = payload.warning_days
            row.valid_days = payload.valid_days
            row.is_frozen = 0
        db.commit()
        _set_task(
            db,
            task_id,
            scan_run_id=scan_run.id,
            executable_count=len(executable_rows),
            message=f"扫描完成，找到 {len(executable_rows)} 个可执行候选",
        )

        if payload.include_news and payload.news_limit > 0 and executable_rows:
            _set_task(db, task_id, stage="news", percent=TASK_STAGE_PERCENT["news"], message="正在合并消息面评分")
            news_symbol_ids = [item.symbol_id for item in executable_rows[: payload.news_limit]]
            news_result = update_news(
                db,
                NewsUpdateRequest(
                    portfolio_id=portfolio_id,
                    scope="symbols",
                    symbol_ids=news_symbol_ids,
                    days=7,
                    include_macro=True,
                    include_sector=True,
                    include_symbol=True,
                ),
            )
            _set_task(db, task_id, news_symbols_total=news_result.get("symbols_total", 0))

        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            current_symbol=None,
            message="机会挖掘完成",
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is not None:
            task.status = "failed"
            task.stage = "failed"
            task.percent = 100
            task.message = f"机会挖掘失败：{exc}"
            task.finished_at = _now()
            _append_error(task, {"scope": "task", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            db.commit()
    finally:
        db.close()
