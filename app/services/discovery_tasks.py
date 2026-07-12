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


def list_discovery_tasks(limit: int = 20, scope: str | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        _expire_stale_tasks(db)
        stmt = select(DiscoveryTaskRecord)
        if scope:
            stmt = stmt.where(DiscoveryTaskRecord.scope == scope)
        rows = (
            db.execute(stmt.order_by(desc(DiscoveryTaskRecord.created_at)).limit(limit))
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


def _can_resume_paused_task(task: DiscoveryTaskRecord) -> bool:
    return task.status == "paused" and task.paused_at is not None and _now() - task.paused_at <= RESUME_DEADLINE


def _expire_paused_task(task: DiscoveryTaskRecord) -> None:
    task.status = "expired"
    task.stage = "failed"
    task.percent = 100
    task.message = "暂停已超过1天，请重新开始"
    task.finished_at = _now()
    task.current_symbol = None



def _resume_task(db: Session, task: DiscoveryTaskRecord, *, message: str) -> dict:
    task.status = "queued"
    task.stage = "queued"
    task.message = message
    task.paused_at = None
    task.cancelled_at = None
    task.finished_at = None
    task.current_symbol = None
    db.commit()
    db.refresh(task)
    _start_worker(task.id)
    return _task_to_dict(task)



def _same_discovery_request(task: DiscoveryTaskRecord, payload: DiscoveryTaskCreate) -> bool:
    saved = _json_loads(task.payload_json, {})
    return task.scope == payload.scope and saved.get("portfolio_id") == payload.portfolio_id



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

        paused_tasks = db.execute(
            select(DiscoveryTaskRecord)
            .where(
                DiscoveryTaskRecord.scope == payload.scope,
                DiscoveryTaskRecord.status == "paused",
            )
            .order_by(desc(DiscoveryTaskRecord.paused_at), desc(DiscoveryTaskRecord.created_at))
        ).scalars().all()
        matched_paused = next((item for item in paused_tasks if _same_discovery_request(item, payload)), None)
        if matched_paused is not None:
            if _can_resume_paused_task(matched_paused):
                return _resume_task(db, matched_paused, message="任务已继续，保留原有进度")
            _expire_paused_task(matched_paused)
            db.commit()

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
        if not _can_resume_paused_task(task):
            _expire_paused_task(task)
            db.commit()
            db.refresh(task)
            return _task_to_dict(task)
        return _resume_task(db, task, message="任务已恢复，等待后台继续")
    finally:
        db.close()



def retry_discovery_task(task_id: str) -> dict:
    """重试已失败/取消/过期的任务。
    关键设计：保留 processed_symbol_ids_json 实现断点续扫（已处理的标的不会重复扫描）；
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
    except Exception as exc:
        # P0 稳定性：主源（东财 push2）失败时，优先尝试 sina 备用源拉取全量 ETF 列表
        # 注意顺序：必须先尝试 sina 备用源（能拿到全市场 ~1500 只 ETF），而非直接用 cache 兜底
        # 因为 DB 中可能只有少量手动添加的 ETF（如 3 只），直接用 cache 会导致任务只处理这几个标的
        # sina 源不受东财 push2 IP 频次风控影响，通常可成功
        logger.warning("cn-etf primary source failed, trying sina backup: %s", exc)
        try:
            with quiet_akshare_output():
                # 注意：必须显式传 symbol="ETF基金"，否则默认 "LOF基金" 会返回 LOF 列表
                frame = call_akshare_with_retry(
                    ak.fund_etf_category_sina,
                    symbol="ETF基金",
                    api_key="fund_etf_category_sina",
                    max_attempts=2,
                    db=db,
                )
            records = frame.to_dict("records")
            code_keys = ["代码", "symbol", "code"]
            name_keys = ["名称", "name"]
        except Exception as exc2:
            # sina 备用源也失败，才用 cached ETF 标的兜底（避免任务直接中断）
            cached_count = db.execute(
                select(func.count(Symbol.id)).where(
                    Symbol.is_active == 1,
                    Symbol.asset_type == "etf",
                    Symbol.market.in_(markets_for_region("cn")),
                )
            ).scalar_one()
            if cached_count:
                logger.warning(
                    "Using cached cn-etf universe after both sources failed: primary=%s | sina=%s",
                    exc, exc2,
                )
                return {"seen": int(cached_count), "created": 0, "source": "cache", "warning": str(exc)}
            # 两个源都失败且无 cache，抛出最后异常
            raise

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


def _resolve_universe_symbols_for_discovery(
    db: Session, payload: DiscoveryTaskCreate
) -> list[tuple[Any, Symbol]]:
    """从基础表 universe_symbols 读已同步标的（is_synced=1），返回 [(UniverseSymbol, Symbol)]。

    核心改造：挖掘任务不再调 akshare 刷新标的列表，直接读基础表。
    对每个 universe_symbol，用 symbol code 在 symbols 表中查找；
    若不存在则创建 Symbol 记录（is_active=1），确保 scores 表外键可用。
    """
    from app.models.universe import UniverseSymbol

    if payload.scope not in DISCOVERY_SCOPE_CONFIG:
        raise ValueError(f"Unsupported discovery scope: {payload.scope}")
    scope_cfg = DISCOVERY_SCOPE_CONFIG[payload.scope]
    asset_type = scope_cfg.get("asset_type")
    region = scope_cfg.get("region")

    # 从 universe_symbols 读已同步标的
    stmt = (
        select(UniverseSymbol)
        .where(
            UniverseSymbol.is_synced == 1,
            UniverseSymbol.asset_type == asset_type,
            UniverseSymbol.region == region,
        )
        .order_by(UniverseSymbol.id.asc())
    )
    if payload.symbol_limit is not None:
        stmt = stmt.limit(payload.symbol_limit)
    universe_symbols = db.execute(stmt).scalars().all()

    if not universe_symbols:
        logger.warning(
            "discovery: no synced universe symbols for scope=%s (region=%s, asset_type=%s)",
            payload.scope, region, asset_type,
        )
        return []

    # 批量查 symbols 表，避免 N+1
    codes = [us.symbol for us in universe_symbols]
    existing_symbols = db.execute(
        select(Symbol).where(Symbol.symbol.in_(codes))
    ).scalars().all()
    code_to_symbol: dict[str, Symbol] = {s.symbol: s for s in existing_symbols}

    result: list[tuple[Any, Symbol]] = []
    created_count = 0
    for us in universe_symbols:
        sym = code_to_symbol.get(us.symbol)
        if sym is None:
            # symbols 表不存在，创建记录（评分需要 Symbol.id 写 scores 表）
            sym = Symbol(
                symbol=us.symbol,
                name=us.name or us.symbol,
                asset_type=us.asset_type,
                market=us.market,
                board=us.board,
                is_active=1,
            )
            db.add(sym)
            db.flush()  # 获取 sym.id
            created_count += 1
            code_to_symbol[us.symbol] = sym
        elif sym.is_active != 1:
            sym.is_active = 1
        result.append((us, sym))

    if created_count > 0:
        db.commit()
        logger.info(
            "discovery: created %d new Symbol records from universe_symbols (scope=%s)",
            created_count, payload.scope,
        )
    logger.info(
        "discovery: resolved %d universe symbols for scope=%s (%d already in symbols table)",
        len(result), payload.scope, len(result) - created_count,
    )
    return result


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
    # 网络请求前释放 DB 连接，避免 refresh_symbol_name / sync_symbol_daily_bars
    # 长时间网络请求期间连接被 MySQL 关闭（Lost connection during query）
    db.commit()
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


def _normalize_task_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _can_reuse_discovery_score(existing_score: Any | None, universe_symbol: Any) -> bool:
    if existing_score is None:
        return False
    score_created_at = _normalize_task_time(getattr(existing_score, "created_at", None))
    last_synced_at = _normalize_task_time(getattr(universe_symbol, "last_synced_at", None))
    if score_created_at is None or last_synced_at is None:
        return False
    return score_created_at >= last_synced_at


def _should_refresh_discovery_stale_bars(payload: DiscoveryTaskCreate) -> bool:
    """sync 模式先补过期 K 线；cached 模式直接复用当前基础数据评分。"""
    return bool(payload.refresh_universe)


def _score_universe_symbol(
    db: Session,
    universe_symbol: Any,
    symbol: Symbol,
    trade_date: date,
    portfolio_id: int | None,
    *,
    prefetched_bars: list | None = None,
    existing_score_map: dict[int, Any] | None = None,
) -> tuple[dict, bool]:
    """从基础表 universe_daily_bars 读K线评分，不调 akshare，不写 daily_bars 表。

    替代 _sync_one_symbol 的网络同步+评分逻辑：
    - 数据源：universe_daily_bars（纯 DB 读取，<50ms/标的）
    - 评分写入：scores 表（关联 symbol.id）
    - 交易计划：延后到 executable 候选阶段生成，避免对全市场逐标的重复 upsert

    P1 优化：支持外部预载 K线 和 Score 去重字典，避免 N+1 查询。

    Returns:
        (result_dict, scored_bool)
        scored_bool=True 表示该标的已有可用于扫描的有效评分（含复用当天最新评分）。
    """
    from app.services.scoring_config_engine import calculate_universe_symbol_score

    existing_score = existing_score_map.get(symbol.id) if existing_score_map is not None else None
    score_reused = _can_reuse_discovery_score(existing_score, universe_symbol)
    if score_reused:
        score = existing_score
    else:
        score = calculate_universe_symbol_score(
            db, universe_symbol, symbol, trade_date,
            prefetched_bars=prefetched_bars,
            existing_score_map=existing_score_map,
        )
        if existing_score_map is not None:
            existing_score_map[symbol.id] = score

    return (
        {
            "symbol_id": symbol.id,
            "symbol": symbol.symbol,
            "asset_type": symbol.asset_type,
            "status": "ok",
            "source": "reused_score" if score_reused else "universe_daily_bars",
            "score_reused": score_reused,
            "rows": 0,
            "latest_score": {
                "trade_date": trade_date.isoformat(),
                "quality_score": score.quality_score,
                "timing_score": score.timing_score,
                "stage": score.stage,
                "action": score.action,
            },
        },
        True,
    )
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
    # 只查 task 的 status 字段，不调用 db.expire_all()（会过期所有已加载的 Symbol 对象，
    # 导致后续访问属性触发大量懒加载查询，严重拖慢挖掘速度）
    row = db.execute(
        select(DiscoveryTaskRecord.status).where(DiscoveryTaskRecord.id == task_id)
    ).scalar_one_or_none()
    if row is None:
        return "cancelled"
    if row == "paused":
        task = db.get(DiscoveryTaskRecord, task_id)
        task.message = "任务已暂停，进度已保留"
        task.current_symbol = None
        db.commit()
        return "paused"
    if row == "cancelled":
        task = db.get(DiscoveryTaskRecord, task_id)
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
                # WHERE 限定非终态，rowcount=0 表示任务已进入终态
                # 注意：prepare 阶段也心跳，防止 prepare→sync 过渡时卡死被 _expire_stale_tasks 误判
                # （universe 刷新有自己的 120s 超时保护，不需要靠 stale 检测兜底）
                now = _now()
                stmt = (
                    update(DiscoveryTaskRecord.__table__)
                    .where(
                        DiscoveryTaskRecord.id == task_id,
                        DiscoveryTaskRecord.status.notin_(_TERMINAL_STATES),
                    )
                    .values(updated_at=now)
                )
                result = db.execute(stmt)
                db.commit()

                if result.rowcount == 0:
                    # 行未更新：任务已进入终态，退出心跳
                    task = db.get(DiscoveryTaskRecord, task_id)
                    if task is None or task.status in _TERMINAL_STATES:
                        logger.info(
                            "watchdog %s exit: status=%s",
                            task_id, task.status if task else "None",
                        )
                        return
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


def _sync_one_symbol_concurrent(
    symbol: Symbol, payload: Any, portfolio_id: int | None
) -> tuple[dict, bool]:
    """并发模式下的单 symbol 同步 worker：使用独立 DB Session，无超时包装（由调用方 future.result(timeout) 控制）。

    与 _sync_one_symbol_with_timeout 的区别：
    - 后者每次创建 1-worker 线程池做超时，适合串行模式
    - 本函数直接在调用方线程池中执行，避免嵌套线程池，适合并发模式

    含 OperationalError 重试：并发模式下连接可能被 MySQL 关闭，重试一次让 SQLAlchemy 重新获取连接。
    """
    from app.db.manager import DatabaseManager
    from sqlalchemy.exc import OperationalError

    SessionLocal = DatabaseManager.get().session_factory

    last_exc: Exception | None = None
    for attempt in range(2):  # 最多重试 1 次
        sub_db = SessionLocal()
        try:
            result = _sync_one_symbol(sub_db, symbol, payload, portfolio_id)
            return result
        except OperationalError as exc:
            # 连接被 MySQL 关闭（Lost connection / Connection aborted），回滚后重试
            last_exc = exc
            try:
                sub_db.rollback()
            except Exception:
                pass
            if attempt == 0:
                logger.warning(
                    "concurrent sync_one %s OperationalError (attempt 1/2), retrying: %s",
                    symbol.symbol, exc,
                )
                continue
            raise
        finally:
            sub_db.close()
    # 理论不可达
    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


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
            # P1 改造：从基础表 universe_symbols 读已同步标的，不再调 akshare 刷新
            universe_pairs = _resolve_universe_symbols_for_discovery(db, payload)
            if not universe_pairs:
                _set_task(
                    db, task_id, status="failed", stage="failed", percent=100,
                    message="基础表无已同步标的，请先在「设置 → 基础数据」完成初始化同步",
                    finished_at=_now(),
                )
                return
            task.message = f"已从基础表载入 {len(universe_pairs)} 个标的"
            db.commit()
        else:
            # 断点续传：重新从 universe_symbols 载入标的列表
            universe_pairs = _resolve_universe_symbols_for_discovery(db, payload)
            if not universe_pairs:
                _set_task(
                    db, task_id, status="failed", stage="failed", percent=100,
                    message="基础表无已同步标的，请先在「设置 → 基础数据」完成初始化同步",
                    finished_at=_now(),
                )
                return

        total = len(universe_pairs)
        task = _set_task(db, task_id, total=total, message=f"已载入 {total} 个标的，开始评分")
        if total == 0:
            _set_task(db, task_id, status="done", stage="done", percent=100, message="当前范围没有可扫描标的", finished_at=_now())
            return

        # P3：sync 模式下先补过期 K 线；cached 模式直接使用当前基础数据评分
        # P3.1 缺失检测：last_bar_date < today 或为 None 的标的需要补齐
        # P3.2 自动补齐：并发调 _sync_one_concurrent 增量同步（带分批限速）
        # P3.3 跳过策略：补齐失败的标的仍参与评分（用已有历史K线），完全无K线的返回中性分
        from app.services.universe_sync import _sync_one_concurrent, SYNC_FAILED_THRESHOLD

        today = datetime.now().date()
        stale_pairs = [
            (us, sym) for us, sym in universe_pairs
            if us.last_bar_date is None or us.last_bar_date < today
        ]
        stale_pending = [
            (us, sym) for us, sym in stale_pairs if sym.id not in processed_ids
        ]
        should_refresh_stale_bars = _should_refresh_discovery_stale_bars(payload)

        if stale_pending and should_refresh_stale_bars:
            # 过滤掉已熔断标的（sync_failed >= 阈值），这些标的补齐无意义
            stale_syncable = [
                (us, sym) for us, sym in stale_pending
                if us.sync_failed < SYNC_FAILED_THRESHOLD
            ]
            stale_broken = len(stale_pending) - len(stale_syncable)

            _set_task(
                db, task_id, stage="prepare",
                message=f"缓存补齐：{len(stale_syncable)} 个标的K线过期，正在增量同步"
                        + (f"（{stale_broken} 个已熔断跳过）" if stale_broken else ""),
            )
            logger.info(
                "discovery %s: P3 cache fill — stale=%d syncable=%d broken=%d",
                task_id, len(stale_pending), len(stale_syncable), stale_broken,
            )

            if stale_syncable:
                from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
                from app.services.universe_sync import SYNC_ONE_SYMBOL_TIMEOUT_SECONDS, DEFAULT_HISTORY_DAYS

                # P3.2 并发补齐（分批限速，与增量同步一致）
                P3_BATCH_SIZE = 50
                P3_BATCH_INTERVAL = 1.0
                P3_MAX_WORKERS = 5
                fill_ok = 0
                fill_failed = 0
                fill_uptodate = 0
                fill_total = len(stale_syncable)

                for batch_start in range(0, fill_total, P3_BATCH_SIZE):
                    if _check_stop_state(db, task_id):
                        logger.info("discovery %s: P3 cache fill cancelled at %d/%d", task_id, batch_start, fill_total)
                        break
                    batch = stale_syncable[batch_start:batch_start + P3_BATCH_SIZE]
                    with ThreadPoolExecutor(max_workers=P3_MAX_WORKERS, thread_name_prefix="p3-fill") as executor:
                        futures = {
                            executor.submit(_sync_one_concurrent, us.id, DEFAULT_HISTORY_DAYS): (us, sym)
                            for us, sym in batch
                        }
                        for future in as_completed(futures):
                            if _check_stop_state(db, task_id):
                                for f in futures:
                                    f.cancel()
                                break
                            us, sym = futures[future]
                            try:
                                result = future.result(timeout=SYNC_ONE_SYMBOL_TIMEOUT_SECONDS)
                                status = result.get("status")
                                if status in ("ok", "empty"):
                                    fill_ok += 1
                                elif status == "uptodate":
                                    fill_uptodate += 1
                                else:
                                    fill_failed += 1
                            except (FuturesTimeoutError, Exception) as exc:
                                logger.warning("discovery %s: P3 fill %s failed: %s", task_id, us.symbol, exc)
                                fill_failed += 1

                    fill_processed = fill_ok + fill_uptodate + fill_failed
                    _set_task(
                        db, task_id,
                        message=f"缓存补齐中 {fill_processed}/{fill_total}（成功 {fill_ok}，已是最新 {fill_uptodate}，失败 {fill_failed}）",
                    )
                    is_last = batch_start + P3_BATCH_SIZE >= fill_total
                    if not is_last:
                        time.sleep(P3_BATCH_INTERVAL)

                logger.info(
                    "discovery %s: P3 cache fill done — ok=%d uptodate=%d failed=%d (total=%d)",
                    task_id, fill_ok, fill_uptodate, fill_failed, fill_total,
                )
                db.expire_all()
                universe_pairs = _resolve_universe_symbols_for_discovery(db, payload)
        elif stale_pending:
            logger.info(
                "discovery %s: cached mode skip stale bar refresh — stale=%d",
                task_id, len(stale_pending),
            )
            _set_task(
                db,
                task_id,
                stage="prepare",
                message=f"直接使用基础数据评分，跳过 {len(stale_pending)} 个过期标的的K线补齐",
            )
        else:
            logger.info("discovery %s: P3 cache fill skipped — all symbols up-to-date", task_id)
        # P1 改造：sync 阶段改为纯 DB 读取 + 评分（不调 akshare，不写 daily_bars 表）
        # 速度从 5-15s/标的 降至 <50ms/标的，5500 标的约 4-8 分钟
        # 评分日期用今天（calculate_universe_symbol_score 内部查 <= today 的K线）
        score_date = datetime.now().date()
        pending_pairs = [(us, sym) for us, sym in universe_pairs if sym.id not in processed_ids]

        logger.info(
            "discovery %s: universe-based scoring start, total=%d pending=%d",
            task_id, total, len(pending_pairs),
        )

        # P1.1 + P1.2：批量预载 K线 + 批量预查 Score 去重（N+1 → 1，大幅减少 DB 往返）
        # 空间换时间：5500标的 * 80根 ≈ 44万行 K线常驻内存（约 50-100MB），评分完成后释放
        from itertools import groupby
        from app.models.universe import UniverseDailyBar
        from app.models.score import Score as ScoreModel
        from app.services.scoring_config_engine import get_active_scoring_config

        # P1.1：批量预载 K线（按 universe_symbol_id 分组，每组取最新 80 根，升序排列）
        bars_map: dict[int, list] = {}
        if pending_pairs:
            us_ids = [us.id for us, _ in pending_pairs]
            all_bars = db.execute(
                select(UniverseDailyBar)
                .where(
                    UniverseDailyBar.universe_symbol_id.in_(us_ids),
                    UniverseDailyBar.trade_date <= score_date,
                )
                .order_by(UniverseDailyBar.universe_symbol_id, UniverseDailyBar.trade_date.desc())
            ).scalars().all()
            for us_id, group in groupby(all_bars, key=lambda b: b.universe_symbol_id):
                # group 已按 trade_date desc 排序，取前 80 根（最新 80 根），再反转为升序
                bars_map[us_id] = list(reversed(list(group)[:80]))
            logger.info(
                "discovery %s: prefetched %d bars for %d symbols",
                task_id, sum(len(v) for v in bars_map.values()), len(bars_map),
            )

        # P1.2：批量预查 Score（覆盖当前 scope 全量标的，便于断点续扫复用当天评分）
        existing_score_map: dict[int, Any] = {}
        symbol_map = {sym.id: sym for _, sym in universe_pairs}
        for asset_type in ("stock", "etf"):
            type_pairs = [(us, sym) for us, sym in universe_pairs if (sym.asset_type or "stock") == asset_type]
            if not type_pairs:
                continue
            cfg = get_active_scoring_config(db, asset_type)
            if cfg is None:
                continue
            date_key = score_date.isoformat()
            calc_batch_id = f"sc-{cfg.id}-v{cfg.version}-{date_key}"
            type_sym_ids = [sym.id for _, sym in type_pairs]
            existing_scores = db.execute(
                select(ScoreModel).where(
                    ScoreModel.symbol_id.in_(type_sym_ids),
                    ScoreModel.trade_date == score_date,
                    ScoreModel.calc_batch_id == calc_batch_id,
                )
            ).scalars().all()
            for s in existing_scores:
                existing_score_map[s.symbol_id] = s
            logger.info(
                "discovery %s: prefetched %d existing scores for %s (%d symbols)",
                task_id, len(existing_scores), asset_type, len(type_sym_ids),
            )

        # P1.4：进度更新降频——每 20 个标的检查一次停止状态，避免每标的一次 SELECT
        PROGRESS_FLUSH_INTERVAL = 20
        reused_score_count = 0
        rescored_count = 0

        for index, (universe_symbol, symbol) in enumerate(pending_pairs, start=1):
            if symbol.id in processed_ids:
                continue
            if index % PROGRESS_FLUSH_INTERVAL == 1:
                stop_state = _check_stop_state(db, task_id)
                if stop_state:
                    return

            task = db.get(DiscoveryTaskRecord, task_id)
            task.stage = "sync"
            task.percent = _sync_progress(task.processed, total)
            task.current_symbol = symbol.symbol
            task.message = f"正在评分 {symbol.symbol} ({task.processed + 1}/{total})"
            db.flush()

            try:
                prefetched_bars = bars_map.get(universe_symbol.id, [])
                result, scored = _score_universe_symbol(
                    db, universe_symbol, symbol, score_date, portfolio_id,
                    prefetched_bars=prefetched_bars,
                    existing_score_map=existing_score_map,
                )
                if result["status"] == "ok":
                    task.ok_count += 1
                    if symbol.id not in synced_symbol_ids:
                        synced_symbol_ids.append(symbol.id)
                    if scored:
                        task.scored_count += 1
                        if result.get("score_reused"):
                            reused_score_count += 1
                        else:
                            rescored_count += 1
                else:
                    task.failed_count += 1
                db.flush()
            except Exception as exc:
                db.rollback()
                task = db.get(DiscoveryTaskRecord, task_id)
                task.failed_count += 1
                _append_error(task, {"symbol_id": symbol.id, "symbol": symbol.symbol, "error": str(exc)})

            processed_ids.add(symbol.id)
            task.processed = len(processed_ids)
            task.percent = _sync_progress(task.processed, total)
            task.processed_symbol_ids_json = json.dumps(sorted(processed_ids))
            task.synced_symbol_ids_json = json.dumps(synced_symbol_ids)
            if index % PROGRESS_FLUSH_INTERVAL == 0:
                db.commit()
                logger.info(
                    "discovery %s: scoring progress %d/%d (%.1f%%)",
                    task_id, task.processed, total, task.percent,
                )
            else:
                db.flush()
        db.commit()
        logger.info(
            "discovery %s: scoring done processed=%d rescored=%d reused=%d failed=%d",
            task_id,
            len(processed_ids),
            rescored_count,
            reused_score_count,
            task.failed_count if task is not None else 0,
        )
        # 预载K线用完即释放，降低内存占用；Score 映射保留到 executable 交易计划阶段复用
        bars_map.clear()
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

        if portfolio_id is not None and executable_rows:
            trade_setup_ok = 0
            trade_setup_failed = 0
            for row in executable_rows:
                symbol_ref = symbol_map.get(row.symbol_id)
                score_ref = existing_score_map.get(row.symbol_id)
                if symbol_ref is None or score_ref is None:
                    trade_setup_failed += 1
                    continue
                try:
                    upsert_trade_setup(
                        db=db,
                        portfolio_id=portfolio_id,
                        symbol=symbol_ref,
                        score=score_ref,
                        scan_run_id=scan_run.id,
                    )
                    trade_setup_ok += 1
                except Exception:
                    trade_setup_failed += 1
                    logger.warning(
                        "discovery %s: trade setup skipped for %s",
                        task_id,
                        symbol_ref.symbol,
                        exc_info=True,
                    )
            logger.info(
                "discovery %s: trade setups generated ok=%d failed=%d",
                task_id,
                trade_setup_ok,
                trade_setup_failed,
            )

        db.commit()
        existing_score_map.clear()
        # P1：把 executable 候选同步写入 discovery_candidates（is_promoted=0）
        # 用户在前端手动点击"加入候选池"才执行 promote_candidate 标记 is_promoted=1
        # 幂等：通过 (scan_run_id, universe_symbol_id) 唯一约束去重
        try:
            from app.services.candidate_promote import sync_scan_results_to_candidates
            candidate_count = sync_scan_results_to_candidates(
                db,
                scan_run_id=scan_run.id,
                warning_days=payload.warning_days,
                valid_days=payload.valid_days,
            )
            logger.info(
                "discovery %s: synced %d candidates to discovery_candidates (scan_run=%d)",
                task_id, candidate_count, scan_run.id,
            )
        except Exception:
            logger.exception("discovery %s: candidate sync failed (non-fatal)", task_id)

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
