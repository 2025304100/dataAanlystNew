"""Controlled local import for user-supplied tail-session minute bars."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.services.factors.tail_proxy import (
    calculate_tail_proxy, minute_session_mask, normalize_minute_timestamps,
)


_REQUIRED = {"symbol", "timestamp", "open", "high", "low", "close", "volume", "amount"}
_SOURCE = "manual_import:tail_minutes:v1"


def _symbol_code(value: object) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value))
    return match.group(1) if match else str(value).strip().upper()


def import_tail_minute_csv(db: Session, *, csv_text: str, filename: str) -> dict[str, Any]:
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        normalized_headers = [str(item or "").lstrip("\ufeff").strip().lower() for item in (reader.fieldnames or [])]
        headers = set(normalized_headers)
        if len(headers) != len(normalized_headers):
            raise ValueError("CSV has duplicate column names")
        if not _REQUIRED.issubset(headers):
            missing = ", ".join(sorted(_REQUIRED - headers))
            raise ValueError(f"CSV missing required columns: {missing}")
        rows = [{str(key).lstrip("\ufeff").strip().lower(): value for key, value in row.items()} for row in reader]
    except csv.Error as exc:
        raise ValueError(f"CSV parse failed: {exc}") from exc
    if not rows:
        raise ValueError("CSV has no data rows")
    if len(rows) > 1_500_000:
        raise ValueError("CSV exceeds 1,500,000 minute rows per import")
    frame = pd.DataFrame(rows)
    frame["symbol"] = frame["symbol"].map(_symbol_code)
    frame["timestamp"] = normalize_minute_timestamps(frame["timestamp"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
    valid = (
        frame["symbol"].str.fullmatch(r"\d{6}", na=False)
        & frame["timestamp"].notna()
        & (frame["timestamp"].dt.date <= datetime.now(ZoneInfo("Asia/Shanghai")).date())
        & np.isfinite(frame[numeric_columns]).all(axis=1)
        & (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
        & (frame[["volume", "amount"]] >= 0).all(axis=1)
        & minute_session_mask(frame["timestamp"])
    )
    invalid_rows = int((~valid).sum())
    frame["_valid_minute_bar"] = valid
    # Keep malformed numerical rows in their identifiable session so that a
    # partially valid CSV does not quietly convert a bad session into success.
    frame = frame[frame["timestamp"].notna() & frame["symbol"].str.fullmatch(r"\d{6}", na=False)]
    if frame.empty:
        raise ValueError("CSV contains no identifiable minute sessions")
    frame["trade_date"] = frame["timestamp"].dt.date
    written = 0
    rejected: list[dict[str, str]] = []
    rejected_count = 0
    dates: list[date] = []
    for (symbol, trade_date), group in frame.groupby(["symbol", "trade_date"], sort=True):
        valid_group = bool(group["_valid_minute_bar"].all())
        metrics = calculate_tail_proxy(group, trade_date=trade_date) if valid_group else None
        if metrics is None:
            rejected_count += 1
            if len(rejected) < 100:
                rejected.append({"symbol": str(symbol), "trade_date": str(trade_date),
                                 "reason": "incomplete_session_or_invalid_amount" if valid_group else "invalid_minute_bars"})
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
    mirror_status: dict[str, Any] = {"status": "not_needed", "records": 0}
    quality_status: dict[str, Any] = {"status": "not_needed"}
    warnings: list[str] = []
    if dates:
        from app.services.factors.data_sync import mirror_factor_inputs
        from app.services.data_quality import capture_field_quality_snapshots

        try:
            mirror = mirror_factor_inputs(
                db, start_date=min(dates), end_date=max(dates),
                include_valuations=False, include_financial_reports=False,
                include_fund_flows=False, include_sentiment=False,
                include_tail_proxy=True, include_etf_indicators=False, include_macro=False,
            )
            mirror_status = {"status": "done", "records": mirror.rows_written, "batch_id": mirror.batch_id}
        except Exception as exc:
            mirror_status = {"status": "warning", "records": 0, "error": str(exc)}
            quality_status = {"status": "skipped", "reason": "warehouse_mirror_failed"}
            warnings.append("CSV 已保存到本地数据库，但因子仓库镜像失败；请重新导入重试。")
        else:
            try:
                quality = capture_field_quality_snapshots(db, trigger="tail_minute_manual_import")
                quality_status = {**quality, "status": "done"}
            except Exception as exc:
                db.rollback()
                quality_status = {"status": "warning", "error": str(exc)}
                warnings.append("CSV 已保存并完成因子仓库镜像，但质量诊断刷新失败；请刷新诊断或重新导入。")
    return {
        "source": _SOURCE, "written_sessions": written,
        "rejected_sessions": rejected_count, "rejected": rejected,
        "invalid_rows": invalid_rows,
        "warehouse_mirror": mirror_status, "quality_snapshot": quality_status,
        "warnings": warnings,
        "calendar_validation": "cn_clock_weekday_only",
        "supplier_history_verified": False,
        "formula_evaluation": "blocked_pending_reliable_marketwide_history",
    }
