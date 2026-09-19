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
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.capital_flow import CapitalFlow, NorthboundFlow
from app.models.symbol import Symbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
from app.services.market_data import _proxy_bypass
from app.services.regions import region_from_market

logger = logging.getLogger(__name__)

# 资金流数据新鲜度阈值（天）
_FLOW_FRESHNESS_DAYS = 3
# 评分时近 N 日均值窗口
_FLOW_LOOKBACK_DAYS = 10


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
    return symbol.symbol.split(".")[-1] if "." in symbol.symbol else symbol.symbol


def _market_prefix(symbol: Symbol) -> str:
    """stock_individual_fund_flow 需要的市场前缀（sh/sz/bj）。"""
    code = _market_code_for_akshare(symbol)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8")):
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
            df = call_akshare_with_retry(ak.stock_individual_fund_flow, stock=code, market=market, api_key="stock_individual_fund_flow", db=db)
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
        logger.debug("stock_individual_fund_flow failed for %s: %s", symbol.symbol, exc)
        return []


def _fetch_individual_fund_flow(
    db: Session, symbol: Symbol, trade_date: date
) -> dict[str, Any]:
    """Backward-compatible latest-row adapter for scoring callers."""
    rows = _fetch_individual_fund_flow_history(
        db, symbol, end_date=trade_date
    )
    if not rows:
        return {}
    return rows[-1]


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
    if not history_inflows or len(history_inflows) < 2:
        # 无历史对比，仅按当日正负评分
        if main_net_inflow > 0:
            return 65.0
        if main_net_inflow < 0:
            return 40.0
        return 50.0

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
) -> CapitalFlow | None:
    """Persist one provider row and compute its score from real prior rows."""
    actual_date = flow_data.get("trade_date")
    if not isinstance(actual_date, date):
        return None
    main_net_inflow = flow_data.get("main_net_inflow")
    if main_net_inflow is None:
        return None

    history_rows = db.execute(
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

    existing = db.execute(
        select(CapitalFlow).where(
            CapitalFlow.symbol_id == symbol.id,
            CapitalFlow.trade_date == actual_date,
        )
    ).scalars().first()
    if existing is None:
        existing = CapitalFlow(symbol_id=symbol.id, trade_date=actual_date)
        db.add(existing)
    existing.main_net_inflow = main_net_inflow
    existing.super_large_net_inflow = flow_data.get("super_large_net_inflow")
    existing.large_net_inflow = flow_data.get("large_net_inflow")
    existing.medium_net_inflow = flow_data.get("medium_net_inflow")
    existing.small_net_inflow = flow_data.get("small_net_inflow")
    existing.main_net_inflow_pct = flow_data.get("main_net_inflow_pct")
    existing.main_net_inflow_score = score
    existing.source = "akshare"
    existing.raw_json = json.dumps(flow_data, ensure_ascii=False, default=str)
    db.flush()
    return existing


def sync_symbol_capital_flow_range(
    db: Session,
    symbol: Symbol,
    *,
    start_date: date,
    end_date: date,
) -> int:
    """Upsert all real fund-flow rows in a requested bounded date range."""
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if symbol.asset_type != "stock" or region_from_market(symbol.market) != "cn":
        return 0
    rows = _fetch_individual_fund_flow_history(
        db, symbol, start_date=start_date, end_date=end_date
    )
    written = 0
    for row in rows:
        if _upsert_capital_flow(db, symbol, row) is not None:
            written += 1
    return written


def sync_symbol_capital_flow(db: Session, symbol: Symbol, trade_date: date | None = None) -> CapitalFlow | None:
    """同步单个 symbol 的最新可用资金流快照。"""
    if symbol.asset_type != "stock":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    target_date = trade_date or date.today()
    flow_data = _fetch_individual_fund_flow(db, symbol, target_date)
    if not flow_data or flow_data.get("main_net_inflow") is None:
        logger.info("No fund flow data for %s, skip", symbol.symbol)
        return None

    return _upsert_capital_flow(db, symbol, flow_data)


def get_latest_capital_flow(db: Session, symbol_id: int) -> CapitalFlow | None:
    return db.execute(
        select(CapitalFlow)
        .where(CapitalFlow.symbol_id == symbol_id)
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

    latest = get_latest_capital_flow(db, symbol.id)
    if latest is not None:
        days_stale = (trade_date - latest.trade_date).days if hasattr(latest.trade_date, "year") else 0
        # 兜底：若 DB 命中但关键分值为 None（上次同步失败），也触发重新同步
        if days_stale <= _FLOW_FRESHNESS_DAYS and latest.main_net_inflow_score is not None:
            return latest

    try:
        return sync_symbol_capital_flow(db, symbol, trade_date)
    except Exception as exc:
        logger.warning("On-demand capital flow sync failed for %s: %s", symbol.symbol, exc)
        return latest


def get_main_net_inflow_score(db: Session, symbol: Symbol, trade_date: date) -> float | None:
    """供评分引擎调用的入口：返回 main_net_inflow_score 或 None。"""
    flow = get_or_sync_capital_flow(db, symbol, trade_date)
    if flow is None:
        return None
    return flow.main_net_inflow_score


# ----------------------------------------------------------------------------
# 北向资金（市场层面）
# ----------------------------------------------------------------------------

def sync_northbound_flow(db: Session, days: int = 30) -> int:
    """同步近 N 日北向资金净流入。

    Returns:
        写入的记录数
    """
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.stock_hsgt_north_net_flow_in, symbol="北上", api_key="stock_hsgt_north_net_flow_in", db=db)
        if df is None or df.empty:
            return 0
        # 列名：日期、当日净流入（元）、当日余额（元）
        df["日期"] = pd.to_datetime(df["日期"]).dt.date
        recent = df.sort_values("日期", ascending=False).head(days)
        count = 0
        for _, row in recent.iterrows():
            trade_date = row["日期"]
            net = _safe_float(row.get("当日净流入"))
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
                existing.total_net_inflow = net
            count += 1
        db.flush()
        return count
    except Exception as exc:
        logger.warning("sync_northbound_flow failed: %s", exc)
        return 0


def get_latest_northbound_flow(db: Session) -> NorthboundFlow | None:
    return db.execute(
        select(NorthboundFlow)
        .order_by(desc(NorthboundFlow.trade_date))
        .limit(1)
    ).scalars().first()
