from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import time
import traceback
import uuid
from typing import Any

import akshare as ak
from sqlalchemy import desc, func, select, update
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.daily_bar import DailyBar
from app.models.discovery import DiscoveryTaskRecord
from app.models.portfolio import Position
from app.models.scan import ScanResult
from app.models.symbol import Symbol
from app.models.watchlist import WatchlistItem
from app.schemas.discovery import DiscoveryTaskCreate
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
from app.schemas.news import NewsUpdateRequest
from app.services.allocation import get_active_rule, get_default_portfolio
from app.services.analysis import calculate_symbol_score
from app.services.market_data import sync_symbol_daily_bars
from app.services.news import update_news
from app.services.regions import markets_for_region
from app.services.scans import run_scan
from app.services.symbol_names import refresh_symbol_name
from app.services.trade_plans import upsert_trade_setup


logger = logging.getLogger(__name__)


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
# 从 30 分钟降到 10 分钟：卡死任务更快被清理。
# 配合单 symbol 90s 超时 + watchdog 心跳，正常任务不会误判（watchdog 每 30s 更新 updated_at）。
STALE_RUNNING_DEADLINE = timedelta(minutes=10)

# 单 symbol 同步超时（秒）
# 覆盖 _fetch_history 3 次重试（每次最多 20s = 连接 5s + 读取 15s）+ 退避 sleep + 余量
# 超时后跳过该 symbol，记录 failed_count，继续下一个，避免单 symbol 卡死阻塞整个任务
SYNC_ONE_SYMBOL_TIMEOUT_SECONDS = 90

# universe 刷新超时（秒）
# 覆盖 _refresh_cn_stock_universe / _refresh_cn_etf_universe 的 akshare 调用
# akshare 内部 pd.read_excel 可能绕过 requests timeout 永久阻塞，需 ThreadPoolExecutor 兜底
# 超时后返回 None，调用方标记 task=failed，避免 prepare 阶段永久卡死
UNIVERSE_REFRESH_TIMEOUT_SECONDS = 120

# watchdog 心跳间隔（秒）
# 每 30s 更新 task.updated_at，防止 _expire_stale_tasks 误判正常任务为 stale
# 真正的卡死由单 symbol 超时（90s）兜底，watchdog 只防止误判
_WATCHDOG_HEARTBEAT_SECONDS = 30


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
        "cleanup_count": task.cleanup_count,
        "news_symbols_total": task.news_symbols_total,
        "errors": _json_loads(task.errors_json, [])[-20:],
        "created_at": task.created_at,
        "started_at": task.started_at,
        "paused_at": task.paused_at,
        "cancelled_at": task.cancelled_at,
        "finished_at": task.finished_at,
        "updated_at": task.updated_at,
        "can_resume": task.status == "paused" and task.paused_at is not None and _now() - task.paused_at <= RESUME_DEADLINE,
        "can_retry": task.status in ("failed", "cancelled", "expired"),
    }


_TERMINAL_STATES = ("done", "failed", "cancelled", "expired")


def _set_task(db: Session, task_id: str, **updates) -> DiscoveryTaskRecord:
    task = db.get(DiscoveryTaskRecord, task_id)
    if task is None:
        raise ValueError("Discovery task not found")
    # 终态保护：已 done/failed/cancelled 的任务不允许被 worker 覆盖状态/阶段
    # 防止用户取消后 worker 仍标记 done 的竞态
    if task.status in _TERMINAL_STATES:
        updates = {k: v for k, v in updates.items() if k not in ("status", "stage")}
        if not updates:
            return task
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
    logger.warning("expire stale tasks: %d rows matched (cutoff=%s)", len(rows), cutoff.isoformat())
    for task in rows:
        logger.warning("expire stale task %s: updated_at=%s stage=%s", task.id, task.updated_at, task.stage)
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
        # 并发保护：已有 queued/running 任务时拒绝创建
        existing = db.execute(
            select(DiscoveryTaskRecord).where(
                DiscoveryTaskRecord.status.in_(("queued", "running"))
            )
        ).scalars().first()
        if existing is not None:
            raise ValueError("已有正在运行的发现任务，请等待完成后再创建")

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


