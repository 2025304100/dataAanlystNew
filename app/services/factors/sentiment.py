"""Pure field adapters for sentiment AkShare interfaces."""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.services.factors.contracts import (
    normalize_numbers,
    normalize_symbol,
    resolve_contract,
)


def normalize_hot_rank_frame(
    frame: pd.DataFrame, *, as_of: date
) -> pd.DataFrame:
    aliases = {
        "hot_rank": ("当前排名", "排名", "rk"),
        "symbol": ("代码", "股票代码", "sc"),
    }
    normalized = resolve_contract(
        frame,
        api_key="stock_hot_rank_em",
        aliases=aliases,
        required=("hot_rank", "symbol"),
    )
    columns = [
        "symbol",
        "trade_date",
        "hot_rank",
        "hot_rank_total",
        "hot_rank_pct",
        "source",
    ]
    if normalized.empty:
        return pd.DataFrame(columns=columns)
    normalized["symbol"] = normalized["symbol"].map(normalize_symbol)
    normalized["hot_rank"] = normalize_numbers(normalized["hot_rank"])
    total = int(normalized["hot_rank"].notna().sum())
    normalized["trade_date"] = as_of
    normalized["hot_rank_total"] = total
    normalized["hot_rank_pct"] = (
        normalized["hot_rank"] / total * 100 if total else pd.NA
    )
    normalized["source"] = "akshare:stock_hot_rank_em"
    return normalized.dropna(subset=["hot_rank"])[columns]
