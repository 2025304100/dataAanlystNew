"""Portfolio-level asset-scope policy shared by admission and execution flows."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio
from app.models.symbol import Symbol

ASSET_SCOPE_STOCK = "stock"
ASSET_SCOPE_ETF = "etf"
ASSET_SCOPE_MIXED = "mixed"
ASSET_SCOPES = frozenset({ASSET_SCOPE_STOCK, ASSET_SCOPE_ETF, ASSET_SCOPE_MIXED})


def normalize_asset_scope(value: str | None) -> str:
    value = str(value or ASSET_SCOPE_MIXED).strip().lower()
    return value if value in ASSET_SCOPES else ASSET_SCOPE_MIXED


def allows_asset_type(scope: str | None, asset_type: str | None) -> bool:
    normalized_scope = normalize_asset_scope(scope)
    if normalized_scope == ASSET_SCOPE_MIXED:
        return asset_type in {ASSET_SCOPE_STOCK, ASSET_SCOPE_ETF}
    return asset_type == normalized_scope


def ensure_symbol_in_scope(portfolio: Portfolio, symbol: Symbol) -> None:
    scope = normalize_asset_scope(getattr(portfolio, "asset_scope", None))
    if allows_asset_type(scope, symbol.asset_type):
        return
    label = "股票" if scope == ASSET_SCOPE_STOCK else "ETF"
    actual = "ETF" if symbol.asset_type == ASSET_SCOPE_ETF else "股票"
    raise ValueError(f"该组合为{label}组合，不能加入{actual}标的 {symbol.symbol}")


def ensure_symbol_ids_in_scope(db: Session, portfolio: Portfolio, symbol_ids: list[int]) -> None:
    """Fail closed when a backtest/execution request contains an out-of-scope asset."""
    unique_ids = sorted({int(symbol_id) for symbol_id in symbol_ids if int(symbol_id) > 0})
    if not unique_ids:
        return
    symbols = db.execute(select(Symbol).where(Symbol.id.in_(unique_ids))).scalars().all()
    symbol_by_id = {symbol.id: symbol for symbol in symbols}
    missing_ids = [symbol_id for symbol_id in unique_ids if symbol_id not in symbol_by_id]
    if missing_ids:
        raise ValueError(f"标的不存在：{', '.join(map(str, missing_ids))}")
    for symbol in symbols:
        ensure_symbol_in_scope(portfolio, symbol)
