"""P2：股票估值数据服务（PE/PB/市值/行业分位）。

数据源：akshare
- ak.stock_zh_a_spot_em()：全 A 股实时行情（含 PE/PB/总市值/流通市值）
- ak.stock_individual_info_em(symbol)：个股基本信息（行业）

日频批量同步使用 valuation_provider 的日期、代码过滤请求，只下载当天缺口；
历史回填同样将日期范围传给服务端。下方旧单股按需查询仅供评分兼容路径使用。

设计要点：
1. sync_symbol_valuation：拉取并写入当日估值快照
2. get_latest_valuation：读取 DB 最新估值
3. calc_pe_score：将 PE 转换为 0-100 评分（结合历史分位 + 行业分位）
4. 评分时按需拉取：calculate_symbol_score_with_config 调用 get_or_sync_valuation

注：akshare 1.18.30 中 stock_a_lg_indicator 不存在，PE 历史分位暂不可用，
    仅按绝对 PE 粗略评分；待数据源稳定后接入历史分位。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Callable

import akshare as ak
import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
from app.services.factors.fundamental import normalize_stock_value_frame
from app.services.factors.contracts import normalize_symbol
from app.services.market_data import _proxy_bypass
from app.services.regions import region_from_market

logger = logging.getLogger(__name__)

# 估值数据新鲜度阈值（天）：超过则触发按需拉取
_VALUATION_FRESHNESS_DAYS = 7


def _safe_float(val: Any) -> float | None:
    """安全转 float，处理 NaN/None/字符串。"""
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
    """从 Symbol 构造 akshare 部分接口需要的 6 位代码（无前缀）。"""
    return normalize_symbol(symbol.symbol)


def _market_prefix_for_fund_flow(symbol: Symbol) -> str:
    """构造 stock_individual_fund_flow 需要的市场前缀（sh/sz/bj）。"""
    code = _market_code_for_akshare(symbol)
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return "sh"


def _fetch_spot_valuation(db: Session, symbol: Symbol) -> dict[str, Any]:
    """从 ak.stock_zh_a_spot_em 拉取全市场快照，过滤出当前 symbol。

    返回 dict：{ pe_ttm, pb, total_market_cap, circulating_market_cap }
    注：该接口返回全市场 ~5000 条，单次调用较慢；调用方应优先使用 _fetch_lg_indicator。
    """
    code = _market_code_for_akshare(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.stock_zh_a_spot_em, api_key="stock_zh_a_spot_em", db=db)
        if df is None or df.empty:
            return {}
        # 代码列名通常为 "代码"
        row = df[df["代码"] == code]
        if row.empty:
            return {}
        row = row.iloc[0]
        return {
            "pe_ttm": _safe_float(row.get("市盈率-动态")),
            "pb": _safe_float(row.get("市净率")),
            "total_market_cap": _safe_float(row.get("总市值")),
            "circulating_market_cap": _safe_float(row.get("流通市值")),
        }
    except Exception as exc:
        logger.debug("stock_zh_a_spot_em failed for %s: %s", symbol.symbol, exc)
        return {}


def sync_market_valuation_snapshot(
    db: Session,
    symbols: list[Symbol],
    trade_date: date | None = None,
    *, should_cancel: Callable[[], bool] | None = None,
) -> set[int]:
    """Fetch only the requested symbols and exact day, with no historical fallback."""
    target_date = trade_date or date.today()
    if target_date != date.today():
        raise ValueError("实时估值快照仅允许同步今天；历史日期请使用估值历史同步。")
    from app.services.market_session import is_cn_trading_day

    if not is_cn_trading_day(db, target_date):
        raise ValueError("休市日不生成实时估值快照")
    eligible = [
        symbol for symbol in symbols
        if symbol.asset_type == "stock" and region_from_market(symbol.market) == "cn"
    ]
    if not eligible:
        return set()
    from app.services.valuation_provider import DAILY_BATCH_SIZE, fetch_valuation_rows

    observations = []
    for offset in range(0, len(eligible), DAILY_BATCH_SIZE):
        batch = eligible[offset:offset + DAILY_BATCH_SIZE]
        rows = fetch_valuation_rows(db, [_market_code_for_akshare(s) for s in batch],
                                    target_date, target_date, should_cancel=should_cancel)
        if offset == 0 and not any(
            any(_safe_float(row.get(field)) is not None for field in ("pe_ttm", "pb", "total_market_cap"))
            for row in rows
        ):
            raise RuntimeError("所请求标的的当日估值尚未发布或无数据，已停止；未下载历史，请稍后重试")
        observations.extend(rows)
    frame = pd.DataFrame([{
        "代码": row["symbol"], "PE(TTM)": row["pe_ttm"], "市净率": row["pb"],
        "总市值": row["total_market_cap"], "流通市值": row["circulating_market_cap"],
        "数据日期": row["trade_date"].isoformat(),
        "_daily_complete": datetime.now(timezone.utc).replace(tzinfo=None) >=
            datetime(target_date.year, target_date.month, target_date.day, 7),
    } for row in observations])

    def value(row: Any, *names: str) -> float | None:
        for name in names:
            if name in row.index:
                parsed = _safe_float(row.get(name))
                if parsed is not None:
                    return parsed
        return None

    by_code = {
        normalize_symbol(code).zfill(6): row
        for _, row in frame.iterrows()
        for code in [row.get("代码")]
        if code is not None
    }
    symbol_ids = [symbol.id for symbol in eligible]
    existing = {
        row.symbol_id: row
        for row in db.execute(
            select(StockValuation).where(
                StockValuation.symbol_id.in_(symbol_ids),
                StockValuation.trade_date == target_date,
            )
        ).scalars().all()
    }
    class SnapshotSymbols(set):
        trade_dates = {target_date}

    synced: set[int] = SnapshotSymbols()
    for symbol in eligible:
        row = by_code.get(_market_code_for_akshare(symbol).zfill(6))
        if row is None:
            continue
        pe_ttm = value(row, "PE(TTM)")
        pb = value(row, "市净率")
        total_market_cap = value(row, "总市值")
        circulating_market_cap = value(row, "流通市值")
        if pe_ttm is None and pb is None and total_market_cap is None:
            continue
        valuation = existing.get(symbol.id)
        if valuation is None:
            valuation = StockValuation(symbol_id=symbol.id, trade_date=target_date)
            db.add(valuation)
        valuation.pe_ttm = pe_ttm
        valuation.pb = pb
        valuation.total_market_cap = total_market_cap
        valuation.circulating_market_cap = circulating_market_cap
        valuation.industry = symbol.industry
        valuation.industry_pe_percentile = None
        valuation.pe_history_percentile = None
        valuation.pe_score = calc_pe_score(pe_ttm, None, None)
        valuation.source = "eastmoney:valuation_daily"
        valuation.raw_json = json.dumps(row.to_dict(), ensure_ascii=False, default=str)
        synced.add(symbol.id)
    db.flush()
    return synced


def complete_daily_valuation_ids(db: Session, symbol_ids: list[int], day: date) -> set[int]:
    """Recognize completed rows without letting an intraday insert suppress the close."""
    if not symbol_ids:
        return set()
    rows = db.execute(select(StockValuation.symbol_id, StockValuation.created_at,
                             StockValuation.raw_json).where(
        StockValuation.symbol_id.in_(symbol_ids), StockValuation.trade_date == day,
        StockValuation.pb.is_not(None), StockValuation.total_market_cap.is_not(None),
    )).all()
    close = datetime(day.year, day.month, day.day, 7)
    complete = set()
    for row in rows:
        try:
            evidence = json.loads(row.raw_json or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        if (row.created_at and row.created_at.replace(tzinfo=None) >= close) or evidence.get("_daily_complete") is True:
            complete.add(row.symbol_id)
    return complete


def _fetch_individual_info(db: Session, symbol: Symbol) -> dict[str, Any]:
    """从 ak.stock_individual_info_em 拉取个股基本信息（行业）。"""
    code = _market_code_for_akshare(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            df = call_akshare_with_retry(ak.stock_individual_info_em, symbol=code, api_key="stock_individual_info_em", db=db)
        if df is None or df.empty:
            return {}
        # df 为两列：item / value
        row_map = dict(zip(df["item"], df["value"]))
        industry = row_map.get("行业")
        return {"industry": str(industry) if industry else None}
    except Exception as exc:
        logger.debug("stock_individual_info_em failed for %s: %s", symbol.symbol, exc)
        return {}


def _fetch_value_history(
    db: Session, symbol: Symbol, trade_date: date
) -> dict[str, Any]:
    """Fallback to the stable per-stock historical valuation interface."""
    code = _market_code_for_akshare(symbol)
    try:
        with _proxy_bypass(), quiet_akshare_output():
            frame = call_akshare_with_retry(
                ak.stock_value_em,
                symbol=code,
                api_key="stock_value_em",
                db=db,
            )
        normalized = normalize_stock_value_frame(frame, symbol=code)
        if normalized.empty:
            return {}
        eligible = normalized[
            normalized["trade_date"] <= trade_date
        ].sort_values("trade_date")
        if eligible.empty:
            return {}
        row = eligible.iloc[-1]
        return {
            "trade_date": row["trade_date"],
            "pe_ttm": _safe_float(row["pe_ttm"]),
            "pb": _safe_float(row["pb"]),
            "total_market_cap": _safe_float(row["total_market_cap"]),
            "circulating_market_cap": _safe_float(
                row["circulating_market_cap"]
            ),
        }
    except Exception as exc:
        logger.debug(
            "stock_value_em failed for %s: %s", symbol.symbol, exc
        )
        return {}


def _fetch_value_history_rows(
    db: Session,
    symbol: Symbol,
    *,
    start_date: date,
    end_date: date,
    quiet_output: bool = True,
) -> list[dict[str, Any]]:
    """Push both history bounds into the provider request, not a local filter."""
    from app.services.valuation_provider import fetch_valuation_rows

    rows = fetch_valuation_rows(db, [_market_code_for_akshare(symbol)], start_date, end_date)
    return [{key: (_safe_float(value) if key not in {"symbol", "trade_date"} else value)
             for key, value in row.items()} for row in rows]


def _upsert_valuation_history_row(
    db: Session,
    symbol: Symbol,
    valuation_data: dict[str, Any],
    *,
    industry: str | None,
) -> StockValuation | None:
    """Persist one provider observation without synthesizing missing dates."""
    actual_date = valuation_data.get("trade_date")
    if not isinstance(actual_date, date):
        return None
    if all(
        valuation_data.get(field) is None
        for field in ("pe_ttm", "pb", "total_market_cap")
    ):
        return None
    existing = db.execute(
        select(StockValuation).where(
            StockValuation.symbol_id == symbol.id,
            StockValuation.trade_date == actual_date,
        )
    ).scalars().first()
    if existing is None:
        existing = StockValuation(symbol_id=symbol.id, trade_date=actual_date)
        db.add(existing)
    existing.pe_ttm = valuation_data.get("pe_ttm")
    existing.pb = valuation_data.get("pb")
    existing.total_market_cap = valuation_data.get("total_market_cap")
    existing.circulating_market_cap = valuation_data.get("circulating_market_cap")
    existing.industry = industry
    existing.industry_pe_percentile = None
    existing.pe_history_percentile = None
    existing.pe_score = calc_pe_score(existing.pe_ttm, None, None)
    existing.source = "akshare:stock_value_em"
    existing.raw_json = json.dumps(valuation_data, ensure_ascii=False, default=str)
    db.flush()
    return existing


def sync_symbol_valuation_range(
    db: Session,
    symbol: Symbol,
    *,
    start_date: date,
    end_date: date,
    quiet_output: bool = True,
) -> int:
    """Upsert historical PE/PB rows for a bounded, provider-backed range."""
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if symbol.asset_type != "stock" or region_from_market(symbol.market) != "cn":
        return 0
    # Historical valuation rows do not require a second per-symbol industry
    # request. The symbol master already carries the latest industry label;
    # avoiding this call halves provider traffic during a full backfill.
    industry = symbol.industry
    rows = _fetch_value_history_rows(
        db, symbol, start_date=start_date, end_date=end_date, quiet_output=quiet_output
    )
    return sum(
        _upsert_valuation_history_row(db, symbol, row, industry=industry) is not None
        for row in rows
    )


def calc_pe_score(
    pe_ttm: float | None,
    pe_history_percentile: float | None,
    industry_pe_percentile: float | None,
) -> float | None:
    """将 PE 转换为 0-100 评分。

    逻辑：
    - PE 为负或 None：返回 None（无法评估）
    - 综合历史分位（权重 0.6）+ 行业分位（权重 0.4）
    - 分位越低（越被低估）→ 评分越高
    - 评分 = 100 - 加权分位（限制在 [20, 95]）

    Args:
        pe_ttm: PE_TTM
        pe_history_percentile: 0-100，当前 PE 在自身历史中的百分位
        industry_pe_percentile: 0-100，当前 PE 在行业内的百分位
    """
    if pe_ttm is None or pe_ttm <= 0:
        return None
    # 负 PE 直接返回低分
    if pe_ttm < 0:
        return 30.0
    components: list[tuple[float, float]] = []
    if pe_history_percentile is not None:
        components.append((pe_history_percentile, 0.6))
    if industry_pe_percentile is not None:
        components.append((industry_pe_percentile, 0.4))
    if not components:
        # 无分位数据，仅按绝对 PE 粗略评分：PE<15 高分，PE>80 低分
        if pe_ttm < 15:
            return 80.0
        if pe_ttm < 30:
            return 65.0
        if pe_ttm < 60:
            return 50.0
        if pe_ttm < 100:
            return 40.0
        return 30.0
    total_w = sum(w for _, w in components)
    weighted_pct = sum(p * w for p, w in components) / total_w
    # 分位越低越优 → 评分越高
    score = 100 - weighted_pct
    return max(20.0, min(95.0, score))


def sync_symbol_valuation(
    db: Session, symbol: Symbol, trade_date: date | None = None, *,
    skip_market_snapshot: bool = False,
    refresh_industry: bool = True,
    history_start_date: date | None = None,
) -> StockValuation | None:
    """同步单个 symbol 的估值数据。

    主数据源：stock_zh_a_spot_em（全市场快照，含 PE/PB/市值）。
    辅助：stock_individual_info_em（行业信息）。
    """
    if symbol.asset_type != "stock":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None  # 当前仅支持 A 股估值数据

    target_date = trade_date or date.today()

    # 1. 拉取数据
    if target_date > date.today():
        return None
    # An undated live quote must never be relabelled as a past observation.
    spot_data = _fetch_spot_valuation(db, symbol) if target_date == date.today() and not skip_market_snapshot else {}
    fallback_data = {}
    if (
        spot_data.get("pe_ttm") is None
        and spot_data.get("pb") is None
        and spot_data.get("total_market_cap") is None
    ):
        fallback_data = _fetch_value_history(db, symbol, target_date)
        if history_start_date is not None:
            actual_date = fallback_data.get("trade_date")
            if not isinstance(actual_date, date) or actual_date < history_start_date:
                return None
        spot_data = fallback_data
    # A failed task-wide market snapshot must not cause every symbol to
    # download it again, or refresh unrelated industry metadata. Dedicated
    # single-symbol callers retain their existing behavior by default.
    info_data = _fetch_individual_info(db, symbol) if refresh_industry else {}

    pe_ttm = spot_data.get("pe_ttm")
    pb = spot_data.get("pb")
    total_market_cap = spot_data.get("total_market_cap")
    circulating_market_cap = spot_data.get("circulating_market_cap")
    industry = info_data.get("industry") or symbol.industry
    # PE 历史分位暂不可用（akshare 1.18.30 无 stock_a_lg_indicator）
    pe_history_percentile = None
    # 行业分位暂不计算（需拉全行业成分股，开销大；预留接口）
    industry_pe_percentile = None

    if pe_ttm is None and pb is None and total_market_cap is None:
        logger.info("No valuation data for %s, skip", symbol.symbol)
        return None

    pe_score = calc_pe_score(pe_ttm, pe_history_percentile, industry_pe_percentile)
    actual_date = fallback_data.get("trade_date") or target_date

    # 2. 写库（upsert）
    existing = db.execute(
        select(StockValuation).where(
            StockValuation.symbol_id == symbol.id,
            StockValuation.trade_date == actual_date,
        )
    ).scalars().first()

    raw_json = json.dumps(
        {
            "spot": spot_data,
            "historical_fallback": bool(fallback_data),
            "info": info_data,
        },
        ensure_ascii=False,
        default=str,
    )

    if existing is None:
        existing = StockValuation(
            symbol_id=symbol.id,
            trade_date=actual_date,
        )
        db.add(existing)

    existing.pe_ttm = pe_ttm
    existing.pb = pb
    existing.total_market_cap = total_market_cap
    existing.circulating_market_cap = circulating_market_cap
    existing.industry_pe_percentile = industry_pe_percentile
    existing.industry = industry
    existing.pe_history_percentile = pe_history_percentile
    existing.pe_score = pe_score
    existing.source = (
        "akshare:stock_value_em"
        if fallback_data
        else "akshare:stock_zh_a_spot_em"
    )
    existing.raw_json = raw_json
    db.flush()
    return existing


def get_latest_valuation(
    db: Session, symbol_id: int, *, as_of: date | None = None
) -> StockValuation | None:
    """读取 DB 中最新的估值记录。"""
    stmt = select(StockValuation).where(StockValuation.symbol_id == symbol_id)
    if as_of is not None:
        stmt = stmt.where(StockValuation.trade_date <= as_of)
    return db.execute(
        stmt
        .order_by(desc(StockValuation.trade_date))
        .limit(1)
    ).scalars().first()


def get_or_sync_valuation(db: Session, symbol: Symbol, trade_date: date) -> StockValuation | None:
    """评分时按需拉取：若 DB 无最新估值或数据过期，触发同步。

    返回 None 表示该 symbol 不支持估值数据（非 A 股 / 非 stock / 数据源失败）。
    """
    if symbol.asset_type != "stock":
        return None
    region = region_from_market(symbol.market)
    if region != "cn":
        return None

    latest = get_latest_valuation(db, symbol.id, as_of=trade_date)
    if latest is not None:
        days_stale = (trade_date - latest.trade_date).days if hasattr(latest.trade_date, "year") else 0
        # 兜底：若 DB 命中但关键分值为 None（上次同步失败），也触发重新同步
        if 0 <= days_stale <= _VALUATION_FRESHNESS_DAYS and latest.pe_score is not None:
            return latest

    # 按需拉取
    try:
        return sync_symbol_valuation(db, symbol, trade_date)
    except Exception as exc:
        logger.warning("On-demand valuation sync failed for %s: %s", symbol.symbol, exc)
        return latest  # 返回旧数据兜底


def get_pe_score(db: Session, symbol: Symbol, trade_date: date) -> float | None:
    """供评分引擎调用的入口：返回 pe_score 或 None。"""
    valuation = get_or_sync_valuation(db, symbol, trade_date)
    if valuation is None:
        return None
    return valuation.pe_score
