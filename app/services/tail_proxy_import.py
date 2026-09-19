"""Controlled local import for user-supplied tail-session minute bars."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.services.factors.tail_proxy import calculate_tail_proxy


_REQUIRED = {"symbol", "timestamp", "open", "high", "low", "close", "volume", "amount"}
_SOURCE = "manual_import:tail_minutes:v1"


def _symbol_code(value: object) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def import_tail_minute_csv(db: Session, *, csv_text: str, filename: str) -> dict[str, Any]:
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        headers = {str(item or "").strip().lower() for item in (reader.fieldnames or [])}
        if not _REQUIRED.issubset(headers):
            missing = ", ".join(sorted(_REQUIRED - headers))
            raise ValueError(f"CSV missing required columns: {missing}")
        rows = [{str(key).strip().lower(): value for key, value in row.items()} for row in reader]
    except csv.Error as exc:
        raise ValueError(f"CSV parse failed: {exc}") from exc
    if not rows:
        raise ValueError("CSV has no data rows")
    if len(rows) > 1_500_000:
        raise ValueError("CSV exceeds 1,500,000 minute rows per import")
    frame = pd.DataFrame(rows)
    frame["symbol"] = frame["symbol"].map(_symbol_code)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["symbol", "timestamp", "high", "low", "close", "amount"])
    if frame.empty:
        raise ValueError("CSV contains no valid minute bars")
    frame["trade_date"] = frame["timestamp"].dt.date
    written = 0
    rejected: list[dict[str, str]] = []
    dates: list[date] = []
    for (symbol, trade_date), group in frame.groupby(["symbol", "trade_date"], sort=True):
        metrics = calculate_tail_proxy(group, trade_date=trade_date)
        if metrics is None:
            if len(rejected) < 100:
                rejected.append({"symbol": str(symbol), "trade_date": str(trade_date), "reason": "incomplete_session_or_invalid_amount"})
            continue
        existing = db.execute(select(TailAccumulationSnapshot).where(
            TailAccumulationSnapshot.symbol == str(symbol),
            TailAccumulationSnapshot.trade_date == trade_date,
            TailAccumulationSnapshot.source == _SOURCE,
        )).scalars().first()
        if existing is None:
            existing = TailAccumulationSnapshot(symbol=str(symbol), trade_date=trade_date, source=_SOURCE)
            db.add(existing)
        for field, value in asdict(metrics).items():
            if field != "trade_date":
                setattr(existing, field, value)
        existing.raw_json = json.dumps({"filename": filename, "rows": len(group), "source": "user_csv"}, ensure_ascii=False)
        dates.append(trade_date)
        written += 1
    db.commit()
    if dates:
        from app.services.factors.data_sync import mirror_factor_inputs
        from app.services.data_quality import capture_field_quality_snapshots

        mirror_factor_inputs(
            db, start_date=min(dates), end_date=max(dates),
            include_valuations=False, include_financial_reports=False,
            include_fund_flows=False, include_sentiment=False,
            include_tail_proxy=True, include_etf_indicators=False, include_macro=False,
        )
        capture_field_quality_snapshots(db, trigger="tail_minute_manual_import")
    return {
        "source": _SOURCE, "written_sessions": written,
        "rejected_sessions": len(rejected), "rejected": rejected,
        "formula_evaluation": "blocked_pending_reliable_marketwide_history",
    }