def retry_discovery_task(task_id: str) -> dict:
    """重试已失败/取消/过期的任务。

    关键设计：保留 processed_symbol_ids_json 实现断点续扫（已处理的标的不会重复扫描），
    清空 errors_json，重置状态为 queued 并重启 worker。
    """
    db = SessionLocal()
    try:
        task = db.get(DiscoveryTaskRecord, task_id)
        if task is None:
            raise ValueError("Discovery task not found")
        # 仅终态任务可重试
        if task.status not in ("failed", "cancelled", "expired"):
            return _task_to_dict(task)
        # 并发保护：已有 queued/running 任务时拒绝
        existing = db.execute(
            select(DiscoveryTaskRecord).where(
                DiscoveryTaskRecord.status.in_(("queued", "running"))
            )
        ).scalars().first()
        if existing is not None:
            raise ValueError("已有正在运行的发现任务，请等待完成后再重试")
        # 保留 processed_symbol_ids_json 实现断点续扫；清空 errors 重新开始
        task.status = "queued"
        task.stage = "queued"
        task.percent = 0
        task.message = "任务已重试，等待后台继续（保留已处理进度）"
        task.errors_json = "[]"
        task.started_at = None
        task.paused_at = None
        task.cancelled_at = None
        task.finished_at = None
        task.current_symbol = None
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


def _cn_stock_universe_frame(db: Session):
    errors: list[str] = []
    sources = [
        ("stock_info_a_code_name", ak.stock_info_a_code_name),
        ("stock_zh_a_spot_em", ak.stock_zh_a_spot_em),
    ]
    for api_key, func in sources:
        try:
            with quiet_akshare_output():
                return call_akshare_with_retry(func, api_key=api_key, max_attempts=2, db=db)
        except Exception as exc:
            errors.append(f"{api_key}: {type(exc).__name__}: {exc}")
            logger.warning("Failed to refresh cn-stock universe via %s: %s", api_key, exc)
    raise RuntimeError("A-share universe refresh failed; " + " | ".join(errors))


def _refresh_cn_stock_universe(db: Session) -> dict:
    logger.info("refresh cn-stock universe start")
    try:
        frame = _cn_stock_universe_frame(db)
    except Exception as exc:
        cached_count = db.execute(
            select(func.count(Symbol.id)).where(
                Symbol.is_active == 1,
                Symbol.asset_type == "stock",
                Symbol.market.in_(markets_for_region("cn")),
            )
        ).scalar_one()
        if cached_count:
            logger.warning("Using cached cn-stock universe after refresh failure: %s", exc)
            return {"seen": int(cached_count), "created": 0, "source": "cache", "warning": str(exc)}
        raise

    created = 0
    seen = 0
    code_keys = ["code", "代码", "symbol"]
    name_keys = ["name", "名称"]
    for row in frame.to_dict("records"):
        raw_code = _first_row_value(row, code_keys)
        raw_name = _first_row_value(row, name_keys)
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
    logger.info("refresh cn-stock universe done: seen=%d created=%d", seen, created)
    return {"seen": seen, "created": created}


def _first_row_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _refresh_cn_etf_universe(db: Session) -> dict:
    logger.info("refresh cn-etf universe start")
    created = 0
    seen = 0
    try:
        with quiet_akshare_output():
            # P0 稳定性：用 call_akshare_with_retry 包装，避免直接调用卡死
            frame = call_akshare_with_retry(
                ak.fund_etf_spot_em,
                api_key="fund_etf_spot_em",
                max_attempts=2,
                db=db,
            )
        records = frame.to_dict("records")
        code_keys = ["代码", "基金代码", "symbol", "code"]
        name_keys = ["名称", "基金简称", "name"]
    except Exception:
        with quiet_akshare_output():
            # 备用源同样用 call_akshare_with_retry 包装
            frame = call_akshare_with_retry(
                ak.fund_etf_category_sina,
                api_key="fund_etf_category_sina",
                max_attempts=2,
                db=db,
            )
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
    logger.info("refresh cn-etf universe done: seen=%d created=%d", seen, created)
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
    """从任务记录解析 payload。payload_json 损坏时抛 ValidationError，调用方需捕获并跳过 scope 清理。

    注意：早期实现用 `or "{}"` 回退默认值，会导致 scope 错误（默认 cn-stock）从而误清其他范围的标的。
    现改为显式抛异常，由调用方决定是否跳过清理。
    """
    if not task.payload_json:
        from pydantic import ValidationError
        raise ValidationError("payload_json 为空", DiscoveryTaskCreate)
    return DiscoveryTaskCreate.model_validate_json(task.payload_json)


