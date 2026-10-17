"""Guarded, caller-committed corrections with atomic before/after evidence."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from numbers import Real
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.universe import UniverseDailyBar
from app.services.data_governance_audit import write_audit_event


_MODELS = {"daily_bars": DailyBar, "universe_daily_bars": UniverseDailyBar}
_FIELDS = ("open", "high", "low", "close", "volume", "amount", "turnover_rate", "source")


def bar_snapshot(bar) -> dict[str, Any]:
    key = "symbol_id" if isinstance(bar, DailyBar) else "universe_symbol_id"
    return {"id": bar.id, key: getattr(bar, key), "trade_date": str(bar.trade_date),
        "created_at": bar.created_at.isoformat(), **{f: getattr(bar, f) for f in _FIELDS}}


@dataclass(frozen=True)
class VerifiedBarCorrection:
    table: str
    row_id: int
    expected: Mapping[str, Any]
    replacement: Mapping[str, Any]


def apply_verified_bar_corrections(
    db: Session, corrections: Sequence[VerifiedBarCorrection], *, operator_id: str,
    correlation_id: str, evidence: Mapping[str, Any],
) -> list[int]:
    """Apply only pre-reviewed exact rows; caller owns commit/rollback.

    Trade dates/IDs cannot be changed. Validate and lock every expected row
    before writing any, so concurrent updates abort the whole plan. Provider
    evidence must include immutable raw response digests and explicit units.
    """
    if not operator_id or not correlation_id or not corrections or len(corrections) > 100:
        raise ValueError("A bounded correction plan and operator/correlation are required")
    receipts = evidence.get("responses")
    if not isinstance(receipts, list) or not receipts or any(
        not isinstance(r, Mapping) or not isinstance(r.get("sha256"), str)
        or len(r["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in r["sha256"])
        or not r.get("path") for r in receipts
    ):
        raise ValueError("Immutable provider response references are required")
    if (evidence.get("volume_unit") != "share" or evidence.get("amount_unit") != "CNY"
            or evidence.get("turnover_unit") != "percentage_point"):
        raise ValueError("Explicit verified input units are required")
    locked = []
    seen = set()
    for correction in corrections:
        model = _MODELS.get(correction.table)
        key = correction.table, correction.row_id
        if model is None or key in seen or set(correction.replacement) != set(_FIELDS):
            raise ValueError("Invalid, duplicate or incomplete correction row")
        seen.add(key)
        replacement = correction.replacement
        for field in _FIELDS[:-1]:
            value = replacement[field]
            if value is not None and (isinstance(value, bool) or not isinstance(value, Real)
                    or not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError("Correction contains invalid numeric values")
        if any(replacement[f] is None or float(replacement[f]) <= 0 for f in ("open", "high", "low", "close")):
            raise ValueError("Verified OHLC must be positive")
        if not (replacement["low"] <= min(replacement["open"], replacement["close"])
                <= max(replacement["open"], replacement["close"]) <= replacement["high"]):
            raise ValueError("Verified OHLC ordering is invalid")
        if not isinstance(replacement["source"], str) or not replacement["source"] or len(replacement["source"]) > 32:
            raise ValueError("A bounded source contract tag is required")
        with db.no_autoflush:
            row = db.execute(select(model).where(model.id == correction.row_id)
                .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if row is None or bar_snapshot(row) != dict(correction.expected):
            raise ValueError(f"Correction precondition changed: {correction.table}/{correction.row_id}")
        locked.append((correction, row, bar_snapshot(row)))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for correction, row, _before in locked:
        for field, value in correction.replacement.items():
            setattr(row, field, value)
        # Corrected prices became available now, not on the original trade day.
        row.created_at = now
    db.flush()
    audit_ids = []
    for correction, row, before in locked:
        db.refresh(row)  # Capture authoritative storage precision, including MySQL FLOAT.
        result = write_audit_event(db, "MARKET_BAR_CORRECTION", operator_id=operator_id,
            business_key=f"{correction.table}::{correction.row_id}",
            symbol_id=getattr(row, "symbol_id", None), correlation_id=correlation_id,
            before=before, after=bar_snapshot(row), attributes=dict(evidence),
            note="Bounded provider-verified correction; historical model artifacts remain immutable.")
        if result.action != "MARKET_BAR_CORRECTION":
            raise ValueError("The database audit action contract must be migrated before correction")
        audit_ids.append(result.event_id)
    return audit_ids
