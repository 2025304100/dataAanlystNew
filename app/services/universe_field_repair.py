"""Evidence-bound compensation of existing empty fields; never rewrites history rows."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
from sqlalchemy import select, text

from app.models.async_task import AsyncTaskRecord
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.history_source_guard import VALUE_FIELDS
from app.services.market_history_contract import production_history_source_tag


REPAIR_FIELDS = frozenset({"volume", "amount", "turnover_rate"})
INSTRUMENT_IDENTITY_FIELDS = ("id", "symbol", "market", "asset_type", "region")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()


def _availability_value(value) -> dict:
    if value is None:
        return {"type": "null", "value": None}
    if not isinstance(value, datetime):
        raise ValueError("field_repair_availability_type_invalid")
    return {"type": "datetime", "value": value.isoformat(timespec="microseconds")}


def _require_transactional_tables(db) -> None:
    if db.get_bind().dialect.name != "mysql":
        return
    rows = dict(db.execute(text("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
        "('universe_symbols','universe_daily_bars','async_tasks')")).tuples().all())
    if any(str(rows.get(name, "")).lower() != "innodb"
           for name in ("universe_symbols", "universe_daily_bars", "async_tasks")):
        raise ValueError("field_repair_requires_innodb: data and audit must roll back together")


def repair_universe_fields(db, *, symbol_ids: list[int], start_date: date, end_date: date,
                          fields: list[str], apply: bool = False,
                          expected_preview_sha256: str | None = None) -> dict:
    """Preview by default; apply exactly the reviewed content in one transaction.

    ``symbol_ids`` are universe_symbols IDs, not symbols IDs. Only existing
    rows in the explicit range and NULL requested fields are eligible. Missing
    days require a separate calendar-aware history import. All unresolved cells
    remain listed, and successful cells never make a partial task appear done.
    Use a dedicated clean Session: fetching releases read transactions before
    network I/O; applying re-locks and checks every old column against preview.
    """
    if (not symbol_ids or len(symbol_ids) > 100 or len(set(symbol_ids)) != len(symbol_ids)
            or any(type(value) is not int or value <= 0 for value in symbol_ids)):
        raise ValueError("symbol_ids must contain 1..100 distinct positive universe IDs")
    if (not fields or not set(fields) <= REPAIR_FIELDS or len(fields) != len(set(fields))
            or end_date < start_date or (end_date - start_date).days > 365):
        raise ValueError("explicit valid dates (at most 366 days) and repair fields are required")
    if db.new or db.dirty or db.deleted:
        raise ValueError("field_repair_requires_clean_session")
    if apply and not expected_preview_sha256:
        raise ValueError("expected_preview_sha256 is required to apply")
    from app.services import universe_sync

    snapshots, changes, failures, instrument_snapshots = [], [], [], []
    identity_columns = [getattr(UniverseSymbol, field) for field in INSTRUMENT_IDENTITY_FIELDS]
    for symbol_id in sorted(symbol_ids):
        identity = db.execute(select(*identity_columns).where(UniverseSymbol.id == symbol_id)).mappings().one_or_none()
        if identity is None:
            failures.append({"symbol_id": symbol_id, "reason": "symbol_not_found"})
            db.commit()
            continue
        instrument_snapshot = dict(identity)
        instrument_snapshots.append(instrument_snapshot)
        # Fetch with this immutable request identity, never with an expired ORM
        # object whose market/code could reload after the transaction releases.
        instrument = SimpleNamespace(**instrument_snapshot)
        rows = [dict(row) for row in db.execute(select(UniverseDailyBar.__table__).where(
            UniverseDailyBar.universe_symbol_id == symbol_id,
            UniverseDailyBar.trade_date.between(start_date, end_date),
        ).order_by(UniverseDailyBar.id)).mappings()]
        snapshots.extend(rows)
        gaps = [(row, field) for row in rows for field in fields if row[field] is None]
        if not rows:
            failures.append({"symbol_id": symbol_id, "reason": "no_existing_rows_in_range"})
        db.commit()
        if not gaps:
            continue
        try:
            frame = universe_sync._fetch_universe_history(instrument, start_date, end_date)
            source = production_history_source_tag(frame)
            by_date = {}
            duplicate_dates = set()
            for raw in frame.to_dict(orient="records"):
                day = universe_sync._normalize_trade_date(raw["trade_date"])
                if day in by_date:
                    duplicate_dates.add(day)
                by_date[day] = raw
            for row, field in gaps:
                incoming = by_date.get(row["trade_date"])
                reason = None
                if row["trade_date"] in duplicate_dates:
                    reason = "ambiguous_duplicate_date"
                elif incoming is None:
                    reason = "provider_day_missing"
                elif source != row["source"] or source in {"", "akshare", "akshare:qfq"}:
                    reason = "source_unproven_or_conflicting"
                elif any(incoming.get(key) is None or pd.isna(incoming[key]) or
                         not math.isfinite(float(incoming[key])) or float(incoming[key]) != row[key]
                         for key in ("open", "high", "low", "close")):
                    reason = "ohlc_context_mismatch"
                elif any(row[key] is not None and incoming.get(key) is not None and
                         not pd.isna(incoming[key]) and float(incoming[key]) != row[key]
                         for key in VALUE_FIELDS):
                    reason = "context_value_mismatch"
                value = incoming.get(field) if incoming is not None else None
                if reason is None and (value is None or pd.isna(value) or
                                        not math.isfinite(float(value)) or float(value) < 0):
                    reason = "provider_field_missing_or_invalid"
                identity = {"symbol_id": symbol_id, "row_id": row["id"],
                            "trade_date": row["trade_date"].isoformat(), "field": field}
                if reason:
                    failures.append({**identity, "reason": reason})
                else:
                    changes.append({**identity, "before": None, "after": float(value), "source": source})
        except Exception as exc:
            failures.append({"symbol_id": symbol_id, "reason": "provider_error", "error": str(exc)})
    old_sha = _digest(snapshots)
    identity_sha = _digest(instrument_snapshots)
    changed_row_ids = {change["row_id"] for change in changes}
    availability_before = [{"row_id": row["id"], "created_at": _availability_value(row["created_at"])}
                           for row in snapshots if row["id"] in changed_row_ids]
    outcome = "partial_success" if failures and changes else "failed" if failures else "success"
    result = {"symbol_ids": symbol_ids, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
              "fields": fields, "old_rows_sha256": old_sha, "row_count": len(snapshots),
              "instrument_snapshots": instrument_snapshots, "old_instruments_sha256": identity_sha,
              "availability_before": availability_before,
              "filled_fields": len(changes), "changes": changes, "failures": failures, "outcome": outcome}
    result["preview_sha256"] = _digest(result)
    result["applied"] = False
    if not apply:
        return result
    if result["preview_sha256"] != expected_preview_sha256:
        raise ValueError("preview_changed: re-preview source response and existing rows before apply")
    try:
        _require_transactional_tables(db)
        locked_identities = [dict(row) for row in db.execute(select(*identity_columns)
            .where(UniverseSymbol.id.in_(symbol_ids)).order_by(UniverseSymbol.id).with_for_update()).mappings()]
        if _digest(locked_identities) != identity_sha:
            raise ValueError("preview_changed: concurrent instrument identity update; no fields applied")
        current_rows = [dict(row) for row in db.execute(select(UniverseDailyBar.__table__).where(
            UniverseDailyBar.universe_symbol_id.in_(symbol_ids),
            UniverseDailyBar.trade_date.between(start_date, end_date),
        ).order_by(UniverseDailyBar.universe_symbol_id, UniverseDailyBar.id).with_for_update()).mappings()]
        if _digest(current_rows) != old_sha:
            raise ValueError("preview_changed: concurrent history update; no fields applied")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Legacy MySQL DATETIME(0) must not round observation availability
        # backwards. A whole-second ceiling adds less than one second and
        # existing future availability must never be moved earlier either.
        observed_ceiling = now.replace(microsecond=0) + timedelta(seconds=1 if now.microsecond else 0)
        for change in changes:
            row = db.get(UniverseDailyBar, change["row_id"], populate_existing=True)
            setattr(row, change["field"], change["after"])
            row.created_at = max(row.created_at, observed_ceiling) if row.created_at is not None else observed_ceiling
        # Read the actual persisted precision before adding the audit. MySQL
        # DATETIME may round fractional seconds, so the assigned Python value
        # alone is not an exact post-image. Audit failure still rolls back all.
        db.flush()
        after_availability = dict(db.execute(select(UniverseDailyBar.id, UniverseDailyBar.created_at)
            .where(UniverseDailyBar.id.in_(changed_row_ids)).with_for_update()).all()) if changed_row_ids else {}
        result["availability_changes"] = [{"row_id": item["row_id"], "before": item["created_at"],
            "after": _availability_value(after_availability[item["row_id"]])} for item in availability_before]
        result.update(applied=True, audit_task_id=uuid4().hex, observed_at=now.isoformat())
        failed_ids = {item["symbol_id"] for item in failures}
        task = AsyncTaskRecord(id=result["audit_task_id"], task_type="universe_field_repair",
            status="failed" if failures else "done", stage="failed" if failures else "done",
            percent=100, message=f"{outcome}: filled {len(changes)} fields; {len(failures)} unresolved",
            total=len(symbol_ids), processed=len(symbol_ids), ok_count=len(symbol_ids) - len(failed_ids),
            failed_count=len(failed_ids), started_at=now, finished_at=now, is_terminal_locked=1,
            payload_json=json.dumps({"symbol_ids": symbol_ids, "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(), "fields": fields, "expected_preview_sha256": expected_preview_sha256}),
            result_json=json.dumps(result, ensure_ascii=False, default=str),
            errors_json=json.dumps(failures, ensure_ascii=False, default=str))
        db.add(task)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