def _cleanup_discovery_symbols(
    db: Session,
    *,
    scoped_symbol_ids: set[int],
    preserve_extra_ids: set[int] | None = None,
    scope: str | None = None,
) -> int:
    """清理挖掘范围内的僵尸标的（is_active=0）。

    保留：executable 候选（由 preserve_extra_ids 传入）+ 观察池 + 持仓。
    清理范围：
      - 优先用 scope（按 asset_type + region markets）确定的全集，覆盖 prepare 阶段
        _refresh_discovery_universe 写入的全市场僵尸标的（即使没进 synced_symbol_ids）。
      - 若未传 scope，回退到 scoped_symbol_ids（仅实际同步过 K 线的标的）。
    返回：清理数量。
    """
    # 确定待清理候选集：scope 全集 优先；否则回退 synced_symbol_ids
    candidate_ids: set[int] = set()
    scoped_id_set = set(scoped_symbol_ids) if scoped_symbol_ids else set()
    if scope and scope in DISCOVERY_SCOPE_CONFIG:
        config = DISCOVERY_SCOPE_CONFIG[scope]
        stmt = select(Symbol.id).where(Symbol.is_active == 1)
        asset_type = config.get("asset_type")
        region = config.get("region")
        if asset_type:
            stmt = stmt.where(Symbol.asset_type == asset_type)
        markets = markets_for_region(region)
        if markets:
            stmt = stmt.where(Symbol.market.in_(markets))
        candidate_ids = {row[0] for row in db.execute(stmt).all() if row[0] is not None}
        # 若同时传了 synced_symbol_ids，合并进来（兜底）
        candidate_ids |= scoped_id_set
    else:
        candidate_ids = scoped_id_set

    if not candidate_ids:
        return 0

    preserve_ids: set[int] = set()

    if preserve_extra_ids:
        preserve_ids |= preserve_extra_ids

    wl_rows = db.execute(select(WatchlistItem.symbol_id).distinct()).all()
    preserve_ids |= {r[0] for r in wl_rows if r[0] is not None}

    pos_rows = db.execute(select(Position.symbol_id).distinct()).all()
    preserve_ids |= {r[0] for r in pos_rows if r[0] is not None}

    deactivate_ids = candidate_ids - preserve_ids
    if not deactivate_ids:
        return 0

    db.execute(
        update(Symbol)
        .where(Symbol.id.in_(deactivate_ids))
        .values(is_active=0)
    )
    db.commit()
    return len(deactivate_ids)


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


def _watchdog_heartbeat(task_id: str, stop_event: threading.Event) -> None:
    """watchdog 心跳线程：每 30s 更新 task.updated_at，防止 _expire_stale_tasks 误判。

    风控加固（P1-7 竞态修复）：
    只用条件 UPDATE 更新 updated_at 字段，WHERE 限定 status NOT IN 终态 AND stage != 'prepare'。
    这样避免整行覆盖 worker 主线程刚写入的 status/stage/percent 等字段，
    同时通过 rowcount 判断是否真的更新了行（若 status 已是终态或 stage 已切走则跳过）。

    真正的卡死由单 symbol 超时（SYNC_ONE_SYMBOL_TIMEOUT_SECONDS=90s）兜底。
    当任务进入终态或 stop_event 被设置时退出。

    关键：prepare 阶段（universe 刷新）不更新 updated_at，让 _expire_stale_tasks
    能兜底中断卡死的 prepare。sync 阶段才正常心跳（单 symbol 90s 超时已兜底）。
    """
    from sqlalchemy import update

    while not stop_event.wait(timeout=_WATCHDOG_HEARTBEAT_SECONDS):
        try:
            db = SessionLocal()
            try:
                # 风控加固：先用条件 UPDATE 只更新 updated_at 字段，避免整行覆盖
                # WHERE 限定非终态 + 非 prepare 阶段，rowcount=0 表示任务已终态或仍在 prepare
                now = _now()
                stmt = (
                    update(DiscoveryTaskRecord.__table__)
                    .where(
                        DiscoveryTaskRecord.id == task_id,
                        DiscoveryTaskRecord.status.notin_(_TERMINAL_STATES),
                        DiscoveryTaskRecord.stage != "prepare",
                    )
                    .values(updated_at=now)
                )
                result = db.execute(stmt)
                db.commit()

                if result.rowcount == 0:
                    # 行未更新：可能是任务已进入终态，或仍在 prepare 阶段
                    # 再查一次状态判断是否该退出
                    task = db.get(DiscoveryTaskRecord, task_id)
                    if task is None or task.status in _TERMINAL_STATES:
                        logger.info(
                            "watchdog %s exit: status=%s",
                            task_id, task.status if task else "None",
                        )
                        return
                    # 仍在 prepare 阶段，跳过本次心跳（让 _expire_stale_tasks 兜底）
                    logger.debug("watchdog %s skip heartbeat in prepare stage", task_id)
                else:
                    logger.debug("watchdog %s heartbeat (rowcount=%d)", task_id, result.rowcount)
            finally:
                db.close()
        except Exception:
            # watchdog 失败不影响主流程，下次心跳再试，但需记录日志便于诊断
            logger.warning("watchdog %s heartbeat failed", task_id, exc_info=True)


