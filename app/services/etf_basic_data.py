"""P2：ETF 特有数据服务（溢价折价/跟踪误差/基金规模/份额变化）。

数据源：akshare
- ak.fund_etf_spot_em()：ETF 实时行情（含净值/折价率）
- ak.fund_etf_fund_info_em(fund)：ETF 历史净值
- ak.fund_etf_fund_daily_em()：ETF 每日份额/规模

设计要点：
1. sync_etf_indicator：拉取并写入当日 ETF 指标快照
2. calc_premium_discount_score：将溢价折价转换为 0-100 评分
3. calc_tracking_error：应用层计算跟踪误差
4. 评分时按需拉取：calculate_symbol_score_with_config 调用 get_or_sync_etf_indicator
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.etf_indicator import EtfIndicator
from app.models.symbol import Symbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
from app.services.market_data import _proxy_bypass
from app.services.regions import region_from_market

logger = logging.getLogger(__name__)

# ETF 指标数据新鲜度阈值（天）
_ETF_FRESHNESS_DAYS = 7


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


def _parse_pct(val: Any) -> float | None:
    """解析百分比字符串（如 '0.11%'）为 float（0.11）。"""
    if val is None:
        return None
    s = str(val).strip().rstrip("%")
    return _safe_float(s)


def _first_present(*values: Any) -> Any:
    """Return the first non-None value; numeric zero is a valid observation."""
    for value in values:
        if value is not None:
            return value
    return None


def _etf_code(symbol: Symbol) -> str:
    return symbol.symbol.split(".")[-1] if "." in symbol.symbol else symbol.symbol


def _fetch_etf_spot(db: Session, symbol: Symbol) -> dict[str, Any]:
    """从 ak.fund_etf_spot_em 拉取 ETF 实时溢价折价。

    返回 dict：{ close, nav, premium_discount }
    注：该接口网络不稳定，失败时返回空 dict，由调用方兜底。
    """
    code = _etf_code(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.fund_etf_spot_em, api_key="fund_etf_spot_em", db=db)
        if df is None or df.empty:
            return {}
        # 列名通常为：代码、名称、最新价、涨跌幅、成交量、成交额、开盘价、最高价、最低价、
        #           昨收、单位净值、累计净值、折价率
        row = df[df["代码"] == code]
        if row.empty:
            return {}
        row = row.iloc[0]
        premium = next(
            (_parse_pct(row.get(name)) for name in ("折价率", "溢价率", "折溢价率")
             if row.get(name) is not None),
            None,
        )
        return {
            "close": _safe_float(_first_present(row.get("最新价"), row.get("市价"))),
            "nav": _safe_float(_first_present(row.get("单位净值"), row.get("基金净值"))),
            "premium_discount": premium,
        }
    except Exception as exc:
        logger.debug("fund_etf_spot_em failed for %s: %s", symbol.symbol, exc)
        return {}


def _fetch_etf_fund_info(db: Session, symbol: Symbol, trade_date: date) -> dict[str, Any]:
    """从 ak.fund_etf_fund_info_em 拉取 ETF 历史净值。

    返回 dict：{ trade_date, nav }
    """
    code = _etf_code(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.fund_etf_fund_info_em, fund=code, api_key="fund_etf_fund_info_em", db=db)
        if df is None or df.empty:
            return {}
        # 列名：净值日期、单位净值、累计净值、日增长率
        df["净值日期"] = pd.to_datetime(df["净值日期"]).dt.date
        recent = df[df["净值日期"] <= trade_date]
        if recent.empty:
            recent = df.iloc[[-1]]
        else:
            recent = recent.iloc[[-1]]
        return {
            "trade_date": recent.iloc[0]["净值日期"],
            "nav": _safe_float(recent.iloc[0].get("单位净值")),
        }
    except Exception as exc:
        logger.debug("fund_etf_fund_info_em failed for %s: %s", symbol.symbol, exc)
        return {}


def _fetch_etf_fund_daily(db: Session, symbol: Symbol) -> dict[str, Any]:
    """从 ak.fund_etf_fund_daily_em 拉取 ETF 每日份额/规模/折价率。

    列名为动态日期（如 '2026-07-03-单位净值'），需按后缀匹配。
    返回 dict：{ nav, close, premium_discount }
    """
    code = _etf_code(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.fund_etf_fund_daily_em, api_key="fund_etf_fund_daily_em", db=db)
        if df is None or df.empty:
            return {}
        row = df[df["基金代码"] == code]
        if row.empty:
            return {}
        row = row.iloc[0]
        # 找第一个匹配 *-单位净值 的列（最新日期的净值）
        nav_col = next((c for c in df.columns if c.endswith("-单位净值")), None)
        nav = _safe_float(row.get(nav_col)) if nav_col else None
        # 市价、折价率是固定列名
        close = _safe_float(row.get("市价"))
        premium_discount = next(
            (_parse_pct(row.get(name)) for name in ("折价率", "溢价率", "折溢价率")
             if row.get(name) is not None),
            None,
        )
        return {
            "nav": nav,
            "close": close,
            "premium_discount": premium_discount,
        }
    except Exception as exc:
        logger.debug("fund_etf_fund_daily_em failed for %s: %s", symbol.symbol, exc)
        return {}


def calc_premium_discount_score(premium_discount: float | None) -> float | None:
    """将溢价折价率转换为 0-100 评分。

    逻辑：
    - 折价（负值）小幅为优（买入折价 ETF 相对便宜）
    - 溢价（正值）大幅为劣（买入溢价 ETF 不划算）
    - 评分区间：折价 -2% → 80，0% → 60，+2% → 40
    - 极端值（>5% 或 <-5%）clip 到 [25, 90]
    """
    if premium_discount is None:
        return None
    # premium_discount 单位 %，正值=溢价，负值=折价
    # 评分 = 60 - pd * 10（每 1% 溢价扣 10 分，每 1% 折价加 10 分）
    score = 60 - premium_discount * 10
    return max(25.0, min(90.0, score))


def calc_tracking_error(etf_returns: list[float], index_returns: list[float], window: int = 20) -> float | None:
    """应用层计算跟踪误差（%）。

    Args:
        etf_returns: ETF 日收益率序列
        index_returns: 标的指数日收益率序列
        window: 滚动窗口（默认 20 日）

    Returns:
        跟踪误差（%），越低越好
    """
    n = min(len(etf_returns), len(index_returns), window)
    if n < 5:
        return None
    diff = [etf_returns[i] - index_returns[i] for i in range(-n, 0)]
    avg = sum(diff) / n
    var = sum((x - avg) ** 2 for x in diff) / n
    return (var ** 0.5) * 100  # 转为百分比


def sync_etf_indicator(db: Session, symbol: Symbol, trade_date: date | None = None) -> EtfIndicator | None:
    """同步单个 ETF symbol 的指标数据。"""
    if symbol.asset_type != "etf":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    target_date = trade_date or date.today()

    # fund_daily 是最稳定的来源（返回 nav/close/premium_discount）
    fund_daily = _fetch_etf_fund_daily(db, symbol)
    fund_info = _fetch_etf_fund_info(db, symbol, target_date)
    spot_data = _fetch_etf_spot(db, symbol)

    # 优先级：fund_daily > fund_info > spot
    nav = _first_present(fund_daily.get("nav"), fund_info.get("nav"), spot_data.get("nav"))
    close = _first_present(fund_daily.get("close"), spot_data.get("close"))
    premium_discount = _first_present(
        fund_daily.get("premium_discount"), spot_data.get("premium_discount")
    )

    if nav is None and close is None and premium_discount is None:
        logger.info("No ETF indicator data for %s, skip", symbol.symbol)
        return None

    # 若无溢价折价率但有 nav 和 close，自行计算
    if premium_discount is None and nav and close and nav > 0:
        premium_discount = (close - nav) / nav * 100

    # 跟踪误差：需 ETF 收盘价序列 + 标的指数序列
    # 简化：暂不计算跟踪误差（需关联指数代码，预留接口）
    tracking_error = None

    score = calc_premium_discount_score(premium_discount)
    # This is a snapshot for the requested sync date.  The historical NAV
    # provider may return its latest available date, but using that date here
    # makes backfill/current coverage and retry resumptions consistent.
    actual_date = target_date

    existing = db.execute(
        select(EtfIndicator).where(
            EtfIndicator.symbol_id == symbol.id,
            EtfIndicator.trade_date == actual_date,
        )
    ).scalars().first()

    raw_json = json.dumps(
        {"spot": spot_data, "fund_info": fund_info, "fund_daily": fund_daily},
        ensure_ascii=False,
        default=str,
    )

    if existing is None:
        existing = EtfIndicator(
            symbol_id=symbol.id,
            trade_date=actual_date,
        )
        db.add(existing)

    existing.nav = nav
    existing.close = close
    existing.premium_discount = premium_discount
    # fund_size/total_shares/shares_change 当前数据源未稳定返回，置 None（预留字段）
    existing.fund_size = None
    existing.total_shares = None
    existing.shares_change = None
    existing.tracking_error = tracking_error
    existing.premium_discount_score = score
    existing.source = "akshare"
    existing.raw_json = raw_json
    db.flush()
    return existing


def get_latest_etf_indicator(db: Session, symbol_id: int) -> EtfIndicator | None:
    return db.execute(
        select(EtfIndicator)
        .where(EtfIndicator.symbol_id == symbol_id)
        .order_by(desc(EtfIndicator.trade_date))
        .limit(1)
    ).scalars().first()


def get_or_sync_etf_indicator(db: Session, symbol: Symbol, trade_date: date) -> EtfIndicator | None:
    """评分时按需拉取：若 DB 无最新 ETF 指标或数据过期，触发同步。

    兜底：若 DB 命中但关键分值（premium_discount_score）为 None（说明上次同步失败），
    也触发重新同步，避免脏数据长期兜底。
    """
    if symbol.asset_type != "etf":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    latest = get_latest_etf_indicator(db, symbol.id)
    if latest is not None:
        days_stale = (trade_date - latest.trade_date).days if hasattr(latest.trade_date, "year") else 0
        if days_stale <= _ETF_FRESHNESS_DAYS and latest.premium_discount_score is not None:
            return latest

    try:
        return sync_etf_indicator(db, symbol, trade_date)
    except Exception as exc:
        logger.warning("On-demand ETF indicator sync failed for %s: %s", symbol.symbol, exc)
        return latest


def get_premium_discount_score(db: Session, symbol: Symbol, trade_date: date) -> float | None:
    """供评分引擎调用的入口：返回 premium_discount_score 或 None。"""
    indicator = get_or_sync_etf_indicator(db, symbol, trade_date)
    if indicator is None:
        return None
    return indicator.premium_discount_score
