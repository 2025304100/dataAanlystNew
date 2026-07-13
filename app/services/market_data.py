from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import logging
from threading import Event, Lock, RLock
import os
import time

import akshare as ak
import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.services.akshare_utils import quiet_akshare_output
from app.models.daily_bar import DailyBar
from app.models.portfolio import Position
from app.models.scan import ScanResult
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.models.watchlist import WatchlistItem
from app.services.allocation import get_active_rule, get_default_portfolio
from app.services.analysis import calculate_symbol_score
from app.services.scoring_config_engine import calculate_universe_symbol_score
from app.services.regions import markets_for_region, region_from_market
from app.services.scans import run_scan
from app.services.symbol_names import refresh_symbol_name
from app.services.trade_plans import upsert_trade_setup
from app.db.session import get_session_local
from app.schemas.async_task import AsyncTaskRead
from app.services.async_tasks import _set_task, _start_worker, create_async_task, get_async_task, list_async_tasks


PROXY_KEYS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]

# 跨线程保护 os.environ 修改的锁（RLock 允许同线程内嵌套调用，避免死锁）
_proxy_lock = RLock()


@contextmanager
def _proxy_bypass():
    with _proxy_lock:
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


def _is_bj_stock_code(code: str) -> bool:
    """根据代码前缀判断是否为北交所标的（920xxx 新代码 / 4xx 8xx 老代码）。"""
    return code.startswith(("920", "4", "8"))


def _cn_prefixed_symbol(symbol: Symbol) -> str:
    market = (symbol.market or "").lower()
    # 北交所标的用 "bj" 前缀（akshare 部分接口支持）
    # 同时检查代码前缀，防止 DB 中 market 字段过时（旧数据可能为 'sh'）
    if market == "bj" or (market in {"sh", "cn"} and _is_bj_stock_code(symbol.symbol)):
        return f"bj{symbol.symbol}"
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


