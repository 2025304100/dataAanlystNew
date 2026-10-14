"""Resolve score recipients without changing the full-market model reference."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models.portfolio import Portfolio, Position
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.async_task import FactorPipelineCreate


def freeze_score_universe(db, payload: FactorPipelineCreate) -> None:
    """Freeze explicit scope at submission; never fall back from an empty pool."""
    if payload.universe_source is None:
        payload.score_symbols = None
        return
    stmt = select(Symbol.symbol).where(
        Symbol.is_active == 1, Symbol.asset_type == 'stock',
        func.lower(Symbol.market).in_(('cn', 'sh', 'sz', 'bj')),
    )
    if payload.universe_source == 'watchlist':
        if db.get(Watchlist, payload.watchlist_id) is None:
            raise ValueError('所选观察池不存在，请重新选择观察池')
        stmt = stmt.join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id).where(
            WatchlistItem.watchlist_id == payload.watchlist_id,
            WatchlistItem.status.in_(('watching', 'ready')),
        )
        codes = db.scalars(stmt).all()
    elif payload.universe_source == 'positions':
        if db.get(Portfolio, payload.portfolio_id) is None:
            raise ValueError('所选组合不存在，请重新选择当前组合')
        stmt = stmt.join(Position, Position.symbol_id == Symbol.id).where(
            Position.portfolio_id == payload.portfolio_id, Position.quantity > 0,
        )
        codes = db.scalars(stmt).all()
    else:
        # External sync's resolver creates/reactivates business symbols. Score
        # refresh is read-only here: warehouse-only symbols remain visible as
        # missing business mappings at publication, rather than being promoted.
        synced = select(UniverseSymbol.symbol).where(
            UniverseSymbol.is_synced == 1,
            UniverseSymbol.asset_type == 'stock', UniverseSymbol.region == 'cn',
        )
        codes = db.scalars(synced).all()
        inactive = set(db.scalars(select(Symbol.symbol).where(Symbol.is_active != 1)).all())
        # Include active legacy symbols only when their local market history has
        # been synced. Mere metadata is not an already-synced scoring universe.
        from app.models.daily_bar import DailyBar
        legacy = stmt.where(select(DailyBar.id).where(DailyBar.symbol_id == Symbol.id).exists())
        codes = [code for code in codes if code not in inactive] + db.scalars(legacy).all()
    payload.score_symbols = sorted(set(codes))
    if not payload.score_symbols:
        raise ValueError('所选范围没有可评分的 A 股股票；请添加有效观察项、持仓或同步股票行情后重试')


def score_universe_summary(payload: FactorPipelineCreate) -> dict | None:
    if payload.universe_source is None:
        return None
    if not payload.score_symbols:
        raise ValueError('评分标的范围未冻结或为空，请重新提交评分任务')
    return {
        'source': payload.universe_source,
        'watchlist_id': payload.watchlist_id,
        'portfolio_id': payload.portfolio_id,
        'symbol_count': len(payload.score_symbols),
        'normalization': 'full_market',
    }
