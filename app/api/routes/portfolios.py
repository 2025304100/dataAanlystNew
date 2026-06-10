import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.symbol import Symbol
from app.schemas.portfolio import (
    AllocationSummary,
    PortfolioCreate,
    PortfolioRead,
    PortfolioRuleUpsert,
    PositionRead,
    PositionUpsert,
)
from app.services.allocation import compute_allocation, get_active_rule
from app.services.sim_accounts import ensure_sim_account_seed


router = APIRouter()


@router.get("/portfolios", response_model=list[PortfolioRead])
def list_portfolios(db: Session = Depends(get_db)):
    return db.execute(select(Portfolio).order_by(Portfolio.id.desc())).scalars().all()


@router.post("/portfolios", response_model=PortfolioRead)
def create_portfolio(payload: PortfolioCreate, db: Session = Depends(get_db)):
    portfolio = Portfolio(
        name=payload.name,
        account_type=payload.account_type,
        total_capital=payload.total_capital,
        investable_ratio=payload.investable_ratio,
        cash_reserve_ratio=payload.cash_reserve_ratio,
        currency=payload.currency,
        is_default=int(payload.is_default),
    )
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    if portfolio.account_type == "simulated":
        ensure_sim_account_seed(db, portfolio)
        db.commit()
    return portfolio


@router.get("/portfolios/{portfolio_id}")
def get_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    rule = get_active_rule(db, portfolio_id)
    allocation = compute_allocation(db, portfolio_id)
    return {
        "portfolio": PortfolioRead.model_validate(portfolio),
        "active_rule": None
        if rule is None
        else {
            "id": rule.id,
            "rule_name": rule.rule_name,
            "max_single_position_pct": rule.max_single_position_pct,
            "max_sector_position_pct": rule.max_sector_position_pct,
            "max_stock_position_pct": rule.max_stock_position_pct,
            "max_etf_position_pct": rule.max_etf_position_pct,
            "max_loss_per_trade_pct": rule.max_loss_per_trade_pct,
            "max_open_positions": rule.max_open_positions,
            "stage_limits_json": json.loads(rule.stage_limits_json),
        },
        "allocation": allocation,
    }


@router.post("/portfolios/{portfolio_id}/rules")
def upsert_portfolio_rule(portfolio_id: int, payload: PortfolioRuleUpsert, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    if payload.is_active:
        active_rules = db.execute(select(PortfolioRule).where(PortfolioRule.portfolio_id == portfolio_id)).scalars().all()
        for rule in active_rules:
            rule.is_active = 0

    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name=payload.rule_name,
        max_single_position_pct=payload.max_single_position_pct,
        max_sector_position_pct=payload.max_sector_position_pct,
        max_stock_position_pct=payload.max_stock_position_pct,
        max_etf_position_pct=payload.max_etf_position_pct,
        max_loss_per_trade_pct=payload.max_loss_per_trade_pct,
        max_open_positions=payload.max_open_positions,
        stage_limits_json=json.dumps(payload.stage_limits_json, ensure_ascii=True),
        is_active=int(payload.is_active),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {"id": rule.id, "portfolio_id": portfolio_id}


@router.get("/portfolios/{portfolio_id}/allocation", response_model=AllocationSummary)
def get_allocation(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    summary = compute_allocation(db, portfolio_id)
    rule = get_active_rule(db, portfolio_id)
    if rule is None:
        summary["remaining_stock_pct"] = 0.0
        summary["remaining_etf_pct"] = 0.0
    else:
        summary["remaining_stock_pct"] = round(max(0.0, rule.max_stock_position_pct - summary["stock_position_pct"]), 4)
        summary["remaining_etf_pct"] = round(max(0.0, rule.max_etf_position_pct - summary["etf_position_pct"]), 4)
    return AllocationSummary(**summary)


@router.get("/portfolios/{portfolio_id}/positions", response_model=list[PositionRead])
def list_positions(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return db.execute(select(Position).where(Position.portfolio_id == portfolio_id).order_by(Position.id.desc())).scalars().all()


@router.post("/portfolios/{portfolio_id}/positions", response_model=PositionRead)
def upsert_position(portfolio_id: int, payload: PositionUpsert, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if portfolio is None or symbol is None:
        raise HTTPException(status_code=404, detail="Portfolio or symbol not found")

    position = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol_id == payload.symbol_id)
    ).scalars().first()
    if position is None:
        position = Position(
            portfolio_id=portfolio_id,
            symbol_id=payload.symbol_id,
            asset_type=symbol.asset_type,
            theme=symbol.theme,
        )
        db.add(position)

    market_value = payload.quantity * payload.latest_price
    position_pct = market_value / portfolio.total_capital if portfolio.total_capital else 0
    position.quantity = payload.quantity
    position.avg_cost = payload.avg_cost
    position.latest_price = payload.latest_price
    position.market_value = market_value
    position.position_pct = round(position_pct, 4)
    position.asset_type = symbol.asset_type
    position.theme = symbol.theme
    db.commit()
    db.refresh(position)
    return position


@router.delete("/portfolios/{portfolio_id}/positions/{symbol_id}")
def delete_position(portfolio_id: int, symbol_id: int, db: Session = Depends(get_db)):
    position = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol_id == symbol_id)
    ).scalars().first()
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    db.delete(position)
    db.commit()
    return {"deleted": True}
