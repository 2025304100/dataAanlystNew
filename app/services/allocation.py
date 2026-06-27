from __future__ import annotations

import json
from math import floor

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


def _sector_key(symbol_or_position: Symbol | Position) -> str:
    return getattr(symbol_or_position, "theme", None) or "unclassified"


def _active_position(db: Session, portfolio_id: int, symbol_id: int) -> Position | None:
    return db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol_id == symbol_id)
    ).scalars().first()


def compute_allocation(db: Session, portfolio_id: int) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    positions = db.execute(select(Position).where(Position.portfolio_id == portfolio_id)).scalars().all()
    total_capital = float(portfolio.total_capital or 0)
    investable_capital = round(total_capital * float(portfolio.investable_ratio or 0), 2)
    used_amount = round(sum(float(p.market_value or 0) for p in positions), 2)
    stock_amount = round(sum(float(p.market_value or 0) for p in positions if p.asset_type == "stock"), 2)
    etf_amount = round(sum(float(p.market_value or 0) for p in positions if p.asset_type == "etf"), 2)

    stock_position_pct = stock_amount / total_capital if total_capital else 0.0
    etf_position_pct = etf_amount / total_capital if total_capital else 0.0
    total_position_pct = used_amount / total_capital if total_capital else 0.0

    sector_exposure: dict[str, float] = {}
    sector_amount: dict[str, float] = {}
    for position in positions:
        key = _sector_key(position)
        sector_exposure[key] = sector_exposure.get(key, 0.0) + float(position.position_pct or 0)
        sector_amount[key] = sector_amount.get(key, 0.0) + float(position.market_value or 0)

    return {
        "total_capital": total_capital,
        "investable_capital": investable_capital,
        "used_amount": used_amount,
        "cash_amount": round(max(0.0, total_capital - used_amount), 2),
        "investable_remaining_amount": round(max(0.0, investable_capital - used_amount), 2),
        "total_position_pct": round(total_position_pct, 4),
        "stock_position_pct": round(stock_position_pct, 4),
        "etf_position_pct": round(etf_position_pct, 4),
        "cash_pct": round(max(0.0, 1 - total_position_pct), 4),
        "investable_remaining_pct": round(max(0.0, float(portfolio.investable_ratio or 0) - total_position_pct), 4),
        "position_count": len(positions),
        "sector_exposure": {key: round(value, 4) for key, value in sector_exposure.items()},
        "sector_amount": {key: round(value, 2) for key, value in sector_amount.items()},
        "stock_amount": stock_amount,
        "etf_amount": etf_amount,
    }


