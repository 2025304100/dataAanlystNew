"""P2：股票估值数据服务（PE/PB/市值/行业分位）。

数据源：akshare
- ak.stock_zh_a_spot_em()：全 A 股实时行情（含 PE/PB/总市值/流通市值）
- ak.stock_individual_info_em(symbol)：个股基本信息（行业）

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
from typing import Any

import akshare as ak
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services.akshare_utils import call_akshare_with_retry, quiet_akshare_output
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
    return symbol.symbol.split(".")[-1] if "." in symbol.symbol else symbol.symbol


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


def sync_symbol_valuation(db: Session, symbol: Symbol, trade_date: date | None = None) -> StockValuation | None:
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
    spot_data = _fetch_spot_valuation(db, symbol)
    info_data = _fetch_individual_info(db, symbol)

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
    actual_date = target_date

    # 2. 写库（upsert）
    existing = db.execute(
        select(StockValuation).where(
            StockValuation.symbol_id == symbol.id,
            StockValuation.trade_date == actual_date,
        )
    ).scalars().first()

    raw_json = json.dumps(
        {"spot": spot_data, "info": info_data},
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
    existing.source = "akshare"
    existing.raw_json = raw_json
    db.flush()
    return existing


def get_latest_valuation(db: Session, symbol_id: int) -> StockValuation | None:
    """读取 DB 中最新的估值记录。"""
    return db.execute(
        select(StockValuation)
        .where(StockValuation.symbol_id == symbol_id)
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

    latest = get_latest_valuation(db, symbol.id)
    if latest is not None:
        days_stale = (trade_date - latest.trade_date).days if hasattr(latest.trade_date, "year") else 0
        # 兜底：若 DB 命中但关键分值为 None（上次同步失败），也触发重新同步
        if days_stale <= _VALUATION_FRESHNESS_DAYS and latest.pe_score is not None:
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