def _sync_one_symbol_with_timeout(
    db: Session, symbol: Symbol, payload: Any, portfolio_id: int | None
) -> tuple[dict, bool]:
    """包装 _sync_one_symbol 加超时保护，防止单 symbol 卡死阻塞整个任务。

    使用 concurrent.futures.ThreadPoolExecutor + future.result(timeout=) 实现。
    超时后抛出 TimeoutError，由调用方记录 failed_count 并继续下一个 symbol。
    注意：超时后子线程仍会泄漏（Python 无法强制 kill 线程），但不阻塞主流程。

    关键：子线程使用独立的 db Session，避免主子线程并发操作同一 Session 导致死锁或脏数据。
    """
    import concurrent.futures
    from app.db.manager import DatabaseManager

    SessionLocal = DatabaseManager.get().session_factory

    def _run() -> tuple[dict, bool]:
        sub_db = SessionLocal()
        try:
            return _sync_one_symbol(sub_db, symbol, payload, portfolio_id)
        finally:
            sub_db.close()

    logger.info("sync_one %s start (timeout=%ds)", symbol.symbol, SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run)
        return future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)


def _refresh_universe_with_timeout(db: Session, payload: DiscoveryTaskCreate) -> dict | None:
    """universe 刷新加 timeout 保护，防止 prepare 阶段永久阻塞。

    akshare 内部 pd.read_excel 可能绕过 requests timeout 永久阻塞，
    用 ThreadPoolExecutor + future.result(timeout=) 兜底。
    超时后返回 None，调用方标记 task=failed。

    关键：这里复用主线程的 db Session，因为 _refresh_discovery_universe 内部会
    upsert Symbol 表，主线程后续 _resolve_discovery_symbols 需要看到这些数据。
    timeout 后子线程仍在跑，但主线程会标记 task=failed 并退出，不再访问 db。
    """
    import concurrent.futures

    logger.info("refresh universe start (timeout=%ds)", UNIVERSE_REFRESH_TIMEOUT_SECONDS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_refresh_discovery_universe, db, payload)
        try:
            result = future.result(timeout=UNIVERSE_REFRESH_TIMEOUT_SECONDS)
            logger.info("refresh universe done: %s", result)
            return result
        except concurrent.futures.TimeoutError:
            logger.error(
                "universe refresh TIMEOUT after %ds, payload scope=%s",
                UNIVERSE_REFRESH_TIMEOUT_SECONDS, payload.scope,
            )
            return None


