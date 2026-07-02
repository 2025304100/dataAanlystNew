from __future__ import annotations

import ast
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.custom_indicator import CustomIndicator, CustomIndicatorVersion
from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.custom_indicator import (
    CustomIndicatorCreate,
    CustomIndicatorPreviewRead,
    CustomIndicatorPreviewRequest,
    CustomIndicatorPreviewSeriesItem,
    CustomIndicatorRead,
    CustomIndicatorUpdate,
)
from app.services.backtest import _resolve_formula_expr

router = APIRouter()

_ALLOWED_FORMULA_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.And, ast.Or, ast.Not, ast.USub,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)
_ALLOWED_FORMULA_FUNCTIONS = {
    "sma", "ema", "rsi", "macd", "macd_signal", "macd_hist",
    "boll_upper", "boll_mid", "boll_lower", "atr",
    "kdj_k", "kdj_d", "kdj_j", "highest", "lowest", "ref",
    "pct_change", "volume_ratio", "cross_over", "cross_under",
    "abs", "min", "max", "round",
}
_ALLOWED_FORMULA_VARIABLES = {
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "prev_close", "quality_score", "timing_score", "trend_score", "momentum_score",
    "True", "False",
}


def _json_loads(value: str | None, default):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _format_indicator(row: CustomIndicator, version: int | None = None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "key": row.key,
        "description": row.description or "",
        "category": row.category or "custom",
        "formula": row.formula,
        "value_type": row.value_type or "boolean",
        "params": _json_loads(row.params_json, []),
        "scope": _json_loads(row.scope_json, ["backtest"]),
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "version": version or _latest_version(row.id),
    }


def _latest_version(indicator_id: int, db: Session | None = None) -> int:
    if db is None:
        return 1
    value = db.execute(
        select(func.max(CustomIndicatorVersion.version)).where(CustomIndicatorVersion.indicator_id == indicator_id)
    ).scalar_one()
    return int(value or 1)


def _ensure_unique(db: Session, key: str, name: str, exclude_id: int | None = None) -> None:
    stmt = select(CustomIndicator).where(or_(CustomIndicator.key == key, CustomIndicator.name == name))
    if exclude_id is not None:
        stmt = stmt.where(CustomIndicator.id != exclude_id)
    existing = db.execute(stmt).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Indicator key or name already exists")


def _write_version(db: Session, indicator: CustomIndicator) -> int:
    latest = db.execute(
        select(func.max(CustomIndicatorVersion.version)).where(CustomIndicatorVersion.indicator_id == indicator.id)
    ).scalar_one()
    next_version = int(latest or 0) + 1
    db.add(CustomIndicatorVersion(
        indicator_id=indicator.id,
        version=next_version,
        formula=indicator.formula,
        params_json=indicator.params_json,
        value_type=indicator.value_type,
    ))
    return next_version


def _validate_formula_expr(expr: str) -> tuple[bool, str | None]:
    formula = str(expr or "").strip()
    if not formula:
        return False, "Formula is required"
    if len(formula) > 500:
        return False, "Formula is too long"
    allowed_names = _ALLOWED_FORMULA_FUNCTIONS | _ALLOWED_FORMULA_VARIABLES
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        return False, f"Syntax error: {exc.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_FORMULA_NODES):
            return False, f"Unsupported syntax: {type(node).__name__}"
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            return False, f"Unknown name or function: {node.id}"
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            return False, "Only direct function calls are allowed"
        if isinstance(node, ast.Call) and node.func.id not in _ALLOWED_FORMULA_FUNCTIONS:
            return False, f"Unsupported function: {node.func.id}"
    return True, None


def _ensure_formula_valid(expr: str) -> None:
    valid, reason = _validate_formula_expr(expr)
    if not valid:
        raise HTTPException(status_code=400, detail=reason or "Invalid formula")


def _format_preview_number(value: float) -> str:
    text = f"{value:.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _resolve_preview_bar(db: Session, symbol_id: int, trade_date):
    stmt = select(DailyBar).where(DailyBar.symbol_id == symbol_id)
    if trade_date is not None:
        stmt = stmt.where(DailyBar.trade_date == trade_date)
    stmt = stmt.order_by(DailyBar.trade_date.desc())
    return db.execute(stmt).scalars().first()