def compute_position_budget(
    db: Session,
    portfolio_id: int,
    symbol: Symbol,
    stage: str,
    action: str | None = None,
    entry_price: float | None = None,
    stop_loss: float | None = None,
) -> dict:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    allocation = compute_allocation(db, portfolio_id)
    rule = get_active_rule(db, portfolio_id)
    position = _active_position(db, portfolio_id, symbol.id)
    current_pct = round(float(position.position_pct or 0), 4) if position is not None else 0.0
    current_amount = round(float(position.market_value or 0), 2) if position is not None else 0.0
    total_capital = float(portfolio.total_capital or 0)
    investable_ratio = float(portfolio.investable_ratio or 0)

    if rule is None:
        return {
            "recommended_pct": 0.0,
            "recommended_amount": 0.0,
            "can_open": False,
            "decision": "blocked",
            "blocked_reasons": ["no_active_rule"],
            "constraints": [],
            "allocation": allocation,
            "current_position_pct": current_pct,
            "current_position_amount": current_amount,
        }

    stage_limits = json.loads(rule.stage_limits_json or "{}")
    stage_limit = float(stage_limits.get(symbol.asset_type, {}).get(stage, 0.0))
    single_limit = float(rule.max_single_position_pct or 0)
    sector_limit = float(rule.max_sector_position_pct or 0)
    asset_limit = float(rule.max_stock_position_pct if symbol.asset_type == "stock" else rule.max_etf_position_pct)
    max_loss_pct = float(rule.max_loss_per_trade_pct or 0)

    sector_key = _sector_key(symbol)
    sector_used = float(allocation["sector_exposure"].get(sector_key, 0.0))
    asset_used = float(allocation["stock_position_pct"] if symbol.asset_type == "stock" else allocation["etf_position_pct"])
    investable_used = float(allocation["total_position_pct"])
    open_slots_remaining = int(rule.max_open_positions or 0) - int(allocation["position_count"])
    has_position = position is not None and current_pct > 0

    constraints = [
        {
            "key": "single",
            "limit_pct": round(single_limit, 4),
            "used_pct": current_pct,
            "remaining_pct": round(max(0.0, single_limit - current_pct), 4),
        },
        {
            "key": "stage",
            "limit_pct": round(stage_limit, 4),
            "used_pct": current_pct,
            "remaining_pct": round(max(0.0, stage_limit - current_pct), 4),
        },
        {
            "key": "sector",
            "limit_pct": round(sector_limit, 4),
            "used_pct": round(sector_used, 4),
            "remaining_pct": round(max(0.0, sector_limit - sector_used), 4),
        },
        {
            "key": "asset",
            "limit_pct": round(asset_limit, 4),
            "used_pct": round(asset_used, 4),
            "remaining_pct": round(max(0.0, asset_limit - asset_used), 4),
        },
        {
            "key": "investable",
            "limit_pct": round(investable_ratio, 4),
            "used_pct": round(investable_used, 4),
            "remaining_pct": round(max(0.0, investable_ratio - investable_used), 4),
        },
    ]

    risk_budget_amount = round(total_capital * max_loss_pct, 2)
    risk_per_share = None
    risk_capped_shares = None
    risk_capped_amount = None
    if entry_price is not None and stop_loss is not None and entry_price > stop_loss:
        risk_per_share = round(entry_price - stop_loss, 4)
        risk_capped_shares = floor(risk_budget_amount / risk_per_share) if risk_per_share > 0 else None
        if risk_capped_shares is not None:
            risk_capped_amount = round(risk_capped_shares * entry_price, 2)
            constraints.append(
                {
                    "key": "risk",
                    "limit_pct": round(risk_capped_amount / total_capital, 4) if total_capital else 0.0,
                    "used_pct": 0.0,
                    "remaining_pct": round(risk_capped_amount / total_capital, 4) if total_capital else 0.0,
                    "amount": risk_capped_amount,
                }
            )

    if not has_position and open_slots_remaining <= 0:
        constraints.append({"key": "open_slots", "limit_pct": 0.0, "used_pct": 1.0, "remaining_pct": 0.0})

    blocked_reasons: list[str] = []
    if action in {"reduce", "exit"} or stage == "overheat":
        blocked_reasons.append("signal_not_buyable")
    if stage_limit <= 0:
        blocked_reasons.append("stage_not_allowed")
    if not has_position and open_slots_remaining <= 0:
        blocked_reasons.append("max_open_positions")
    if sector_used >= sector_limit:
        blocked_reasons.append("sector_overweight")
    if asset_used >= asset_limit:
        blocked_reasons.append("asset_overweight")
    if investable_used >= investable_ratio:
        blocked_reasons.append("investable_cap_reached")

    remaining_values = [float(item["remaining_pct"]) for item in constraints]
    recommended_pct = round(max(0.0, min(remaining_values) if remaining_values else 0.0), 4)
    if blocked_reasons:
        recommended_pct = 0.0

    recommended_amount = round(total_capital * recommended_pct, 2)
    decision = "buy_allowed" if recommended_pct > 0 and not blocked_reasons else "blocked"
    if recommended_pct == 0 and not blocked_reasons:
        decision = "wait"

    return {
        "recommended_pct": recommended_pct,
        "recommended_amount": recommended_amount,
        "can_open": decision == "buy_allowed",
        "decision": decision,
        "blocked_reasons": blocked_reasons,
        "constraints": constraints,
        "allocation": allocation,
        "current_position_pct": current_pct,
        "current_position_amount": current_amount,
        "open_slots_remaining": max(0, open_slots_remaining),
        "has_position": has_position,
        "stage_limit_pct": round(stage_limit, 4),
        "single_limit_pct": round(single_limit, 4),
        "sector_limit_pct": round(sector_limit, 4),
        "asset_limit_pct": round(asset_limit, 4),
        "risk_budget_amount": risk_budget_amount,
        "risk_per_share": risk_per_share,
        "risk_capped_shares": risk_capped_shares,
        "risk_capped_amount": risk_capped_amount,
        "is_sector_overweight": sector_used >= sector_limit,
        "is_asset_overweight": asset_used >= asset_limit,
    }


def compute_recommended_position_pct(
    db: Session,
    portfolio_id: int,
    symbol: Symbol,
    stage: str,
    action: str | None = None,
    entry_price: float | None = None,
    stop_loss: float | None = None,
) -> tuple[float, bool, bool]:
    budget = compute_position_budget(
        db=db,
        portfolio_id=portfolio_id,
        symbol=symbol,
        stage=stage,
        action=action,
        entry_price=entry_price,
        stop_loss=stop_loss,
    )
    return round(float(budget["recommended_pct"]), 4), bool(budget["is_sector_overweight"]), bool(budget["is_asset_overweight"])
