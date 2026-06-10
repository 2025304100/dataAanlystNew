from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.journal_entry import JournalEntry
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.schemas.journal import JournalCreate, JournalRead, JournalUpdate


router = APIRouter()


@router.get("/journals", response_model=list[JournalRead])
def list_journals(
    portfolio_id: int | None = Query(default=None),
    symbol_id: int | None = Query(default=None),
    entry_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    stmt = select(JournalEntry)
    if portfolio_id is not None:
        stmt = stmt.where(JournalEntry.portfolio_id == portfolio_id)
    if symbol_id is not None:
        stmt = stmt.where(JournalEntry.symbol_id == symbol_id)
    if entry_type is not None:
        stmt = stmt.where(JournalEntry.entry_type == entry_type)
    stmt = stmt.order_by(JournalEntry.created_at.desc(), JournalEntry.id.desc())
    return db.execute(stmt).scalars().all()


@router.post("/journals", response_model=JournalRead)
def create_journal(payload: JournalCreate, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, payload.portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if portfolio is None or symbol is None:
        raise HTTPException(status_code=404, detail="Portfolio or symbol not found")
    if payload.trade_setup_id is not None and db.get(TradeSetup, payload.trade_setup_id) is None:
        raise HTTPException(status_code=404, detail="Trade setup not found")

    journal = JournalEntry(
        portfolio_id=payload.portfolio_id,
        symbol_id=payload.symbol_id,
        trade_setup_id=payload.trade_setup_id,
        entry_type=payload.entry_type,
        title=payload.title,
        content=payload.content,
        subjective_view=payload.subjective_view,
        follow_system=int(payload.follow_system),
        outcome=payload.outcome,
        review_note=payload.review_note,
    )
    db.add(journal)
    db.commit()
    db.refresh(journal)
    return journal


@router.patch("/journals/{journal_id}", response_model=JournalRead)
def update_journal(journal_id: int, payload: JournalUpdate, db: Session = Depends(get_db)):
    journal = db.get(JournalEntry, journal_id)
    if journal is None:
        raise HTTPException(status_code=404, detail="Journal not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "follow_system" and value is not None:
            setattr(journal, key, int(value))
        else:
            setattr(journal, key, value)

    db.commit()
    db.refresh(journal)
    return journal
