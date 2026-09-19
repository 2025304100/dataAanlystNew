"""Persistence and lookup for auditable G5 dual-run summaries."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.g5_dual_run import G5DualRunReport


def _canonical_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _g5_provenance_errors(summary: dict[str, Any]) -> list[str]:
    """Return audit gaps that prevent a G5 export from unlocking G6."""
    errors: list[str] = []
    provenance = summary.get("provenance")
    if not isinstance(provenance, dict):
        return ["缺少 G5 来源证明"]
    capture_mode = provenance.get("capture_mode")
    if capture_mode not in {"real_dry_run_export", "historical_replay"}:
        errors.append("G5 来源必须是 real_dry_run_export 或 historical_replay")
    if capture_mode == "historical_replay":
        if provenance.get("simulated") is not True:
            errors.append("historical_replay 必须标记 simulated=true")
        if provenance.get("real_dry_run") is not False:
            errors.append("historical_replay 必须标记 real_dry_run=false")
    for field in ("exported_at", "trading_calendar", "source_manifest_sha256"):
        if not isinstance(provenance.get(field), str) or not provenance[field].strip():
            errors.append(f"G5 来源证明缺少 {field}")
    exported_at = provenance.get("exported_at")
    if isinstance(exported_at, str) and exported_at.strip():
        try:
            datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("G5 来源证明 exported_at 不是 ISO 时间")

    expected_dates = provenance.get("expected_trade_dates")
    daily_reports = summary.get("daily_reports")
    expected_set: set[str] = set()
    if not isinstance(expected_dates, list) or not expected_dates:
        errors.append("G5 来源证明缺少 expected_trade_dates")
    else:
        expected_set = {str(value) for value in expected_dates}
        if len(expected_set) != len(expected_dates):
            errors.append("G5 expected_trade_dates 存在重复日期")
    if not isinstance(daily_reports, list):
        errors.append("G5 daily_reports 非法")
        daily_reports = []
    actual_dates = {str(item.get("trade_date")) for item in daily_reports if isinstance(item, dict)}
    skipped_set = {str(value) for value in (summary.get("skipped_days") or [])}
    if expected_set:
        if actual_dates & skipped_set:
            errors.append("G5 已回放日与 skipped_days 重复")
        if actual_dates | skipped_set != expected_set:
            errors.append("G5 已回放日、跳过日与预期交易日清单不一致")
        if len(skipped_set) != len(summary.get("skipped_days") or []):
            errors.append("G5 skipped_days 存在重复日期")
    if int(summary.get("days_replayed") or 0) != len(actual_dates):
        errors.append("G5 days_replayed 与每日报告数量不一致")

    for item in daily_reports:
        if not isinstance(item, dict):
            errors.append("G5 每日报告包含非法记录")
            continue
        for name, expected_mode in (("chain_a_meta", "legacy_dry_run"), ("chain_b_meta", "unified_dry_run")):
            meta = item.get(name)
            if not isinstance(meta, dict):
                errors.append(f"G5 {item.get('trade_date')} 缺少 {name}")
                continue
            if meta.get("capture_mode") != expected_mode:
                errors.append(f"G5 {item.get('trade_date')} {name} 不是 {expected_mode}")
            for field in ("source_run_id", "data_cutoff_at"):
                if not isinstance(meta.get(field), str) or not meta[field].strip():
                    errors.append(f"G5 {item.get('trade_date')} {name} 缺少 {field}")
    return errors


def persist_g5_summary(db: Session, summary: dict[str, Any]) -> G5DualRunReport:
    """Persist one immutable G5 summary, fail-closed for G6 eligibility."""
    summary = json.loads(json.dumps(summary, ensure_ascii=False, default=str))
    provenance_errors = _g5_provenance_errors(summary)
    if provenance_errors:
        summary["g5_eligible_for_g6"] = False
        summary["g5_validation_errors"] = provenance_errors
    rendered = _canonical_json(summary)
    report_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    existing = db.execute(
        select(G5DualRunReport).where(G5DualRunReport.report_hash == report_hash)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    skipped_days = list(summary.get("skipped_days") or [])
    record = G5DualRunReport(
        portfolio_id=int(summary["portfolio_id"]),
        start_date=date.fromisoformat(str(summary["start_date"])),
        end_date=date.fromisoformat(str(summary["end_date"])),
        total_days=int(summary.get("total_days") or 0),
        days_replayed=int(summary.get("days_replayed") or 0),
        skipped_day_count=len(skipped_days),
        p0_unexplained_count=int(summary.get("total_p0_unexplained") or 0),
        p1_hold_noaction_flip_count=int(summary.get("total_p1_hold_noaction_flip") or 0),
        eligible_for_g6=1 if bool(summary.get("g5_eligible_for_g6")) else 0,
        report_hash=report_hash,
        report_json=rendered,
    )
    db.add(record)
    db.flush()
    return record


def latest_g5_summary(db: Session, *, portfolio_id: int) -> dict[str, Any] | None:
    """Return the newest report payload for a portfolio, or ``None``."""
    record = db.execute(
        select(G5DualRunReport)
        .where(G5DualRunReport.portfolio_id == int(portfolio_id))
        .order_by(G5DualRunReport.created_at.desc(), G5DualRunReport.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if record is None:
        return None
    try:
        payload = json.loads(record.report_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["audit_report_id"] = record.id
    payload["audit_report_hash"] = record.report_hash
    return payload
