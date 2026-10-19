"""ETF dated snapshots and bounded historical NAV lookup.

Current sync batches selected codes only. NAV is explicitly an IOPV estimate;
estimated net assets are IOPV * shares. Benchmark tracking error is not collected.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Any, Callable

import akshare as ak
import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.etf_indicator import EtfIndicator
from app.models.symbol import Symbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
from app.services.market_data import _proxy_bypass
from app.services.factors.contracts import normalize_symbol
from app.services.regions import region_from_market
from app.services.etf_provider import BATCH_SIZE, fetch_etf_quotes

TRACKING_ERROR_UNAVAILABLE = "跟踪误差尚未接入对应基准及同日收益序列，当前同步不提供该指标。"

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
    return normalize_symbol(symbol.symbol)


def _observation_date(value: Any) -> date | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _fetch_etf_spot(db: Session, symbol: Symbol) -> dict[str, Any]:
    """Compatibility helper using the same targeted current quote provider."""
    quote = fetch_etf_quotes(db, [_etf_code(symbol)]).get(_etf_code(symbol), {})
    nav, close = quote.get("nav"), quote.get("close")
    return {**quote, "nav_date": quote.get("trade_date"),
            "premium_discount": (close / nav - 1) * 100 if nav and close else None}


def _fetch_etf_fund_info(db: Session, symbol: Symbol, trade_date: date) -> dict[str, Any]:
    """从 ak.fund_etf_fund_info_em 拉取 ETF 历史净值。

    返回 dict：{ trade_date, nav }
    """
    code = _etf_code(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.fund_etf_fund_info_em, fund=code,
                start_date=(trade_date - timedelta(days=10)).strftime("%Y%m%d"),
                end_date=trade_date.strftime("%Y%m%d"),
                api_key="fund_etf_fund_info_em", max_attempts=2, db=db)
        if df is None or df.empty:
            return {}
        # 列名：净值日期、单位净值、累计净值、日增长率
        df = df.copy()
        df["净值日期"] = pd.to_datetime(df["净值日期"], errors="coerce").dt.date
        recent = df[df["净值日期"].notna() & (df["净值日期"] <= trade_date)]
        if recent.empty:
            return {}
        recent = recent.sort_values("净值日期").iloc[[-1]]
        return {
            "trade_date": recent.iloc[0]["净值日期"],
            "nav": _safe_float(recent.iloc[0].get("单位净值")),
        }
    except Exception as exc:
        logger.debug("fund_etf_fund_info_em failed for %s: %s", symbol.symbol, exc)
        return {}


def _fetch_etf_fund_daily(db: Session, symbol: Symbol) -> dict[str, Any]:
    """从 ak.fund_etf_fund_daily_em 拉取带日期净值；该接口不提供份额或规模。

    列名为动态日期（如 '2026-07-03-单位净值'），需按后缀匹配。
    返回 dict：{ nav, close, premium_discount }
    """
    code = _etf_code(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.fund_etf_fund_daily_em, api_key="fund_etf_fund_daily_em", db=db)
        if df is None or df.empty:
            return {}
        row = df[df["基金代码"].map(normalize_symbol) == code]
        if row.empty:
            return {}
        row = row.iloc[0]
        dated_columns = [
            (observed, column)
            for column in df.columns if str(column).endswith("-单位净值")
            for observed in [_observation_date(str(column).removesuffix("-单位净值"))]
            if observed is not None and observed <= date.today()
        ]
        nav_date, nav_col = max(dated_columns) if dated_columns else (None, None)
        nav = _safe_float(row.get(nav_col)) if nav_col else None
        # 市价、折价率是固定列名
        close = _safe_float(row.get("市价"))
        premium_discount = next(
            (_parse_pct(row.get(name)) for name in ("折价率", "溢价率", "折溢价率")
             if row.get(name) is not None),
            None,
        )
        return {
            "trade_date": nav_date,
            "price_date": _observation_date(row.get("交易日期")),
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


def sync_etf_snapshots(
    db: Session, symbols: list[Symbol], trade_date: date,
    *, should_cancel: Callable[[], bool] | None = None,
    on_result: Callable[[Symbol, EtfIndicator | None, dict], None] | None = None,
) -> dict:
    """Collect only same-day IOPV snapshots in selected-symbol batches.

    fund_size is an explicit estimate (IOPV * shares), never market cap or a
    claimed audited NAV. Tracking error requires a separate benchmark contract.
    """
    from app.services.market_session import is_cn_trading_day

    if trade_date != date.today():
        raise ValueError("ETF 实时快照仅支持当前日期，历史净值请使用有界净值接口")
    result = {"dataset": "etf", "total": len(symbols), "success": 0, "skipped": 0,
              "failed": 0, "records": 0, "errors": [], "warnings": [],
              "unavailable_fields": {"etf_tracking_error": TRACKING_ERROR_UNAVAILABLE},
              "field_coverage": {key: {"available": 0, "total": len(symbols)} for key in
                  ("etf_premium_discount", "etf_fund_size", "etf_total_shares")}}
    if not is_cn_trading_day(db, trade_date):
        return {**result, "skipped": len(symbols), "skip_reason": "market_closed",
                "message": f"{trade_date} 休市，无需更新"}
    eligible = [symbol for symbol in symbols
                if symbol.asset_type == "etf" and region_from_market(symbol.market) == "cn"]
    if not eligible:
        return {**result, "skipped": len(symbols), "skip_reason": "no_symbols",
                "message": "所选范围没有境内 ETF，无需同步"}
    result["skipped"] = len(symbols) - len(eligible)
    for start in range(0, len(eligible), BATCH_SIZE):
        batch = eligible[start:start + BATCH_SIZE]
        if should_cancel and should_cancel():
            raise InterruptedError("ETF 同步已取消")
        quotes = fetch_etf_quotes(db, [_etf_code(symbol) for symbol in batch], should_cancel=should_cancel)
        for symbol in batch:
            if should_cancel and should_cancel():
                raise InterruptedError("ETF 同步已取消")
            quote = quotes.get(_etf_code(symbol), {})
            item, fresh = None, {}
            if quote.get("trade_date") == trade_date:
                nav, close, shares = (quote.get(key) for key in ("nav", "close", "total_shares"))
                premium = (close / nav - 1) * 100 if nav and close else None
                size = nav * shares if nav and shares else None
                premium = premium if premium is not None and math.isfinite(premium) else None
                size = size if size is not None and math.isfinite(size) else None
                fresh = {"etf_premium_discount": premium, "etf_fund_size": size, "etf_total_shares": shares}
                # A local closing price or a returned quote alone is not an ETF metric.
                if any(value is not None and math.isfinite(value) for value in fresh.values()):
                    item = db.execute(select(EtfIndicator).where(
                        EtfIndicator.symbol_id == symbol.id, EtfIndicator.trade_date == trade_date,
                    )).scalars().first()
                    if item is None:
                        item = EtfIndicator(symbol_id=symbol.id, trade_date=trade_date)
                        db.add(item)
                    prior_source = item.source
                    retained = [key for key, value in {"nav": nav, "close": close,
                        "total_shares": shares, "fund_size": size, "premium_discount": premium}.items()
                        if value is None and getattr(item, key) is not None]
                    for key, value in {"nav": nav, "close": close, "total_shares": shares,
                                       "fund_size": size, "premium_discount": premium}.items():
                        if value is not None and math.isfinite(value):
                            setattr(item, key, value)
                    if premium is not None:
                        item.premium_discount_score = calc_premium_discount_score(premium)
                    item.source = "eastmoney:etf_snapshot"
                    item.raw_json = json.dumps({"quote": quote,
                        "nav_basis": "iopv" if nav is not None else "retained_same_day" if item.nav is not None else None,
                        "fund_size_basis": "same_day_iopv_times_shares" if size is not None else "retained_same_day" if item.fund_size is not None else None,
                        "retained_same_day_fields": retained, "retained_source": prior_source if retained else None,
                        "tracking_error_status": "benchmark_history_unavailable"}, ensure_ascii=False, default=str)
                    db.flush()
            if item is None:
                result["skipped"] += 1
            else:
                result["success"] += 1
                result["records"] += 1
                for key, value in fresh.items():
                    if value is not None and math.isfinite(value):
                        result["field_coverage"][key]["available"] += 1
            if on_result:
                on_result(symbol, item, result)
    counts = result["field_coverage"]
    total = len(symbols)
    result["warnings"] = [TRACKING_ERROR_UNAVAILABLE]
    result["message"] = (f"ETF 快照：溢折价 {counts['etf_premium_discount']['available']}/{total}，"
        f"规模估算 {counts['etf_fund_size']['available']}/{total}，"
        f"份额 {counts['etf_total_shares']['available']}/{total}；跟踪误差尚未接入")
    if result["records"]:
        result.update(written_start_date=trade_date.isoformat(), written_end_date=trade_date.isoformat())
    else:
        result.update(skip_reason="data_unavailable", message=f"未取得 {trade_date} 的有效 ETF 指标，未更新本地数据")
    return result


def sync_etf_indicator(db: Session, symbol: Symbol, trade_date: date | None = None) -> EtfIndicator | None:
    """同步单个 ETF symbol 的指标数据。"""
    if symbol.asset_type != "etf":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    target_date = trade_date or date.today()

    if target_date > date.today():
        return None
    if target_date == date.today():
        result = sync_etf_snapshots(db, [symbol], target_date)
        if not result["records"]:
            return None
        return db.execute(select(EtfIndicator).where(
            EtfIndicator.symbol_id == symbol.id, EtfIndicator.trade_date == target_date,
        )).scalars().first()
    # Current quotes have no historical contract. Never request them for a
    # historical target or join today's quote to a prior day's published NAV.
    fund_daily = _fetch_etf_fund_daily(db, symbol) if target_date == date.today() else {}
    fund_info = _fetch_etf_fund_info(db, symbol, target_date)
    spot_data = _fetch_etf_spot(db, symbol) if target_date == date.today() else {}
    nav_observations = [
        (observed, _safe_float(source.get("nav")))
        for source in (fund_daily, fund_info)
        for observed in [_observation_date(source.get("trade_date"))]
        if observed is not None and observed <= target_date and _safe_float(source.get("nav")) is not None
    ]
    spot_nav_date = _observation_date(spot_data.get("nav_date"))
    if spot_nav_date is not None and spot_nav_date <= target_date and spot_data.get("nav") is not None:
        nav_observations.append((spot_nav_date, _safe_float(spot_data["nav"])))
    if nav_observations:
        actual_date, nav = max(nav_observations, key=lambda item: item[0])
    else:
        actual_date, nav = target_date, None
    close = premium_discount = None
    for source, date_key in ((fund_daily, "price_date"), (spot_data, "trade_date")):
        if _observation_date(source.get(date_key)) == actual_date:
            close = _first_present(close, _safe_float(source.get("close")))
            premium_discount = _first_present(premium_discount, _safe_float(source.get("premium_discount")))
    if close is None:
        close = _safe_float(db.execute(select(DailyBar.close).where(
            DailyBar.symbol_id == symbol.id, DailyBar.trade_date == actual_date
        )).scalar())

    if nav is None and close is None and premium_discount is None:
        logger.info("No ETF indicator data for %s, skip", symbol.symbol)
        return None

    existing = db.execute(
        select(EtfIndicator).where(
            EtfIndicator.symbol_id == symbol.id,
            EtfIndicator.trade_date == actual_date,
        )
    ).scalars().first()
    retained_fields = []
    if existing is not None:
        # A provider outage is missing evidence, not an instruction to clear
        # a known fact. Reuse only this exact day's snapshot, never another day.
        if nav is None and existing.nav is not None:
            nav = _safe_float(existing.nav)
            retained_fields.append("nav")
        if close is None and existing.close is not None:
            close = _safe_float(existing.close)
            retained_fields.append("close")
    if premium_discount is None:
        if nav and close and nav > 0:
            premium_discount = (close - nav) / nav * 100
        elif existing is not None and nav == existing.nav and close == existing.close:
            premium_discount = _safe_float(existing.premium_discount)
            retained_fields.append("premium_discount")
    score = calc_premium_discount_score(premium_discount)

    raw_json = json.dumps(
        {"spot": spot_data, "fund_info": fund_info, "fund_daily": fund_daily,
         "retained_same_day_fields": retained_fields,
         "retained_source": existing.source if existing is not None and retained_fields else None},
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
    # Historical NAV does not supply shares or benchmark returns. Preserve
    # exact-day facts when this is only a partial historical refresh.
    existing.premium_discount_score = score
    existing.source = "akshare"
    existing.raw_json = raw_json
    db.flush()
    return existing


def get_latest_etf_indicator(
    db: Session, symbol_id: int, *, as_of: date | None = None
) -> EtfIndicator | None:
    stmt = select(EtfIndicator).where(EtfIndicator.symbol_id == symbol_id)
    if as_of is not None:
        stmt = stmt.where(EtfIndicator.trade_date <= as_of)
    return db.execute(
        stmt
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

    latest = get_latest_etf_indicator(db, symbol.id, as_of=trade_date)
    if latest is not None:
        days_stale = (trade_date - latest.trade_date).days if hasattr(latest.trade_date, "year") else 0
        if 0 <= days_stale <= _ETF_FRESHNESS_DAYS and latest.premium_discount_score is not None:
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
