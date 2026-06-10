from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.models.sim_account import CashLedger
from app.models.symbol import Symbol
from app.schemas.sim_account import (
    CashLedgerRead,
    SimAccountSnapshot,
    SimAccountSummary,
    SimOrderCreate,
    SimOrderExecutionResult,
    SimOrderRead,
    SimTradeRead,
)
from app.services.sim_accounts import build_sim_account_summary, place_sim_order, recent_sim_trades


router = APIRouter()


def _get_portfolio_or_404(db: Session, portfolio_id: int) -> Portfolio:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


@router.get("/portfolios/{portfolio_id}/sim-account", response_model=SimAccountSnapshot)
def get_sim_account_snapshot(
    portfolio_id: int,
    trade_limit: int = Query(default=8, ge=1, le=30),
    ledger_limit: int = Query(default=8, ge=1, le=30),
    db: Session = Depends(get_db),
):
    portfolio = _get_portfolio_or_404(db, portfolio_id)
    summary = SimAccountSummary(**build_sim_account_summary(db, portfolio))
    db.flush()
    recent_ledger = db.execute(
        select(CashLedger)
        .where(CashLedger.portfolio_id == portfolio_id)
        .order_by(desc(CashLedger.id))
        .limit(ledger_limit)
    ).scalars().all()
    return SimAccountSnapshot(
        summary=summary,
        recent_trades=[SimTradeRead(**item) for item in recent_sim_trades(db, portfolio_id, limit=trade_limit)],
        recent_ledger=[CashLedgerRead.model_validate(item) for item in recent_ledger],
    )


@router.post("/portfolios/{portfolio_id}/sim-orders", response_model=SimOrderExecutionResult)
def create_sim_order(portfolio_id: int, payload: SimOrderCreate, db: Session = Depends(get_db)):
    portfolio = _get_portfolio_or_404(db, portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")

    order, trade = place_sim_order(
        db=db,
        portfolio=portfolio,
        symbol=symbol,
        side=payload.side,
        quantity=payload.quantity,
        price=payload.price,
        order_type=payload.order_type,
        note=payload.note,
    )
    summary = build_sim_account_summary(db, portfolio)
    db.commit()
    db.refresh(order)
    db.refresh(trade)
    return SimOrderExecutionResult(
        order=SimOrderRead.model_validate(order),
        trade=SimTradeRead(
            **{
                "id": trade.id,
                "symbol_id": trade.symbol_id,
                "symbol": symbol.symbol,
                "name": symbol.name,
                "side": trade.side,
                "quantity": trade.quantity,
                "price": trade.price,
                "amount": trade.amount,
                "fee": trade.fee,
                "realized_pnl": trade.realized_pnl,
                "created_at": trade.created_at,
            }
        ),
        summary=SimAccountSummary(**summary),
    )
