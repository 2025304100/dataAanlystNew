from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.scan import ScanResult
from app.models.symbol import Symbol
from app.models.custom_indicator import CustomIndicator
from app.models.score import Score
from app.schemas.discovery import DiscoveryIndicatorEvaluateRequest
from app.services.backtest import _resolve_formula_expr
from app.schemas.discovery import DiscoveryResultUpdate
from app.services.analysis import calculate_symbol_score
from app.services.market_data import sync_symbol_daily_bars
from app.services.symbol_names import refresh_symbol_name


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_datetime(value):
    """Safely convert a value to datetime, handling strings from MySQL."""
    from datetime import datetime
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _serialize_result(result: ScanResult) -> dict:
    age_days = max(0, (_now() - _safe_datetime(result.created_at)).days)
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


def evaluate_discovery_indicators(db: Session, payload: DiscoveryIndicatorEvaluateRequest) -> list[dict]:
    result_ids = [int(item) for item in payload.scan_result_ids if int(item) > 0]
    indicator_keys = [str(item).strip() for item in payload.indicator_keys if str(item).strip()]
    if not result_ids or not indicator_keys:
        return []

    indicator_rows = db.execute(
        select(CustomIndicator).where(
            CustomIndicator.key.in_(indicator_keys),
            CustomIndicator.enabled == True,
        )
    ).scalars().all()
    indicator_map = {row.key: row for row in indicator_rows}
    if not indicator_map:
        return []

    rows = db.execute(
        select(ScanResult, Symbol)
        .join(Symbol, Symbol.id == ScanResult.symbol_id)
        .where(ScanResult.id.in_(result_ids))
    ).all()
    if not rows:
        return []

    response: list[dict] = []
    for scan_result, symbol in rows:
        latest_bar = (
            db.execute(select(DailyBar).where(DailyBar.symbol_id == symbol.id).order_by(DailyBar.trade_date.desc()))
            .scalars()
            .first()
        )
        values: dict[str, bool | float | None] = {}
        if latest_bar is not None:
            history_bars = (
                db.execute(
                    select(DailyBar)
                    .where(
                        DailyBar.symbol_id == symbol.id,
                        DailyBar.trade_date < latest_bar.trade_date,
                    )
                    .order_by(DailyBar.trade_date.desc())
                    .limit(250)
                )
                .scalars()
                .all()
            )
            history_bars = list(reversed(history_bars))
            prev_bar = history_bars[-1] if history_bars else None
            score = (
                db.execute(
                    select(Score)
                    .where(
                        Score.symbol_id == symbol.id,
                        Score.trade_date <= latest_bar.trade_date,
                    )
                    .order_by(Score.trade_date.desc())
                )
                .scalars()
                .first()
            )
            for key in indicator_keys:
                indicator = indicator_map.get(key)
                if indicator is None:
                    values[key] = None
                    continue
                result = _resolve_formula_expr(score, latest_bar, prev_bar, history_bars, indicator.formula)
                if indicator.value_type == "number":
                    try:
                        values[key] = float(result)
                    except (TypeError, ValueError):
                        values[key] = None
                else:
                    values[key] = bool(result)
        else:
            for key in indicator_keys:
                values[key] = None

        response.append({
            "scan_result_id": scan_result.id,
            "symbol_id": symbol.id,
            "values": values,
        })
    return response