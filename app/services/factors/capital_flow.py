"""Pure field adapters for capital-flow AkShare interfaces."""
from __future__ import annotations

import pandas as pd

from app.services.factors.contracts import (
    normalize_dates,
    normalize_numbers,
    normalize_symbol,
    resolve_contract,
)


def normalize_fund_flow_frame(
    frame: pd.DataFrame, *, symbol: str
) -> pd.DataFrame:
    aliases = {
        "trade_date": ("日期", "TRADE_DATE"),
        "main_net_inflow": ("主力净流入-净额", "MAIN_NET_INFLOW"),
        "main_net_inflow_pct": (
            "主力净流入-净占比",
            "MAIN_NET_INFLOW_PCT",
        ),
        "super_large_net_inflow": (
            "超大单净流入-净额",
            "SUPER_LARGE_NET_INFLOW",
        ),
        "large_net_inflow": ("大单净流入-净额", "LARGE_NET_INFLOW"),
        "medium_net_inflow": ("中单净流入-净额", "MEDIUM_NET_INFLOW"),
        "small_net_inflow": ("小单净流入-净额", "SMALL_NET_INFLOW"),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_individual_fund_flow",
        aliases=aliases,
        required=("trade_date", "main_net_inflow"),
    )
    columns = [
        "symbol",
        *aliases,
        "source",
    ]
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized["trade_date"] = normalize_dates(normalized["trade_date"])
    for column in aliases:
        if column != "trade_date":
            normalized[column] = normalize_numbers(normalized[column])
    normalized.insert(0, "symbol", normalize_symbol(symbol))
    normalized["source"] = "akshare:stock_individual_fund_flow"
    return normalized.dropna(subset=["trade_date"])[columns]


def normalize_lhb_detail_frame(frame: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "symbol": ("代码", "SECURITY_CODE"),
        "trade_date": ("上榜日", "TRADE_DATE"),
        "lhb_net_buy": ("龙虎榜净买额", "BILLBOARD_NET_AMT"),
        "lhb_buy": ("龙虎榜买入额", "BILLBOARD_BUY_AMT"),
        "lhb_sell": ("龙虎榜卖出额", "BILLBOARD_SELL_AMT"),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_lhb_detail_em",
        aliases=aliases,
        required=("symbol", "trade_date", "lhb_net_buy"),
    )
    columns = [*aliases, "has_lhb", "source"]
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized["symbol"] = normalized["symbol"].map(normalize_symbol)
    normalized["trade_date"] = normalize_dates(normalized["trade_date"])
    for column in ("lhb_net_buy", "lhb_buy", "lhb_sell"):
        normalized[column] = normalize_numbers(normalized[column])
    normalized["has_lhb"] = True
    normalized["source"] = "akshare:stock_lhb_detail_em"
    return normalized.dropna(subset=["trade_date"])[columns]
