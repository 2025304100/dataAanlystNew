from __future__ import annotations

from functools import lru_cache

import akshare as ak

from app.models.symbol import Symbol
from app.services.akshare_utils import quiet_akshare_output


def _is_placeholder_name(symbol: str, name: str | None) -> bool:
    cleaned_name = (name or "").strip()
    cleaned_symbol = (symbol or "").strip()
    return not cleaned_name or cleaned_name.upper() == cleaned_symbol.upper()


def _contains_cjk(value: str | None) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value or "")


def _should_use_official_cn_name(symbol: str, current_name: str | None, official_name: str | None) -> bool:
    if not official_name:
        return False
    if _is_placeholder_name(symbol, current_name):
        return True
    return _contains_cjk(official_name) and not _contains_cjk(current_name)


@lru_cache(maxsize=1)
def _cn_stock_name_map() -> dict[str, str]:
    with quiet_akshare_output():
        frame = ak.stock_info_a_code_name()
    return {
        str(row["code"]).zfill(6): str(row["name"]).strip()
        for row in frame.to_dict("records")
        if row.get("code") and row.get("name")
    }


@lru_cache(maxsize=1)
def _fund_name_map() -> dict[str, str]:
    with quiet_akshare_output():
        frame = ak.fund_name_em()
    return {
        str(row["基金代码"]).zfill(6): str(row["基金简称"]).strip()
        for row in frame.to_dict("records")
        if row.get("基金代码") and row.get("基金简称")
    }


def resolve_symbol_name(symbol_code: str, asset_type: str, market: str, current_name: str | None = None) -> str | None:
    symbol = symbol_code.strip().upper()
    market_lower = (market or "").lower()
    if market_lower not in {"sh", "sz", "bj", "cn"}:
        return current_name
    if not _is_placeholder_name(symbol, current_name) and _contains_cjk(current_name):
        return current_name

    lookups = [_fund_name_map] if asset_type == "etf" else [_cn_stock_name_map, _fund_name_map]
    for lookup in lookups:
        try:
            resolved = lookup().get(symbol)
        except Exception:
            resolved = None
        if _should_use_official_cn_name(symbol, current_name, resolved):
            return resolved
    return current_name


def refresh_symbol_name(symbol: Symbol) -> bool:
    resolved = resolve_symbol_name(symbol.symbol, symbol.asset_type, symbol.market, symbol.name)
    if resolved and resolved != symbol.name:
        symbol.name = resolved
        return True
    return False