def _run_discovery_task(task_id: str) -> None:
    db = SessionLocal()
    # 启动 watchdog 心跳线程，防止长时间同步任务被 _expire_stale_tasks 误判
    watchdog_stop = threading.Event()
    watchdog_thread = threading.Thread(
        target=_watchdog_heartbeat,
        args=(task_id, watchdog_stop),
        daemon=True,
        name=f"discovery-watchdog-{task_id}",
    )
    watchdog_thread.start()
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
            universe = _refresh_universe_with_timeout(db, payload)
            if universe is None:
                # universe 刷新超时（120s），直接中断任务
                raise RuntimeError("全市场标的列表刷新超时（120s），akshare 接口可能卡死，请稍后重试")
            # seen=0 说明全市场标的列表拉取失败（akshare 接口异常或网络问题）
            # 此时继续跑空扫描会让用户困惑，直接中断任务
            if universe.get("seen", 0) == 0:
                raise RuntimeError("全市场标的列表拉取失败（akshare 接口异常或网络问题），请稍后重试")
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

            logger.info("discovery %s: sync %s (%d/%d)", task_id, symbol.symbol, index, len(symbols))
            try:
                try:
                    result, scored = _sync_one_symbol_with_timeout(db, symbol, payload, portfolio_id)
                except TimeoutError as timeout_exc:
                    # 单 symbol 同步超时（90s），跳过该 symbol，记录 failed_count
                    # 子线程仍会泄漏（Python 无法强制 kill），但不阻塞主流程
                    logger.warning(
                        "sync_one %s TIMEOUT after %ds (discovery %s)",
                        symbol.symbol, SYNC_ONE_SYMBOL_TIMEOUT_SECONDS, task_id,
                    )
                    db.rollback()
                    task = db.get(DiscoveryTaskRecord, task_id)
                    task.failed_count += 1
                    _append_error(task, {
                        "symbol_id": symbol.id,
                        "symbol": symbol.symbol,
                        "error": f"同步超时（{SYNC_ONE_SYMBOL_TIMEOUT_SECONDS}s），跳过",
                    })
                    adaptive_delay = min(5, max(payload.delay_seconds, adaptive_delay * 1.4 + 0.1))
                    # 跳过下面的状态更新，直接进入 processed_ids 更新
                    result = {"status": "failed"}
                    scored = False
                else:
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
        # P0：把当前激活评分预设信息写入 filters_snapshot，便于 run_scan 应用维度阈值并归档
        scope_cfg = DISCOVERY_SCOPE_CONFIG.get(payload.scope) or {}
        active_preset_info = {}
        try:
            from app.services.scoring_config_engine import get_active_scoring_config
            active_preset = get_active_scoring_config(db, scope_cfg.get("asset_type", "stock"))
            if active_preset is not None:
                active_preset_info = {
                    "scoring_config_id": active_preset.id,
                    "scoring_preset_key": active_preset.preset_key,
                    "scoring_preset_name": active_preset.name,
                    "scoring_config_version": active_preset.version,
                }
        except Exception:
            active_preset_info = {}
        scan_run = run_scan(
            db=db,
            scope_snapshot={"symbol_ids": synced_symbol_ids},
            filters_snapshot={
                "source": "discovery.task",
                "task_id": task_id,
                "scope": payload.scope,
                "min_score": payload.min_score,
                "global_mode": payload.global_mode,
                **active_preset_info,
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
        # done 清理：保留 executable 候选 + 观察池 + 持仓，其余 is_active=0
        # 传 scope 以覆盖 prepare 阶段 _refresh_discovery_universe 写入的全市场僵尸标的
        cleanup_count = _cleanup_discovery_symbols(
            db,
            scoped_symbol_ids=set(synced_symbol_ids),
            preserve_extra_ids={row.symbol_id for row in executable_rows},
            scope=payload.scope,
        )
        _set_task(
            db,
            task_id,
            scan_run_id=scan_run.id,
            executable_count=len(executable_rows),
            cleanup_count=cleanup_count,
            message=f"扫描完成，找到 {len(executable_rows)} 个可执行候选，已清理 {cleanup_count} 个僵尸标的",
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
            # failed 清理：只保留观察池 + 持仓，没有 executable
            # 传 scope 以清理 prepare 阶段写入的全市场僵尸标的
            # payload_json 损坏时跳过 scope（避免回退默认 scope 误清其他范围），只清 synced_ids
            try:
                synced_ids = set(_json_loads(task.synced_symbol_ids_json, []))
                failed_scope = None
                try:
                    failed_scope = _payload_from_task(task).scope
                except Exception:
                    _append_error(task, {"scope": "cleanup", "error": "payload_json 解析失败，跳过 scope 清理，仅清理 synced_symbol_ids"})
                cleanup_cnt = _cleanup_discovery_symbols(
                    db, scoped_symbol_ids=synced_ids, preserve_extra_ids=None, scope=failed_scope,
                )
            except Exception:
                cleanup_cnt = 0
            task.status = "failed"
            task.stage = "failed"
            task.percent = 100
            task.cleanup_count = cleanup_cnt
            task.message = f"机会挖掘失败：{exc}"
            task.finished_at = _now()
            _append_error(task, {"scope": "task", "error": str(exc), "traceback": traceback.format_exc(limit=8)})
            db.commit()
    finally:
        # 停止 watchdog 心跳线程
        watchdog_stop.set()
        # cancelled 清理：只保留观察池 + 持仓
        # paused 不清理，等续跑或定时任务
        # 传 scope 以清理 prepare 阶段写入的全市场僵尸标的
        # payload_json 损坏时跳过 scope（避免回退默认 scope 误清其他范围），只清 synced_ids
        try:
            task = db.get(DiscoveryTaskRecord, task_id)
            if task is not None and task.status == "cancelled":
                synced_ids = set(_json_loads(task.synced_symbol_ids_json, []))
                cancelled_scope = None
                try:
                    cancelled_scope = _payload_from_task(task).scope
                except Exception:
                    _append_error(task, {"scope": "cleanup", "error": "payload_json 解析失败，跳过 scope 清理，仅清理 synced_symbol_ids"})
                cleanup_cnt = _cleanup_discovery_symbols(
                    db, scoped_symbol_ids=synced_ids, preserve_extra_ids=None, scope=cancelled_scope,
                )
                if cleanup_cnt:
                    task.cleanup_count = cleanup_cnt
                    db.commit()
        except Exception:
            pass
        db.close()