def _preview_score_payload(score: Score | None) -> dict | None:
    if score is None:
        return None
    return {
        "quality_score": float(score.quality_score) if score.quality_score is not None else None,
        "timing_score": float(score.timing_score) if score.timing_score is not None else None,
        "trend_score": float(score.trend_score) if score.trend_score is not None else None,
        "momentum_score": float(score.momentum_score) if score.momentum_score is not None else None,
    }


def _preview_series_payload(bar: DailyBar, score: Score | None, raw_value, value_type: str) -> dict:
    result_boolean: bool | None = None
    result_number: float | None = None
    display_value = "—"
    if value_type == "number":
        if raw_value is not None:
            try:
                result_number = float(raw_value)
            except (TypeError, ValueError):
                result_number = None
            else:
                display_value = _format_preview_number(result_number)
    else:
        if raw_value is not None:
            result_boolean = bool(raw_value)
            display_value = "True" if result_boolean else "False"

    return {
        "trade_date": bar.trade_date,
        "value_type": value_type,
        "result_boolean": result_boolean,
        "result_number": result_number,
        "display_value": display_value,
        "latest_bar": {
            "trade_date": bar.trade_date,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume) if bar.volume is not None else None,
        },
        "score_snapshot": _preview_score_payload(score),
    }

@router.get("/settings/custom-indicators", response_model=list[CustomIndicatorRead])
def list_custom_indicators(
    scope: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    db: Session = Depends(get_db),
):
    stmt = select(CustomIndicator).order_by(desc(CustomIndicator.updated_at), desc(CustomIndicator.id))
    if enabled is not None:
        stmt = stmt.where(CustomIndicator.enabled == enabled)
    rows = db.execute(stmt).scalars().all()
    formatted = []
    for row in rows:
        item_scope = _json_loads(row.scope_json, [])
        if scope and scope not in item_scope:
            continue
        formatted.append(_format_indicator(row, _latest_version(row.id, db)))
    return formatted


@router.post("/settings/custom-indicators", response_model=CustomIndicatorRead, status_code=201)
def create_custom_indicator(payload: CustomIndicatorCreate, db: Session = Depends(get_db)):
    _ensure_unique(db, payload.key, payload.name)
    _ensure_formula_valid(payload.formula)
    row = CustomIndicator(
        name=payload.name,
        key=payload.key,
        description=payload.description,
        category=payload.category,
        formula=payload.formula,
        value_type=payload.value_type,
        params_json=json.dumps(payload.params, ensure_ascii=False),
        scope_json=json.dumps(payload.scope, ensure_ascii=False),
        enabled=payload.enabled,
    )
    db.add(row)
    db.flush()
    version = _write_version(db, row)
    db.commit()
    db.refresh(row)
    return _format_indicator(row, version)


