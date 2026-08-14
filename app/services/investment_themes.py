from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.investment_theme import InvestmentTheme, SymbolThemeMapping, ThemeCatalyst
from app.models.market_event import MarketEvent
from app.models.symbol import Symbol
from app.schemas.investment_theme import InvestmentThemeCreate, ThemeCatalystCreate, ThemeSymbolMappingCreate


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def theme_to_dict(theme: InvestmentTheme) -> dict[str, Any]:
    return {
        "id": theme.id,
        "code": theme.code,
        "name": theme.name,
        "description": theme.description,
        "source_type": theme.source_type,
        "is_active": bool(theme.is_active),
        "created_at": theme.created_at.isoformat() if theme.created_at else None,
        "updated_at": theme.updated_at.isoformat() if theme.updated_at else None,
    }


def list_themes(db: Session, *, active_only: bool = True) -> list[dict[str, Any]]:
    stmt = select(InvestmentTheme)
    if active_only:
        stmt = stmt.where(InvestmentTheme.is_active == 1)
    rows = db.execute(stmt.order_by(InvestmentTheme.name.asc())).scalars().all()
    return [theme_to_dict(row) for row in rows]


def create_theme(db: Session, payload: InvestmentThemeCreate) -> dict[str, Any]:
    code = payload.code.strip().lower().replace(" ", "-")
    existing = db.execute(
        select(InvestmentTheme).where(
            or_(InvestmentTheme.code == code, InvestmentTheme.name == payload.name.strip())
        )
    ).scalars().first()
    if existing is not None:
        raise ValueError("theme code or name already exists")
    theme = InvestmentTheme(
        code=code,
        name=payload.name.strip(),
        description=payload.description,
        source_type=payload.source_type,
        is_active=1,
    )
    db.add(theme)
    db.commit()
    db.refresh(theme)
    return theme_to_dict(theme)


def add_symbol_mapping(db: Session, theme_id: int, payload: ThemeSymbolMappingCreate) -> dict[str, Any]:
    theme = db.get(InvestmentTheme, theme_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if theme is None or symbol is None:
        raise LookupError("theme or symbol not found")
    mapping = db.execute(
        select(SymbolThemeMapping).where(
            SymbolThemeMapping.theme_id == theme_id,
            SymbolThemeMapping.symbol_id == payload.symbol_id,
        )
    ).scalars().first()
    if mapping is None:
        mapping = SymbolThemeMapping(theme_id=theme_id, symbol_id=payload.symbol_id)
        db.add(mapping)
    mapping.source_type = payload.source_type
    mapping.source_ref = payload.source_ref
    mapping.confidence = payload.confidence
    mapping.is_confirmed = int(payload.is_confirmed)
    db.commit()
    db.refresh(mapping)
    return {
        "id": mapping.id,
        "theme_id": mapping.theme_id,
        "theme_name": theme.name,
        "symbol_id": mapping.symbol_id,
        "symbol": symbol.symbol,
        "source_type": mapping.source_type,
        "source_ref": mapping.source_ref,
        "confidence": mapping.confidence,
        "is_confirmed": bool(mapping.is_confirmed),
    }


def add_catalyst(db: Session, theme_id: int, payload: ThemeCatalystCreate) -> dict[str, Any]:
    theme = db.get(InvestmentTheme, theme_id)
    if theme is None:
        raise LookupError("theme not found")
    event = db.get(MarketEvent, payload.market_event_id) if payload.market_event_id is not None else None
    if payload.market_event_id is not None and event is None:
        raise LookupError("market event not found")
    catalyst = ThemeCatalyst(
        theme_id=theme_id,
        market_event_id=event.id if event else None,
        title_snapshot=event.title if event else payload.title.strip(),
        catalyst_score=payload.catalyst_score,
        confidence=payload.confidence,
        source_type=payload.source_type if not event else event.source,
        source_ref=payload.source_ref or (event.source_url if event else None),
        published_at=payload.published_at or (event.published_at if event else None),
        expires_at=payload.expires_at or (event.expires_at if event else None),
    )
    db.add(catalyst)
    db.commit()
    db.refresh(catalyst)
    return {
        "id": catalyst.id,
        "theme_id": theme_id,
        "theme_name": theme.name,
        "market_event_id": catalyst.market_event_id,
        "title": catalyst.title_snapshot,
        "catalyst_score": catalyst.catalyst_score,
        "confidence": catalyst.confidence,
        "source_type": catalyst.source_type,
        "source_ref": catalyst.source_ref,
        "published_at": catalyst.published_at.isoformat() if catalyst.published_at else None,
        "expires_at": catalyst.expires_at.isoformat() if catalyst.expires_at else None,
    }


def get_active_theme_opportunities(
    db: Session,
    symbol_ids: list[int] | None = None,
    *,
    min_catalyst_score: float = 60,
    min_mapping_confidence: float = 0.6,
) -> dict[int, dict[str, Any]]:
    """Return the strongest auditable theme opportunity for each symbol."""
    if symbol_ids is not None and not symbol_ids:
        return {}
    now = _now()
    rows = db.execute(
        select(SymbolThemeMapping, InvestmentTheme, ThemeCatalyst)
        .join(InvestmentTheme, InvestmentTheme.id == SymbolThemeMapping.theme_id)
        .join(ThemeCatalyst, ThemeCatalyst.theme_id == InvestmentTheme.id)
        .where(
            SymbolThemeMapping.is_confirmed == 1,
            SymbolThemeMapping.confidence >= min_mapping_confidence,
            InvestmentTheme.is_active == 1,
            ThemeCatalyst.catalyst_score >= min_catalyst_score,
            or_(ThemeCatalyst.expires_at.is_(None), ThemeCatalyst.expires_at >= now),
        )
        .order_by(ThemeCatalyst.catalyst_score.desc(), ThemeCatalyst.published_at.desc())
    ).all()
    opportunities: dict[int, dict[str, Any]] = {}
    for mapping, theme, catalyst in rows:
        if mapping.symbol_id in opportunities:
            continue
        opportunities[mapping.symbol_id] = {
            "theme_id": theme.id,
            "code": theme.code,
            "name": theme.name,
            "mapping": {
                "source_type": mapping.source_type,
                "source_ref": mapping.source_ref,
                "confidence": mapping.confidence,
                "is_confirmed": bool(mapping.is_confirmed),
            },
            "catalyst": {
                "id": catalyst.id,
                "market_event_id": catalyst.market_event_id,
                "title": catalyst.title_snapshot,
                "score": catalyst.catalyst_score,
                "confidence": catalyst.confidence,
                "source_type": catalyst.source_type,
                "source_ref": catalyst.source_ref,
                "published_at": catalyst.published_at.isoformat() if catalyst.published_at else None,
                "expires_at": catalyst.expires_at.isoformat() if catalyst.expires_at else None,
            },
        }
    return opportunities
