"""P2：资金流数据服务（主力/超大单/大单/中单/小单 净流入 + 北向资金）。

数据源：akshare
- ak.stock_individual_fund_flow(stock, market)：个股资金流（日频）
- ak.stock_hsgt_north_net_flow_in(symbol="北上")：北向资金每日净流入

设计要点：
1. sync_symbol_capital_flow：拉取并写入当日资金流快照
2. sync_northbound_flow：拉取北向资金市场层面数据
3. calc_main_net_inflow_score：将主力净流入转换为 0-100 评分
4. 评分时按需拉取：calculate_symbol_score_with_config 调用 get_or_sync_capital_flow
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import desc, select, union_all
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError, PendingRollbackError, SQLAlchemyError

from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.symbol import Symbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
from app.services.market_data import _proxy_bypass
from app.services.regions import region_from_market
from app.services.capital_flow_history_provider import fetch_capital_flow_history
from app.services.data_source_execution import source_task

logger = logging.getLogger(__name__)


class FundFlowProviderError(RuntimeError):
    """资金流请求本身失败（网络/接口变更），与"provider 没返回这个标的数据"不同。"""


class _SyncedSymbolIds(set):
    def __init__(self, values=(), *, trade_dates=()):
        super().__init__(values)
        self.trade_dates = set(trade_dates)

# 资金流数据新鲜度阈值（天）
_FLOW_FRESHNESS_DAYS = 3
# 评分时近 N 日均值窗口
_FLOW_LOOKBACK_DAYS = 10
# 少于这个可比样本数就不出分（2 样本时 z 恒为 ±1，见 calc_main_net_inflow_score）
_FLOW_MIN_SCORE_SAMPLES = 5


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _market_code_for_akshare(symbol: Symbol) -> str:
    import re
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(symbol.symbol))
    return match.group(1) if match else str(symbol.symbol).strip()


def _market_prefix(symbol: Symbol) -> str:
    """stock_individual_fund_flow 需要的市场前缀（sh/sz/bj）。"""
    market = str(symbol.market or "").lower()
    if market in {"sh", "sz", "bj"}:
        return market
    code = _market_code_for_akshare(symbol)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "sh"


def _fetch_individual_fund_flow_history(
    db: Session,
    symbol: Symbol,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Fetch the provider's real daily fund-flow rows without inventing gaps.

    AKShare currently returns a bounded recent history (usually about 100
    trading days).  Callers receive only rows that the provider actually
    returned; a requested date range is a filter, never a fill policy.
    """
    code = _market_code_for_akshare(symbol)
    market = _market_prefix(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            # The dated adapter owns its attempts, cooldown and elapsed budget.
            df = call_akshare_with_retry(fetch_capital_flow_history, stock=code, market=market, api_key="stock_individual_fund_flow", db=db, max_attempts=1)
        if df is None or df.empty:
            return []
        # 列名：日期、收盘价、涨跌幅、主力净流入-净额、主力净流入-净占比、
        #       超大单净流入-净额、超大单净流入-净占比、大单...、中单...、小单...
        df["日期"] = pd.to_datetime(df["日期"]).dt.date
        eligible = df
        if start_date is not None:
            eligible = eligible[eligible["日期"] >= start_date]
        if end_date is not None:
            eligible = eligible[eligible["日期"] <= end_date]
        return [
            {
                "trade_date": row["日期"],
                "_history_complete": row["日期"] < date.today(),
                "main_net_inflow": _safe_float(row.get("主力净流入-净额")),
                "main_net_inflow_pct": _safe_float(row.get("主力净流入-净占比")),
                "super_large_net_inflow": _safe_float(row.get("超大单净流入-净额")),
                "large_net_inflow": _safe_float(row.get("大单净流入-净额")),
                "medium_net_inflow": _safe_float(row.get("中单净流入-净额")),
                "small_net_inflow": _safe_float(row.get("小单净流入-净额")),
            }
            for _, row in eligible.sort_values("日期").iterrows()
        ]
    except Exception as exc:
        # 请求失败不能塌成"这个标的没有数据"：回填验收要靠 failed 与 provider 空返回
        # 分得开，否则网络被掐时整市场爬取会跑成"done，但全是 skipped"。
        logger.warning(
            "stock_individual_fund_flow request failed for %s: %s", symbol.symbol, exc
        )
        raise FundFlowProviderError(
            f"{symbol.symbol}: {type(exc).__name__}: {exc}"
        ) from exc


def calc_main_net_inflow_score(
    main_net_inflow: float | None,
    history_inflows: list[float],
) -> float | None:
    """将主力净流入转换为 0-100 评分。

    逻辑：
    - 当日净流入相对于近 N 日均值的偏离程度
    - 净流入为正且高于均值 → 高分
    - 净流入为负且低于均值 → 低分
    - 评分基于 z-score 映射到 [20, 95]

    Args:
        main_net_inflow: 当日主力净流入（元）
        history_inflows: 近 N 日主力净流入列表（含当日）
    """
    if main_net_inflow is None:
        return None
    if len(history_inflows) < _FLOW_MIN_SCORE_SAMPLES:
        # 样本太少时 z-score 不是" noisy"，而是**没有信息**：只有 2 个样本时
        # (x-avg)/std 恒等于 ±1，得分只能是 75 或 45，与净流入的量级无关
        # （2026-10-01 实测 000048 种子序列第二行就是 75.0 整）。
        # 消费方（评分维度）拿到 None 会走"无数据"分支。
        return None

    # 基于历史均值的标准化
    avg = sum(history_inflows) / len(history_inflows)
    std = (sum((x - avg) ** 2 for x in history_inflows) / len(history_inflows)) ** 0.5
    if std == 0:
        z = 0.0
    else:
        z = (main_net_inflow - avg) / std
    # z-score 映射到 0-100：z=0 → 60，z=+2 → 90，z=-2 → 30
    score = 60 + z * 15
    return max(20.0, min(95.0, score))


def _upsert_capital_flow(
    db: Session,
    symbol: Symbol,
    flow_data: dict[str, Any],
    *, prefetched=None, flush: bool = True, fill_missing_only: bool = False,
) -> CapitalFlow | None:
    """Persist one provider row and compute its score from real prior rows."""
    actual_date = flow_data.get("trade_date")
    if not isinstance(actual_date, date):
        return None
    main_net_inflow = _safe_float(flow_data.get("main_net_inflow"))
    if main_net_inflow is None and not fill_missing_only:
        return None

    history_rows = prefetched[1] if prefetched is not None else db.execute(
        select(CapitalFlow.main_net_inflow)
        .where(
            CapitalFlow.symbol_id == symbol.id,
            CapitalFlow.trade_date < actual_date,
        )
        .order_by(desc(CapitalFlow.trade_date))
        .limit(_FLOW_LOOKBACK_DAYS - 1)
    ).scalars().all()
    history_inflows = [main_net_inflow] + [
        value for value in history_rows if value is not None
    ]
    score = calc_main_net_inflow_score(main_net_inflow, history_inflows)

    existing = prefetched[0] if prefetched is not None else db.execute(
        select(CapitalFlow).where(
            CapitalFlow.symbol_id == symbol.id,
            CapitalFlow.trade_date == actual_date,
        )
    ).scalars().first()
    if existing is None:
        existing = CapitalFlow(symbol_id=symbol.id, trade_date=actual_date)
        db.add(existing)
        was_existing = False
    else:
        was_existing = True
    if fill_missing_only:
        from app.services.sync_write_policy import fill_missing_values, record_missing_fill

        if main_net_inflow is None and existing.main_net_inflow is None:
            if not was_existing:
                db.expunge(existing)
            return None
        filled = fill_missing_values(existing, {field: _safe_float(flow_data.get(field)) for field in
            ("main_net_inflow", "super_large_net_inflow", "large_net_inflow",
             "medium_net_inflow", "small_net_inflow", "main_net_inflow_pct")})
        retained_history = [existing.main_net_inflow] + [value for value in history_rows if value is not None]
        filled += fill_missing_values(existing, {"main_net_inflow_score":
            calc_main_net_inflow_score(existing.main_net_inflow, retained_history)})
        if not filled:
            return None
        if was_existing:
            record_missing_fill(existing, filled, flow_data, "akshare")
        else:
            existing.source = "akshare"
            existing.raw_json = json.dumps(flow_data, ensure_ascii=False, default=str)
        if flush:
            db.flush()
        return existing
    existing.main_net_inflow = main_net_inflow
    for field in ("super_large_net_inflow", "large_net_inflow", "medium_net_inflow", "small_net_inflow", "main_net_inflow_pct"):
        value = _safe_float(flow_data.get(field))
        if value is not None:
            setattr(existing, field, value)
    existing.main_net_inflow_score = score
    existing.source = "akshare"
    existing.raw_json = json.dumps(flow_data, ensure_ascii=False, default=str)
    if flush:
        db.flush()
    return existing


@source_task(lane='backfill')
def sync_symbol_capital_flow_range(
    db: Session,
    symbol: Symbol,
    *,
    start_date: date,
    end_date: date,
    fill_missing_only: bool = False,
    allowed_dates: set[date] | None = None,
    recover_connection: bool = False,
) -> int:
    """Upsert all real fund-flow rows in a requested bounded date range."""
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if end_date > date.today():
        raise ValueError("资金流结束日期不能晚于今天")
    if symbol.asset_type != "stock" or region_from_market(symbol.market) != "cn":
        return 0
    from app.services.sync_write_policy import DateWriteCount
    if allowed_dates is not None and not any(start_date <= day <= end_date for day in allowed_dates):
        return DateWriteCount(0, [])
    symbol_id = symbol.id
    rows = _fetch_individual_fund_flow_history(
        db, symbol, start_date=start_date, end_date=end_date
    )
    # Only the async worker opts in: it commits the partition boundary before
    # fetching, so rolling back cannot discard unrelated caller changes.
    for attempt in range(2):
        try:
            current = db.get(Symbol, symbol_id)
            written_dates = []
            for row in rows:
                if (start_date <= row["trade_date"] <= end_date
                        and (allowed_dates is None or row["trade_date"] in allowed_dates)
                        and _upsert_capital_flow(db, current, row, fill_missing_only=fill_missing_only) is not None):
                    written_dates.append(row["trade_date"])
            result = DateWriteCount(len(written_dates), written_dates)
            result.db_recoveries = attempt
            return result
        except (DBAPIError, PendingRollbackError) as exc:
            if (not recover_connection or attempt or
                    isinstance(exc, DBAPIError) and not exc.connection_invalidated):
                raise
            logger.warning("Recovering disconnected fund-flow transaction for symbol id=%s; reusing fetched rows", symbol_id)
            try:
                db.rollback()
            except SQLAlchemyError:
                # A failed DBAPI rollback may still leave a logical transaction
                # to close. Do not expunge the worker's remaining symbol/partition
                # objects with Session.invalidate(); finish rollback once more.
                db.rollback()


_RANK_FIELD_MAP = {
    "main_net_inflow": "今日主力净流入-净额",
    "main_net_inflow_pct": "今日主力净流入-净占比",
    "super_large_net_inflow": "今日超大单净流入-净额",
    "large_net_inflow": "今日大单净流入-净额",
    "medium_net_inflow": "今日中单净流入-净额",
    "small_net_inflow": "今日小单净流入-净额",
}


def _fetch_market_fund_flow_rank(db: Session) -> list[dict[str, Any]]:
    """拉取全市场当日主力资金流排行（provider 分页，请求数与标的数无关）。"""
    with _proxy_bypass(), quiet_akshare_output():
        frame = call_akshare_with_retry(
            ak.stock_individual_fund_flow_rank,
            indicator="今日",
            api_key="stock_individual_fund_flow_rank",
            db=db,
        )
    if frame is None or frame.empty or "代码" not in frame.columns:
        raise RuntimeError("market fund flow ranking returned no usable rows")
    rows: list[dict[str, Any]] = []
    for _, frame_row in frame.iterrows():
        code = str(frame_row.get("代码") or "").split(".")[-1]
        if not code:
            continue
        row: dict[str, Any] = {"symbol_code": code.zfill(6)}
        for field, column in _RANK_FIELD_MAP.items():
            row[field] = _safe_float(frame_row.get(column))
        rows.append(row)
    return rows


def _corroborate_trade_date(
    db: Session, symbols: list[Symbol], requested_date: date
) -> date | None:
    """Return the session the provider is actually reporting, or None.

    The ranking endpoint has no date column: on a weekend or holiday it shows
    the previous session. The dated per-symbol endpoint is the only truth
    source for which session that is, so the snapshot is stamped with it
    instead of with the calendar day (which would invent non-trading dates).
    """
    for symbol in symbols[:3]:
        rows = _fetch_individual_fund_flow_history(db, symbol, end_date=requested_date)
        if rows:
            latest = rows[-1].get("trade_date")
            if isinstance(latest, date):
                return latest
    return None


@source_task(lane='incremental')
def sync_market_capital_flow_snapshot(
    db: Session,
    symbols: list[Symbol],
    trade_date: date | None = None,
    *, should_cancel=None, fill_missing_only: bool = False,
) -> set[int]:
    """Fetch selected current quotes in batches, writing only the requested day."""
    from app.services.capital_flow_provider import BATCH_SIZE, fetch_capital_flow_quotes
    from app.services.market_session import is_cn_trading_day
    requested_date = trade_date or date.today()
    if requested_date != date.today():
        raise ValueError("资金流排行仅提供当前快照；历史日期请使用历史回填接口")
    if not is_cn_trading_day(db, requested_date):
        return _SyncedSymbolIds()
    eligible = [
        symbol for symbol in symbols
        if symbol.asset_type == "stock" and region_from_market(symbol.market) == "cn"
    ]
    if not eligible:
        return set()
    synced: set[int] = set()
    for offset in range(0, len(eligible), BATCH_SIZE):
        batch = eligible[offset:offset + BATCH_SIZE]
        secids = [f"{'1' if _market_prefix(s) == 'sh' else '0'}.{_market_code_for_akshare(s)}" for s in batch]
        quotes = fetch_capital_flow_quotes(db, secids, should_cancel=should_cancel)
        current_ids = list(dict.fromkeys(s.id for s in batch
            if (quotes.get(_market_code_for_akshare(s)) or {}).get("trade_date") == requested_date))
        if not current_ids:
            continue
        existing = {row.symbol_id: row for row in db.execute(select(CapitalFlow).where(
            CapitalFlow.symbol_id.in_(current_ids), CapitalFlow.trade_date == requested_date)).scalars()}
        # Each indexed range returns at most nine prior rows. Combining the
        # bounded selects keeps one batch read without MySQL 8 window functions
        # or loading every symbol's entire history into Python.
        prior_ranges = [select(CapitalFlow.symbol_id, CapitalFlow.main_net_inflow, CapitalFlow.trade_date)
            .where(CapitalFlow.symbol_id == symbol_id, CapitalFlow.trade_date < requested_date)
            .order_by(CapitalFlow.trade_date.desc()).limit(_FLOW_LOOKBACK_DAYS - 1).subquery()
            for symbol_id in current_ids]
        recent = union_all(*(select(part.c.symbol_id, part.c.main_net_inflow, part.c.trade_date)
                              for part in prior_ranges)).subquery()
        history = {}
        for symbol_id, inflow in db.execute(select(recent.c.symbol_id, recent.c.main_net_inflow)
            .order_by(recent.c.symbol_id, recent.c.trade_date.desc())):
            history.setdefault(symbol_id, []).append(inflow)
        for symbol in batch:
            row = quotes.get(_market_code_for_akshare(symbol))
            if row is None or row.get("trade_date") != requested_date:
                continue
            item = _upsert_capital_flow(db, symbol, row,
                prefetched=(existing.get(symbol.id), history.get(symbol.id, [])), flush=False,
                fill_missing_only=fill_missing_only)
            if item is not None:
                existing[symbol.id] = item
                synced.add(symbol.id)
        db.flush()
    db.flush()
    return _SyncedSymbolIds(synced, trade_dates={requested_date} if synced else ())


@source_task(lane='incremental')
def sync_symbol_capital_flow(db: Session, symbol: Symbol, trade_date: date | None = None, *,
                             fill_missing_only: bool = False) -> CapitalFlow | None:
    """Sync one requested day; history is available only through explicit backfill."""
    if symbol.asset_type != "stock":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    target_date = trade_date or date.today()
    if target_date > date.today():
        raise ValueError("资金流日期不能晚于今天")
    if target_date == date.today():
        synced = sync_market_capital_flow_snapshot(db, [symbol], target_date,
            **({"fill_missing_only": True} if fill_missing_only else {}))
        if symbol.id not in synced:
            return None
        item = db.execute(select(CapitalFlow).where(CapitalFlow.symbol_id == symbol.id,
                                                   CapitalFlow.trade_date == target_date)).scalars().first()
        item.sync_rows_written = 1
        item.sync_written_dates = [target_date]
        return item
    rows = _fetch_individual_fund_flow_history(
        db, symbol, start_date=target_date, end_date=target_date
    )
    if not rows:
        logger.info("No fund flow data for %s, skip", symbol.symbol)
        return None

    written: CapitalFlow | None = None
    saved_dates: list[date] = []
    for row in rows:
        if row.get("trade_date") != target_date:
            continue
        saved = _upsert_capital_flow(db, symbol, row, fill_missing_only=fill_missing_only)
        if saved is not None:
            written = saved
            saved_dates.append(saved.trade_date)
    if written is None:
        logger.info("No usable main net inflow rows for %s, skip", symbol.symbol)
    else:
        written.sync_rows_written = len(saved_dates)
        written.sync_written_dates = saved_dates
    return written


def get_latest_capital_flow(db: Session, symbol_id: int, *, as_of: date | None = None) -> CapitalFlow | None:
    stmt = select(CapitalFlow).where(CapitalFlow.symbol_id == symbol_id)
    if as_of is not None:
        stmt = stmt.where(CapitalFlow.trade_date <= as_of)
    return db.execute(
        stmt
        .order_by(desc(CapitalFlow.trade_date))
        .limit(1)
    ).scalars().first()


def get_or_sync_capital_flow(db: Session, symbol: Symbol, trade_date: date) -> CapitalFlow | None:
    """评分时按需拉取：若 DB 无最新资金流或数据过期，触发同步。"""
    if symbol.asset_type != "stock":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    latest = get_latest_capital_flow(db, symbol.id, as_of=trade_date)
    if latest is not None:
        days_stale = (trade_date - latest.trade_date).days if hasattr(latest.trade_date, "year") else 0
        # 分值为 None 现在是一个合法状态（可比历史不足时不给分），不能再拿它当
        # "上次同步失败"的信号触发重拉，否则每次打分都会打外网。
        if 0 <= days_stale <= _FLOW_FRESHNESS_DAYS:
            return latest

    try:
        return sync_symbol_capital_flow(db, symbol, trade_date)
    except Exception as exc:
        logger.warning("On-demand capital flow sync failed for %s: %s", symbol.symbol, exc)
        return latest


def get_main_net_inflow_score(db: Session, symbol: Symbol, trade_date: date) -> float | None:
    """Read the score from current inputs; a saved score can predate repairs."""
    flow = get_or_sync_capital_flow(db, symbol, trade_date)
    if flow is None or flow.main_net_inflow is None:
        return None
    history = db.execute(
        select(CapitalFlow.main_net_inflow)
        .where(CapitalFlow.symbol_id == symbol.id, CapitalFlow.trade_date < flow.trade_date)
        .order_by(desc(CapitalFlow.trade_date))
        .limit(_FLOW_LOOKBACK_DAYS - 1)
    ).scalars().all()
    # Historical imports can correct old inputs or intentionally clear the
    # stored score. Recompute on read without rewriting source observations
    # or already published score/model snapshots.
    return calc_main_net_inflow_score(
        flow.main_net_inflow, [flow.main_net_inflow] + [value for value in history if value is not None]
    )


# ----------------------------------------------------------------------------
# 北向资金（市场层面）
# ----------------------------------------------------------------------------

def northbound_capability() -> dict[str, Any]:
    available = callable(getattr(ak, "stock_hsgt_north_net_flow_in", None))
    return {
        "available": available,
        "api_key": "stock_hsgt_north_net_flow_in",
        "reason": "" if available else "当前数据源版本不提供北向每日净流入接口；请接入可用数据源后同步。",
    }


@source_task(lane='incremental')
def sync_northbound_flow(db: Session, days: int = 30, *, start_date: date | None = None,
                         end_date: date | None = None, fill_missing_only: bool = False) -> int:
    """同步近 N 日北向资金净流入。

    Returns:
        写入的记录数
    """
    capability = northbound_capability()
    if not capability["available"]:
        raise FundFlowProviderError(capability["reason"])
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("northbound start_date must be on or before end_date")
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.stock_hsgt_north_net_flow_in, symbol="北上", api_key="stock_hsgt_north_net_flow_in", db=db)
        if df is None or df.empty:
            return 0
        # 列名：日期、当日净流入（元）、当日余额（元）
        df["日期"] = pd.to_datetime(df["日期"]).dt.date
        recent = df
        if start_date is not None:
            recent = recent[recent["日期"] >= start_date]
        if end_date is not None:
            recent = recent[recent["日期"] <= end_date]
        recent = recent.sort_values("日期", ascending=False).head(days)
        count = 0
        for _, row in recent.iterrows():
            trade_date = row["日期"]
            net = _safe_float(row.get("当日净流入"))
            if fill_missing_only and net is None:
                continue
            existing = db.execute(
                select(NorthboundFlow).where(NorthboundFlow.trade_date == trade_date)
            ).scalars().first()
            if existing is None:
                db.add(NorthboundFlow(
                    trade_date=trade_date,
                    total_net_inflow=net,
                    source="akshare",
                ))
            else:
                if fill_missing_only and (existing.total_net_inflow is not None or net is None):
                    continue
                existing.total_net_inflow = net
            count += 1
        db.flush()
        return count
    except Exception as exc:
        logger.warning("sync_northbound_flow failed: %s", exc)
        raise FundFlowProviderError(f"northbound: {type(exc).__name__}: {exc}") from exc


def get_latest_northbound_flow(db: Session) -> NorthboundFlow | None:
    return db.execute(
        select(NorthboundFlow)
        .order_by(desc(NorthboundFlow.trade_date))
        .limit(1)
    ).scalars().first()