@router.put("/settings/custom-indicators/{indicator_id}", response_model=CustomIndicatorRead)
def update_custom_indicator(indicator_id: int, payload: CustomIndicatorUpdate, db: Session = Depends(get_db)):
    row = db.get(CustomIndicator, indicator_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Custom indicator not found")
    next_key = payload.key if payload.key is not None else row.key
    next_name = payload.name if payload.name is not None else row.name
    _ensure_unique(db, next_key, next_name, exclude_id=indicator_id)
    if payload.formula is not None:
        _ensure_formula_valid(payload.formula)

    formula_changed = False
    if payload.name is not None:
        row.name = payload.name
    if payload.key is not None:
        row.key = payload.key
    if payload.description is not None:
        row.description = payload.description
    if payload.category is not None:
        row.category = payload.category
    if payload.formula is not None and payload.formula != row.formula:
        row.formula = payload.formula
        formula_changed = True
    if payload.value_type is not None and payload.value_type != row.value_type:
        row.value_type = payload.value_type
        formula_changed = True
    if payload.params is not None:
        next_params = json.dumps(payload.params, ensure_ascii=False)
        if next_params != row.params_json:
            row.params_json = next_params
            formula_changed = True
    if payload.scope is not None:
        row.scope_json = json.dumps(payload.scope, ensure_ascii=False)
    if payload.enabled is not None:
        row.enabled = payload.enabled
    row.updated_at = datetime.now(timezone.utc)

    version = _latest_version(row.id, db)
    if formula_changed:
        db.flush()
        version = _write_version(db, row)
    db.commit()
    db.refresh(row)
    return _format_indicator(row, version)


@router.post("/settings/custom-indicators/preview", response_model=CustomIndicatorPreviewRead)
def preview_custom_indicator(payload: CustomIndicatorPreviewRequest, db: Session = Depends(get_db)):
    _ensure_formula_valid(payload.formula)
    symbol = db.get(Symbol, payload.symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")

    preview_bar = _resolve_preview_bar(db, payload.symbol_id, payload.trade_date)
    if preview_bar is None:
        if payload.trade_date is not None:
            raise HTTPException(status_code=400, detail=f"No daily bar data available on {payload.trade_date.isoformat()}")
        raise HTTPException(status_code=400, detail="No daily bar data available for preview")

    bars = (
        db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == payload.symbol_id, DailyBar.trade_date <= preview_bar.trade_date)
            .order_by(DailyBar.trade_date.desc())
            .limit(251)
        )
        .scalars()
        .all()
    )
    bars = list(reversed(bars))
    history_bars = bars[:-1]
    prev_bar = history_bars[-1] if history_bars else None

    score_rows = (
        db.execute(
            select(Score)
            .where(Score.symbol_id == payload.symbol_id, Score.trade_date <= preview_bar.trade_date)
            .order_by(Score.trade_date.desc())
            .limit(251)
        )
        .scalars()
        .all()
    )
    score_by_date = {row.trade_date: row for row in reversed(score_rows)}
    score = score_by_date.get(preview_bar.trade_date)

    raw_value = _resolve_formula_expr(score, preview_bar, prev_bar, history_bars, payload.formula)
    result_boolean: bool | None = None
    result_number: float | None = None
    if payload.value_type == "number":
        try:
            result_number = float(raw_value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Formula did not return a numeric value on the preview bar")
        display_value = _format_preview_number(result_number)
    else:
        result_boolean = bool(raw_value)
        display_value = "True" if result_boolean else "False"

    recent_count = max(1, min(int(payload.recent_count), len(bars)))
    recent_results: list[dict] = []
    for idx in range(len(bars) - 1, len(bars) - recent_count - 1, -1):
        current_bar = bars[idx]
        current_history = bars[:idx]
        current_prev_bar = bars[idx - 1] if idx > 0 else None
        current_score = score_by_date.get(current_bar.trade_date)
        try:
            current_raw_value = _resolve_formula_expr(current_score, current_bar, current_prev_bar, current_history, payload.formula)
        except Exception:
            current_raw_value = None

        series_boolean: bool | None = None
        series_number: float | None = None
        series_display = "—"
        if payload.value_type == "number":
            if current_raw_value is not None:
                try:
                    series_number = float(current_raw_value)
                except (TypeError, ValueError):
                    series_number = None
                else:
                    series_display = _format_preview_number(series_number)
        else:
            if current_raw_value is not None:
                series_boolean = bool(current_raw_value)
                series_display = "True" if series_boolean else "False"

        recent_results.append({
            "trade_date": current_bar.trade_date,
            "value_type": payload.value_type,
            "result_boolean": series_boolean,
            "result_number": series_number,
            "display_value": series_display,
            "latest_bar": {
                "trade_date": current_bar.trade_date,
                "open": float(current_bar.open),
                "high": float(current_bar.high),
                "low": float(current_bar.low),
                "close": float(current_bar.close),
                "volume": float(current_bar.volume) if current_bar.volume is not None else None,
            },
            "score_snapshot": _preview_score_payload(current_score),
        })

    return {
        "ok": True,
        "message": "Preview ready",
        "symbol_id": symbol.id,
        "symbol": symbol.symbol,
        "name": symbol.name,
        "trade_date": preview_bar.trade_date,
        "value_type": payload.value_type,
        "result_boolean": result_boolean,
        "result_number": result_number,
        "display_value": display_value,
        "latest_bar": {
            "trade_date": preview_bar.trade_date,
            "open": float(preview_bar.open),
            "high": float(preview_bar.high),
            "low": float(preview_bar.low),
            "close": float(preview_bar.close),
            "volume": float(preview_bar.volume) if preview_bar.volume is not None else None,
        },
        "score_snapshot": _preview_score_payload(score),
        "recent_results": recent_results,
    }


@router.delete("/settings/custom-indicators/{indicator_id}")
def delete_custom_indicator(indicator_id: int, db: Session = Depends(get_db)):
    row = db.get(CustomIndicator, indicator_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Custom indicator not found")
    db.query(CustomIndicatorVersion).filter(CustomIndicatorVersion.indicator_id == indicator_id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()
    return {"success": True}