def _safe_stock_zh_a_daily(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    """安全调用 stock_zh_a_daily，处理北交所空数据导致的 KeyError。

    新浪 stock_zh_a_daily 对无数据的北交所标的（老代码 430xxx/830xxx）返回空 dict_list，
    pd.DataFrame([]) 创建空 DataFrame，访问 'date' 列时触发 KeyError。
    这里在调用前 patch akshare 函数，捕获空数据并返回空 DataFrame。
    """
    from app.services.akshare_utils import call_akshare_with_retry
    try:
        return call_akshare_with_retry(
            ak.stock_zh_a_daily,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            api_key="stock_zh_a_daily",
            max_attempts=2,
        )
    except KeyError as e:
        if "date" in str(e):
            logger.info("stock_zh_a_daily returned empty data for %s (likely unsupported BJ code)", symbol)
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"])
        raise


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

    # P0 稳定性：仅尝试 1 轮（内层已有多数据源回退：cn-stock 3 源 / cn-etf 2 源）
    # 3 源 × 1 次 × 20s = 60s < SYNC_ONE_SYMBOL_TIMEOUT_SECONDS(90s)，留余量给评分计算
    # 风控加固：每个源用 call_akshare_with_retry 包装，max_attempts=2 给一次瞬时风控重试机会
    # 重试间隔由 base_delay*2^0=1s 提供，不会触发更严的风控升级
    from app.services.akshare_utils import call_akshare_with_retry
    last_error = None
    for attempt in range(1):
        try:
            with _proxy_bypass():
                with quiet_akshare_output():
                    if region == "cn" and symbol.asset_type == "stock":
                        # 北交所标的（market="bj"）：东财 stock_zh_a_hist 不支持，
                        # 优先用新浪 stock_zh_a_daily（已验证 bj920xxx 前缀可用）
                        # 注意：新浪对部分北交所标的（老代码 430xxx/830xxx）返回空数据会触发 KeyError，
                        # 需用 _safe_stock_zh_a_daily 包装处理空 DataFrame
                        if (symbol.market or "").lower() == "bj" or _is_bj_stock_code(symbol.symbol):
                            try:
                                return _normalize_cn_stock_sina_history(
                                    _safe_stock_zh_a_daily(
                                        _cn_prefixed_symbol(symbol),
                                        start, end, adjust,
                                    ),
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                            except Exception:
                                logger.info("AKShare sina source failed for BJ %s, trying tx fallback", symbol.symbol, exc_info=True)
                                return _normalize_cn_stock_tx_history(
                                    call_akshare_with_retry(
                                        ak.stock_zh_a_hist_tx,
                                        symbol=_cn_prefixed_symbol(symbol),
                                        start_date=start,
                                        end_date=end,
                                        adjust=adjust,
                                        api_key="stock_zh_a_hist_tx",
                                        max_attempts=2,
                                    ),
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                        # 沪深股票：东财 → 新浪 → 腾讯
                        try:
                            return _normalize_cn_em_history(
                                call_akshare_with_retry(
                                    ak.stock_zh_a_hist,
                                    symbol=symbol.symbol,
                                    period="daily",
                                    start_date=start,
                                    end_date=end,
                                    adjust=adjust,
                                    api_key="stock_zh_a_hist",
                                    max_attempts=2,
                                )
                            )
                        except Exception:
                            logger.info("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
                            try:
                                return _normalize_cn_stock_sina_history(
                                    call_akshare_with_retry(
                                        ak.stock_zh_a_daily,
                                        symbol=_cn_prefixed_symbol(symbol),
                                        start_date=start,
                                        end_date=end,
                                        adjust=adjust,
                                        api_key="stock_zh_a_daily",
                                        max_attempts=2,
                                    ),
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                            except Exception:
                                logger.info("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
                                return _normalize_cn_stock_tx_history(
                                    call_akshare_with_retry(
                                        ak.stock_zh_a_hist_tx,
                                        symbol=_cn_prefixed_symbol(symbol),
                                        start_date=start,
                                        end_date=end,
                                        adjust=adjust,
                                        api_key="stock_zh_a_hist_tx",
                                        max_attempts=2,
                                    ),
                                    start_date=start_date,
                                    end_date=end_date,
                                )
                    if region == "cn" and symbol.asset_type == "etf":
                        try:
                            return _normalize_cn_em_history(
                                call_akshare_with_retry(
                                    ak.fund_etf_hist_em,
                                    symbol=symbol.symbol,
                                    period="daily",
                                    start_date=start,
                                    end_date=end,
                                    adjust=adjust,
                                    api_key="fund_etf_hist_em",
                                    max_attempts=2,
                                )
                            )
                        except Exception:
                            logger.info("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
                            return _normalize_cn_etf_sina_history(
                                call_akshare_with_retry(
                                    ak.fund_etf_hist_sina,
                                    symbol=_cn_prefixed_symbol(symbol),
                                    api_key="fund_etf_hist_sina",
                                    max_attempts=2,
                                ),
                                start_date=start_date,
                                end_date=end_date,
                            )
                    if region == "us":
                        us_adjust = adjust if adjust in {"", "qfq"} else ""
                        frame = call_akshare_with_retry(
                            ak.stock_us_daily,
                            symbol=symbol.symbol,
                            adjust=us_adjust,
                            api_key="stock_us_daily",
                            max_attempts=2,
                        )
                        return _normalize_us_history(frame=frame, start_date=start_date, end_date=end_date)
        except Exception as exc:
            last_error = exc
            logger.info("AKShare source failed for %s, trying fallback", symbol.symbol, exc_info=True)
            continue
    if last_error is not None:
        logger.warning("All AKShare sources failed for %s", symbol.symbol, exc_info=True)
        raise last_error
    raise RuntimeError("AKShare fetch failed without an explicit exception")


def _normalize_trade_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.to_datetime(value).date()


def _dedupe_history_rows(frame: pd.DataFrame) -> list[dict]:
    """按 trade_date 去重，保留同日最后一条记录。"""
    deduped: dict[date, dict] = {}
    for raw_row in frame.to_dict(orient="records"):
        trade_date = _normalize_trade_date(raw_row["trade_date"])
        row = dict(raw_row)
        row["trade_date"] = trade_date
        deduped[trade_date] = row
    return list(deduped.values())

def _upsert_bars(db: Session, symbol: Symbol, frame: pd.DataFrame) -> tuple[int, int]:
    inserted = 0
    updated = 0

    rows = _dedupe_history_rows(frame)
    if not rows:
        return inserted, updated

    # 批量查询已存在的日期，避免行级 N+1
    trade_dates = [r["trade_date"] for r in rows]
    existing_bars = db.execute(
        select(DailyBar).where(
            DailyBar.symbol_id == symbol.id,
            DailyBar.trade_date.in_(trade_dates),
        )
    ).scalars().all()
    existing_map = {bar.trade_date: bar for bar in existing_bars}

    for row in rows:
        trade_date = row["trade_date"]
        existing = existing_map.get(trade_date)
        if existing is None:
            existing = DailyBar(symbol_id=symbol.id, trade_date=trade_date)
            db.add(existing)
            existing_map[trade_date] = existing
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

    # 网络请求前释放 DB 连接，避免 akshare 长时间请求期间连接被 MySQL 关闭
    # latest_bar 是只读查询，commit 安全；后续 _upsert_bars 会重新从池获取连接（触发 pre_ping）
    db.commit()

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
            # 单标的失败时回滚脏数据，避免混入后续 commit
            db.rollback()
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
HISTORY_INIT_STAGE_KEYS = ("prepare", "sync_bars", "calc_scores", "scan", "finalize")
HISTORY_INIT_TASK_TYPE = "history_initialization"
HISTORY_INIT_RECENT_LIMIT = 8
HISTORY_INIT_REPAIR_MODES = {"both", "bars", "scores"}
HISTORY_INIT_SYMBOL_SOURCES = {"all", "watchlist", "positions", "scored", "candidates", "cn-stock", "cn-etf"}
_HISTORY_INIT_LOCK = Lock()
_HISTORY_INIT_CANCEL_EVENT: Event = Event()
_HISTORY_INIT_TASK: dict = {
    "task_id": None,
    "status": "idle",
    "preset": "1y",
    "adjust": "qfq",
    "repair_mode": "both",
    "symbol_source": "all",
    "auto_scan": False,
    "portfolio_id": None,
    "watchlist_id": None,
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
        "scan_run_id": None,
        "scan_executable_count": 0,
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



def _history_parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _upsert_history_failure(
    task: dict,
    *,
    symbol: Symbol,
    stage: str,
    message: str,
    failed_days: int = 0,
    last_trade_date: date | None = None,
) -> None:
    items = task.setdefault("failed_items", [])
    for item in items:
        if int(item.get("symbol_id") or 0) == int(symbol.id) and item.get("stage") == stage:
            item["message"] = message
            item["name"] = symbol.name
            item["asset_type"] = symbol.asset_type
            item["symbol"] = symbol.symbol
            if failed_days:
                item["failed_days"] = int(item.get("failed_days") or 0) + int(failed_days)
            if last_trade_date is not None:
                item["last_trade_date"] = last_trade_date.isoformat()
            return
    items.append({
        "symbol_id": symbol.id,
        "symbol": symbol.symbol,
        "name": symbol.name,
        "asset_type": symbol.asset_type,
        "stage": stage,
        "message": message,
        "failed_days": int(failed_days or 0),
        "last_trade_date": last_trade_date.isoformat() if last_trade_date is not None else None,
    })

def _history_duration_seconds(started_at: str | None, finished_at: str | None = None) -> int | None:
    # 风控：started_at/finished_at 可能是 aware 或 naive datetime 字符串，
    # 直接 fromisoformat 后相减会抛 TypeError（aware - naive 不兼容）
    # 复用 _history_db_datetime 统一 normalize 为 naive UTC
    if not started_at:
        return None
    started_dt = _history_db_datetime(started_at)
    if started_dt is None:
        return None
    if finished_at:
        finished_dt = _history_db_datetime(finished_at)
        if finished_dt is None:
            return None
    else:
        finished_dt = datetime.now(timezone.utc).replace(tzinfo=None)
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
    if status == "cancelled":
        return "cancelled"
    if status == "running":
        return "running"
    return "queued"


def _history_active_stage(task: dict) -> dict | None:
    stages = task.get("stages") or []
    for stage in stages:
        if stage.get("status") == "running":
            return stage
    for stage in reversed(stages):
        if stage.get("status") in {"failed", "completed", "cancelled"}:
            return stage
    return stages[0] if stages else None


def _history_async_stage(task: dict) -> str:
    status = task.get("status")
    if status == "completed":
        return "done"
    if status == "failed":
        failed_stage = _history_active_stage(task)
        return (failed_stage or {}).get("key") or "failed"
    if status == "cancelled":
        cancelled_stage = _history_active_stage(task)
        return (cancelled_stage or {}).get("key") or "cancelled"
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
        elif task.status in {"running", "failed", "cancelled"}:
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
        "repair_mode": snapshot.get("repair_mode") or "both",
        "asset_types": deepcopy(snapshot.get("asset_types") or ["stock", "etf"]),
        "symbol_ids": deepcopy(snapshot.get("symbol_ids") or []),
        "start_date": snapshot.get("start_date"),
        "end_date": snapshot.get("end_date"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": snapshot.get("duration_seconds")
        or _history_duration_seconds(started_at, finished_at),
        "message": snapshot.get("message") or task.message,
        "stages": deepcopy(snapshot.get("stages") or []),
        "failed_items": deepcopy(snapshot.get("failed_items") or []),
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


def _load_local_score_dates(
    db: Session,
    symbol: Symbol,
    universe_symbol: UniverseSymbol | None,
    start_date: date,
    end_date: date,
) -> tuple[list[date], set[date]]:
    """Return merged local bar dates and the dates backed by business DailyBar rows."""
    daily_dates = set(
        db.execute(
            select(DailyBar.trade_date).where(
                DailyBar.symbol_id == symbol.id,
                DailyBar.trade_date >= start_date,
                DailyBar.trade_date <= end_date,
            )
        ).scalars().all()
    )
    universe_dates: set[date] = set()
    if universe_symbol is not None:
        universe_dates = set(
            db.execute(
                select(UniverseDailyBar.trade_date).where(
                    UniverseDailyBar.universe_symbol_id == universe_symbol.id,
                    UniverseDailyBar.trade_date >= start_date,
                    UniverseDailyBar.trade_date <= end_date,
                    UniverseDailyBar.close.is_not(None),
                )
            ).scalars().all()
        )
    return sorted(daily_dates | universe_dates), daily_dates


def _resolve_symbols_by_source(
    db: Session,
    *,
    source: str,
    asset_types: list[str],
    symbol_ids: list[int] | None = None,
    portfolio_id: int | None = None,
    watchlist_id: int | None = None,
) -> list[Symbol]:
    """根据来源类型解析历史初始化需要处理的标的列表。

    来源语义：
    - all: 全部 is_active=1 的标的（按 asset_types 过滤），默认行为
    - watchlist: 观察池标的（可选 watchlist_id 过滤）
    - positions: 持仓标的（可选 portfolio_id 过滤）
    - scored: 已有评分记录的标的
    - candidates: 最新一次扫描的 executable 候选
    - cn-stock: A 股标的（asset_type=stock + CN 市场）
    - cn-etf: CN ETF 标的（asset_type=etf + CN 市场）

    若 symbol_ids 非空，则在来源结果上再做 id 过滤（取交集）。
    """
    # symbol_ids 优先：显式指定时直接按 id 取，但仍受 is_active=1 约束
    if symbol_ids:
        stmt = select(Symbol).where(Symbol.is_active == 1, Symbol.id.in_(symbol_ids))
        if asset_types:
            stmt = stmt.where(Symbol.asset_type.in_(asset_types))
        return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()

    resolved_source = source if source in HISTORY_INIT_SYMBOL_SOURCES else "all"

    if resolved_source == "cn-stock":
        stmt = select(Symbol).where(Symbol.is_active == 1, Symbol.asset_type == "stock")
        cn_markets = markets_for_region("cn")
        if cn_markets:
            stmt = stmt.where(Symbol.market.in_(cn_markets))
        return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()

    if resolved_source == "cn-etf":
        stmt = select(Symbol).where(Symbol.is_active == 1, Symbol.asset_type == "etf")
        cn_markets = markets_for_region("cn")
        if cn_markets:
            stmt = stmt.where(Symbol.market.in_(cn_markets))
        return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()

    if resolved_source == "watchlist":
        stmt = (
            select(Symbol)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
        if watchlist_id is not None:
            stmt = stmt.where(WatchlistItem.watchlist_id == int(watchlist_id))
        if asset_types:
            stmt = stmt.where(Symbol.asset_type.in_(asset_types))
        return db.execute(stmt.distinct().order_by(Symbol.id.asc())).scalars().all()

    if resolved_source == "positions":
        stmt = (
            select(Symbol)
            .join(Position, Position.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
        if portfolio_id is not None:
            stmt = stmt.where(Position.portfolio_id == int(portfolio_id))
        if asset_types:
            stmt = stmt.where(Symbol.asset_type.in_(asset_types))
        return db.execute(stmt.distinct().order_by(Symbol.id.asc())).scalars().all()

    if resolved_source == "scored":
        stmt = (
            select(Symbol)
            .join(Score, Score.symbol_id == Symbol.id)
            .where(Symbol.is_active == 1)
        )
        if asset_types:
            stmt = stmt.where(Symbol.asset_type.in_(asset_types))
        return db.execute(stmt.distinct().order_by(Symbol.id.asc())).scalars().all()

    if resolved_source == "candidates":
        # 最新一次扫描的 executable 候选
        latest_run_id_row = (
            db.execute(
                select(ScanResult.scan_run_id)
                .where(ScanResult.result_type == "executable")
                .order_by(ScanResult.created_at.desc())
                .limit(1)
            )
            .first()
        )
        if latest_run_id_row is None:
            return []
        stmt = (
            select(Symbol)
            .join(ScanResult, ScanResult.symbol_id == Symbol.id)
            .where(
                Symbol.is_active == 1,
                ScanResult.scan_run_id == int(latest_run_id_row[0]),
                ScanResult.result_type == "executable",
            )
        )
        if asset_types:
            stmt = stmt.where(Symbol.asset_type.in_(asset_types))
        return db.execute(stmt.distinct().order_by(Symbol.id.asc())).scalars().all()

    # 默认 all
    stmt = select(Symbol).where(Symbol.is_active == 1)
    if asset_types:
        stmt = stmt.where(Symbol.asset_type.in_(asset_types))
    return db.execute(stmt.order_by(Symbol.id.asc())).scalars().all()


def _resolve_history_scan_refs(
    db: Session,
    portfolio_id: int | None,
) -> tuple[int | None, int | None]:
    """解析自动扫描所需的 portfolio_id 与 portfolio_rule_id。

    若未传入 portfolio_id，则回退到默认组合及其激活规则。
    """
    resolved_portfolio_id = portfolio_id
    resolved_rule_id: int | None = None
    if resolved_portfolio_id is None:
        default_portfolio = get_default_portfolio(db)
        if default_portfolio is not None:
            resolved_portfolio_id = default_portfolio.id
    if resolved_portfolio_id is not None:
        active_rule = get_active_rule(db, resolved_portfolio_id)
        if active_rule is not None:
            resolved_rule_id = active_rule.id
    return resolved_portfolio_id, resolved_rule_id


def start_history_initialization_task(
    preset: str = "1y",
    adjust: str = "qfq",
    asset_types: list[str] | None = None,
    symbol_ids: list[int] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    repair_mode: str = "both",
    symbol_source: str = "all",
    auto_scan: bool = False,
    portfolio_id: int | None = None,
    watchlist_id: int | None = None,
) -> dict:
    preset_key = preset if preset in HISTORY_INIT_PRESET_DAYS else "1y"
    resolved_end = end_date or date.today()
    resolved_start = start_date or (resolved_end - timedelta(days=HISTORY_INIT_PRESET_DAYS[preset_key]))
    resolved_asset_types = asset_types or ["stock", "etf"]
    resolved_symbol_ids = [int(item) for item in (symbol_ids or []) if item is not None]
    resolved_repair_mode = repair_mode if repair_mode in HISTORY_INIT_REPAIR_MODES else "both"
    resolved_source = symbol_source if symbol_source in HISTORY_INIT_SYMBOL_SOURCES else "all"
    resolved_portfolio_id = int(portfolio_id) if portfolio_id is not None else None
    resolved_watchlist_id = int(watchlist_id) if watchlist_id is not None else None

    with _HISTORY_INIT_LOCK:
        if _HISTORY_INIT_TASK.get("status") == "running":
            raise ValueError("A history initialization task is already running")
        _HISTORY_INIT_CANCEL_EVENT.clear()

    task = {
        "status": "running",
        "preset": preset_key,
        "adjust": adjust,
        "repair_mode": resolved_repair_mode,
        "symbol_source": resolved_source,
        "auto_scan": bool(auto_scan),
        "portfolio_id": resolved_portfolio_id,
        "watchlist_id": resolved_watchlist_id,
        "asset_types": resolved_asset_types,
        "symbol_ids": resolved_symbol_ids,
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
            "scan_run_id": None,
            "scan_executable_count": 0,
        },
        "failed_items": [],
    }
    prepare_stage = _history_stage(task, "prepare")
    if prepare_stage is not None:
        prepare_stage.update({"status": "running", "total": 1, "message": "Collecting symbols"})
    task_read = create_async_task(
        HISTORY_INIT_TASK_TYPE,
        {
            "preset": preset_key,
            "adjust": adjust,
            "repair_mode": resolved_repair_mode,
            "symbol_source": resolved_source,
            "auto_scan": bool(auto_scan),
            "portfolio_id": resolved_portfolio_id,
            "watchlist_id": resolved_watchlist_id,
            "asset_types": resolved_asset_types,
            "symbol_ids": resolved_symbol_ids,
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
    symbol_ids: list[int] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    repair_mode: str = "both",
    symbol_source: str = "all",
    auto_scan: bool = False,
    portfolio_id: int | None = None,
    watchlist_id: int | None = None,
) -> dict:
    task = start_history_initialization_task(
        preset=preset,
        adjust=adjust,
        asset_types=asset_types,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        repair_mode=repair_mode,
        symbol_source=symbol_source,
        auto_scan=auto_scan,
        portfolio_id=portfolio_id,
        watchlist_id=watchlist_id,
    )
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
        repair_mode = snapshot.get("repair_mode") or "both"
        asset_types = snapshot.get("asset_types") or ["stock", "etf"]
        symbol_ids = [int(item) for item in (snapshot.get("symbol_ids") or []) if item is not None]
        symbol_source = snapshot.get("symbol_source") or "all"
        auto_scan = bool(snapshot.get("auto_scan"))
        portfolio_id = snapshot.get("portfolio_id")
        watchlist_id = snapshot.get("watchlist_id")

        symbols = _resolve_symbols_by_source(
            db,
            source=symbol_source,
            asset_types=asset_types,
            symbol_ids=symbol_ids or None,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
        )

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
        if repair_mode == "scores":
            _update_history_stage(
                task_id,
                "sync_bars",
                status="completed",
                done=0,
                total=0,
                message="Skipped bar sync. Repair mode: scores only.",
            )
        else:
            _update_history_stage(
                task_id,
                "sync_bars",
                status="running",
                done=0,
                total=sync_total,
                message="Syncing historical bars",
            )
            for index, symbol in enumerate(symbols, start=1):
                if _HISTORY_INIT_CANCEL_EVENT.is_set():
                    cancel_history_initialization_task(force_task_id=task_id, from_worker=True)
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

                def _sync_summary(task: dict, sync_result: dict = result, current_symbol: Symbol = symbol) -> None:
                    summary = task["summary"]
                    summary["bars_rows"] += int(sync_result.get("rows") or 0)
                    if sync_result.get("status") == "ok":
                        summary["sync_ok_count"] += 1
                    elif sync_result.get("status") == "empty":
                        summary["empty_count"] += 1
                    else:
                        summary["sync_failed_count"] += 1
                        _upsert_history_failure(
                            task,
                            symbol=current_symbol,
                            stage="sync_bars",
                            message=str(sync_result.get("error") or sync_result.get("message") or "Bar sync failed"),
                        )
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

        if repair_mode == "bars":
            def _set_score_total(task: dict) -> None:
                task["summary"]["score_days_total"] = 0
            _mutate_history_task(task_id, _set_score_total)
            _update_history_stage(
                task_id,
                "calc_scores",
                status="completed",
                done=0,
                total=0,
                message="Skipped score calculation. Repair mode: bars only.",
            )
        else:
            symbol_id_list = [symbol.id for symbol in symbols]
            universe_symbols = db.execute(
                select(UniverseSymbol).where(UniverseSymbol.symbol.in_([symbol.symbol for symbol in symbols]))
            ).scalars().all() if symbols else []
            universe_by_code = {item.symbol: item for item in universe_symbols}
            if symbol_id_list:
                daily_bar_counts = {
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
                universe_bar_counts_by_id = {
                    int(universe_id): int(total)
                    for universe_id, total in db.execute(
                        select(UniverseDailyBar.universe_symbol_id, func.count(UniverseDailyBar.id))
                        .where(
                            UniverseDailyBar.trade_date >= start_date,
                            UniverseDailyBar.trade_date <= end_date,
                            UniverseDailyBar.universe_symbol_id.in_([item.id for item in universe_symbols]),
                            UniverseDailyBar.close.is_not(None),
                        )
                        .group_by(UniverseDailyBar.universe_symbol_id)
                    ).all()
                } if universe_symbols else {}
                bar_counts = {
                    symbol.id: max(
                        daily_bar_counts.get(symbol.id, 0),
                        universe_bar_counts_by_id.get(universe_by_code[symbol.symbol].id, 0)
                        if symbol.symbol in universe_by_code else 0,
                    )
                    for symbol in symbols
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
                    cancel_history_initialization_task(force_task_id=task_id, from_worker=True)
                    return
                universe_symbol = universe_by_code.get(symbol.symbol)
                trade_dates, daily_dates = _load_local_score_dates(
                    db,
                    symbol,
                    universe_symbol,
                    start_date,
                    end_date,
                )
                if not trade_dates:
                    continue
                symbol_failed = False
                score_failed_days = 0
                last_failed_trade_date = None
                last_score_error = None
                for trade_date in trade_dates:
                    try:
                        if trade_date in daily_dates:
                            calculate_symbol_score(db=db, symbol=symbol, trade_date=trade_date)
                        elif universe_symbol is not None:
                            calculate_universe_symbol_score(
                                db,
                                universe_symbol,
                                symbol,
                                trade_date,
                            )
                        score_done += 1
                    except Exception as exc:
                        db.rollback()
                        logger.warning("History init score calc failed for %s on %s: %s", symbol.symbol, trade_date, exc)
                        symbol_failed = True
                        score_failed_days += 1
                        last_failed_trade_date = trade_date
                        last_score_error = str(exc)
                if symbol_failed:
                    def _score_failure(task: dict, current_symbol: Symbol = symbol, failed_days: int = score_failed_days, failed_date=last_failed_trade_date, error_message: str | None = last_score_error) -> None:
                        _upsert_history_failure(
                            task,
                            symbol=current_symbol,
                            stage="calc_scores",
                            message=error_message or "Score calculation failed",
                            failed_days=failed_days,
                            last_trade_date=failed_date,
                        )
                    _mutate_history_task(task_id, _score_failure)
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

        # 扫描阶段：可选，auto_scan=true 时触发候选池扫描
        if auto_scan:
            if _HISTORY_INIT_CANCEL_EVENT.is_set():
                cancel_history_initialization_task(force_task_id=task_id, from_worker=True)
                return
            _update_history_stage(
                task_id,
                "scan",
                status="running",
                done=0,
                total=1,
                message="Running candidate scan",
            )
            try:
                synced_symbol_ids = [s.id for s in symbols]
                resolved_portfolio_id, resolved_rule_id = _resolve_history_scan_refs(db, portfolio_id)
                scan_run = run_scan(
                    db=db,
                    scope_snapshot={"symbol_ids": synced_symbol_ids},
                    filters_snapshot={
                        "source": "history_initialization",
                        "task_id": task_id,
                        "symbol_source": symbol_source,
                    },
                    portfolio_id=resolved_portfolio_id,
                    portfolio_rule_id=resolved_rule_id,
                    run_name="history-initialization-scan",
                    preset_id=None,
                )
                executable_count = (
                    db.execute(
                        select(func.count(ScanResult.id)).where(
                            ScanResult.scan_run_id == scan_run.id,
                            ScanResult.result_type == "executable",
                        )
                    ).scalar_one()
                    or 0
                )

                def _scan_summary(task: dict, run_id: int = scan_run.id, exec_count: int = int(executable_count)) -> None:
                    task["summary"]["scan_run_id"] = run_id
                    task["summary"]["scan_executable_count"] = exec_count
                _mutate_history_task(task_id, _scan_summary)
                _update_history_stage(
                    task_id,
                    "scan",
                    status="completed",
                    done=1,
                    total=1,
                    message=f"Scan complete: {int(executable_count)} executable candidates",
                )
            except Exception as scan_exc:
                db.rollback()
                logger.warning("History init auto scan failed: %s", scan_exc, exc_info=True)
                _update_history_stage(
                    task_id,
                    "scan",
                    status="completed",
                    done=1,
                    total=1,
                    message=f"Scan skipped: {scan_exc}",
                )
        else:
            _update_history_stage(
                task_id,
                "scan",
                status="completed",
                done=0,
                total=0,
                message="Scan skipped. auto_scan disabled.",
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
def cancel_history_initialization_task(force_task_id: str | None = None, from_worker: bool = False) -> dict:
    """Cancel the current history initialization task."""
    from app.services.async_tasks import cancel_async_task

    with _HISTORY_INIT_LOCK:
        task = _HISTORY_INIT_TASK
        task_id = force_task_id or task.get("task_id")
        if task_id is None:
            raise ValueError("No running history initialization task to cancel")
        if not from_worker and task.get("status") != "running":
            raise ValueError("No running history initialization task to cancel")
        _HISTORY_INIT_CANCEL_EVENT.set()

    if task_id:
        try:
            cancel_async_task(task_id)
        except ValueError:
            logger.warning("Async task %s not found for cancel", task_id)

    def _apply(task: dict) -> None:
        task["status"] = "cancelled"
        task["message"] = "Task cancelled by user"
        task["finished_at"] = _now_iso()
        for key in reversed(HISTORY_INIT_STAGE_KEYS):
            stage = _history_stage(task, key)
            if stage is not None and stage.get("status") == "running":
                stage["status"] = "cancelled"
                stage["message"] = "Cancelled by user"
                break
        _recompute_history_progress(task)

    return _mutate_history_task(task_id, _apply)


def retry_history_initialization_failed_items(source_task_id: str) -> dict:
    source_task = get_async_task(source_task_id)
    if source_task is None:
        raise ValueError("History initialization task not found")

    snapshot = deepcopy(source_task.result or {})
    failed_items = snapshot.get("failed_items") or []
    symbol_ids = sorted({int(item.get("symbol_id")) for item in failed_items if item.get("symbol_id") is not None})
    if not symbol_ids:
        raise ValueError("No failed items to retry")

    failed_stages = {str(item.get("stage")) for item in failed_items if item.get("stage")}
    repair_mode = snapshot.get("repair_mode") or "both"
    if failed_stages == {"sync_bars"}:
        repair_mode = "bars"
    elif failed_stages == {"calc_scores"}:
        repair_mode = "scores"

    start_date = _history_parse_date(snapshot.get("start_date"))
    end_date = _history_parse_date(snapshot.get("end_date"))
    return create_history_initialization_task(
        preset=snapshot.get("preset") or "1y",
        adjust=snapshot.get("adjust") or "qfq",
        asset_types=snapshot.get("asset_types") or ["stock", "etf"],
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        repair_mode=repair_mode,
        symbol_source=snapshot.get("symbol_source") or "all",
        auto_scan=bool(snapshot.get("auto_scan")),
        portfolio_id=snapshot.get("portfolio_id"),
        watchlist_id=snapshot.get("watchlist_id"),
    )
def cleanup_history_records(keep: int = 5) -> dict:
    """Cleanup history initialization records and keep the latest N runs."""
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
