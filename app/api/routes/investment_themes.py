from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.investment_theme import InvestmentThemeCreate, ThemeCatalystCreate, ThemeSymbolMappingCreate
from app.services.investment_themes import add_catalyst, add_symbol_mapping, create_theme, list_themes


router = APIRouter()


@router.get("/investment-themes")
def get_themes(active_only: bool = Query(default=True), db: Session = Depends(get_db)):
    return list_themes(db, active_only=active_only)


@router.post("/investment-themes", status_code=201)
def create_investment_theme(payload: InvestmentThemeCreate, db: Session = Depends(get_db)):
    try:
        return create_theme(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/investment-themes/{theme_id}/symbols", status_code=201)
def map_theme_symbol(theme_id: int, payload: ThemeSymbolMappingCreate, db: Session = Depends(get_db)):
    try:
        return add_symbol_mapping(db, theme_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/investment-themes/{theme_id}/catalysts", status_code=201)
def create_theme_catalyst(theme_id: int, payload: ThemeCatalystCreate, db: Session = Depends(get_db)):
    try:
        return add_catalyst(db, theme_id, payload)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
