from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import logging
import os
import time

import akshare as ak
import pandas as pd
from sqlalchemy import select
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
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "换手率": "turnover_rate",
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
            "start_date": resolved_start.isoformat(),
            "end_date": resolved_end.isoformat(),
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
        "start_date": resolved_start.isoformat(),
        "end_date": resolved_end.isoformat(),
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
                        "trade_date": latest_bar.trade_date.isoformat(),
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
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
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
