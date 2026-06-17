from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.scan import ScanResult
from app.models.symbol import Symbol
from app.schemas.discovery import DiscoveryResultUpdate
from app.services.analysis import calculate_symbol_score
from app.services.market_data import sync_symbol_daily_bars
from app.services.symbol_names import refresh_symbol_name


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _serialize_result(result: ScanResult) -> dict:
    age_days = max(0, (_now() - result.created_at).days)
    return {
        "id": result.id,
        "scan_run_id": result.scan_run_id,
        "symbol_id": result.symbol_id,
        "is_frozen": bool(result.is_frozen),
        "warning_days": result.warning_days,
        "valid_days": result.valid_days,
        "age_days": age_days,
        "is_warning": not result.is_frozen and age_days >= result.warning_days,
        "is_expired": not result.is_frozen and age_days >= result.valid_days,
        "created_at": result.created_at,
    }


def update_discovery_result(db: Session, scan_result_id: int, payload: DiscoveryResultUpdate) -> dict | None:
    result = db.get(ScanResult, scan_result_id)
    if result is None:
        return None
    if payload.is_frozen is not None:
        result.is_frozen = int(payload.is_frozen)
    if payload.warning_days is not None:
        result.warning_days = payload.warning_days
    if payload.valid_days is not None:
        result.valid_days = payload.valid_days
    db.commit()
    db.refresh(result)
    return _serialize_result(result)


def update_discovery_symbol(db: Session, scan_result_id: int) -> dict | None:
    result = db.get(ScanResult, scan_result_id)
    if result is None:
        return None
    if result.is_frozen:
        payload = _serialize_result(result)
        payload["skipped"] = True
        payload["reason"] = "frozen"
        return payload
    symbol = db.get(Symbol, result.symbol_id)
    if symbol is None:
        return None
    refresh_symbol_name(symbol)
    sync_symbol_daily_bars(db=db, symbol=symbol)
    latest_bar = (
        db.execute(select(DailyBar).where(DailyBar.symbol_id == symbol.id).order_by(DailyBar.trade_date.desc()))
        .scalars()
        .first()
    )
    if latest_bar is not None:
        score = calculate_symbol_score(db=db, symbol=symbol, trade_date=latest_bar.trade_date)
        result.quality_score = score.quality_score
        result.timing_score = score.timing_score
        result.priority_score = score.priority_score
        result.stage = score.stage
        result.action = score.action
        result.created_at = _now()
    db.commit()
    db.refresh(result)
    return _serialize_result(result)
