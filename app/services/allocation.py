from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.symbol import Symbol


def get_default_portfolio(db: Session) -> Portfolio | None:
    stmt = select(Portfolio).where(Portfolio.is_default == 1).order_by(Portfolio.id.asc())
    return db.execute(stmt).scalars().first()


def get_active_rule(db: Session, portfolio_id: int) -> PortfolioRule | None:
    stmt = (
        select(PortfolioRule)
        .where(PortfolioRule.portfolio_id == portfolio_id, PortfolioRule.is_active == 1)
        .order_by(PortfolioRule.id.desc())
    )
    return db.execute(stmt).scalars().first()


def compute_allocation(db: Session, portfolio_id: int) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    positions = db.execute(select(Position).where(Position.portfolio_id == portfolio_id)).scalars().all()
    stock_position_pct = sum(p.position_pct for p in positions if p.asset_type == "stock")
    etf_position_pct = sum(p.position_pct for p in positions if p.asset_type == "etf")
    total_position_pct = stock_position_pct + etf_position_pct
    sector_exposure: dict[str, float] = {}
    for position in positions:
        key = position.theme or "unclassified"
        sector_exposure[key] = sector_exposure.get(key, 0.0) + position.position_pct

    return {
        "total_position_pct": round(total_position_pct, 4),
        "stock_position_pct": round(stock_position_pct, 4),
        "etf_position_pct": round(etf_position_pct, 4),
        "cash_pct": round(max(0.0, 1 - total_position_pct), 4),
        "position_count": len(positions),
        "sector_exposure": sector_exposure,
    }


def compute_recommended_position_pct(
    db: Session,
    portfolio_id: int,
    symbol: Symbol,
    stage: str,
) -> tuple[float, bool, bool]:
    allocation = compute_allocation(db, portfolio_id)
    rule = get_active_rule(db, portfolio_id)
    if rule is None:
        return 0.0, False, False

    stage_limits = json.loads(rule.stage_limits_json)
    asset_stage_limits = stage_limits.get(symbol.asset_type, {})
    stage_limit = float(asset_stage_limits.get(stage, 0.0))
    single_limit = float(rule.max_single_position_pct)
    sector_limit = float(rule.max_sector_position_pct)
    asset_limit = float(rule.max_stock_position_pct if symbol.asset_type == "stock" else rule.max_etf_position_pct)

    sector_used = allocation["sector_exposure"].get(symbol.theme or "unclassified", 0.0)
    asset_used = allocation["stock_position_pct"] if symbol.asset_type == "stock" else allocation["etf_position_pct"]

    sector_remaining = max(0.0, sector_limit - sector_used)
    asset_remaining = max(0.0, asset_limit - asset_used)
    recommended = min(single_limit, stage_limit, sector_remaining, asset_remaining)
    is_sector_overweight = sector_used >= sector_limit
    is_asset_overweight = asset_used >= asset_limit

    return round(max(0.0, recommended), 4), is_sector_overweight, is_asset_overweight
