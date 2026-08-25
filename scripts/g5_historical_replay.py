"""Build a read-only G5 historical replay export from business data.

This adapter intentionally fails closed. It does not create DecisionRun rows,
orders, fills, positions, or cash entries in the source database. A day is
replayable only when both legacy scan input and unified evidence input exist,
PIT-safe scores and prior-close bars exist, and a prior portfolio state can be
proven. Missing days remain visible in the export and prevent G6 eligibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.g5_dual_run import G5DualRunReport  # noqa: E402
from app.services.g5_dual_run_audit import persist_g5_summary  # noqa: E402
from app.services.g5_dual_run_replay import DailyChainResult, SecurityDecision, run_g5_dual_run_replay  # noqa: E402


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if parsed is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _cutoff(trade_date: date) -> datetime:
    # 15:00 Asia/Shanghai expressed as UTC-naive, matching the DB contract.
    return datetime.combine(trade_date, time(7, 0))


def _manifest_hash(items: list[dict[str, Any]]) -> str:
    rendered = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _decisions_from_evidence(rows: list[dict[str, Any]]) -> tuple[list[int], dict[int, SecurityDecision]]:
    universe: list[int] = []
    decisions: dict[int, SecurityDecision] = {}
    for row in rows:
        sid = int(row["symbol_id"])
        universe.append(sid)
        action = str(row.get("action") or "NO_ACTION").upper()
        decisions[sid] = SecurityDecision(
            symbol_id=sid,
            action=action,
            target_weight=row.get("target_position_pct"),
            score_value=row.get("score_value"),
            reject_reason_code=row.get("rejection_reason") or row.get("blocking_reason"),
            reject_reason_human=row.get("rejection_detail") or row.get("blocking_reason"),
            reason_category="SCORE_DATA_GAP" if action == "DATA_BLOCKED" else "EXPECTED_STRUCTURAL_DIFF",
            checks_json={"pit_safe_flag": row.get("pit_safe_flag"), "target_quantity": row.get("target_quantity"), "target_qty_delta": row.get("target_qty_delta")},
        )
    return sorted(set(universe)), decisions


def _scan_legacy(db: Any, portfolio_id: int, trade_date: date, cutoff: datetime) -> tuple[list[int], dict[int, SecurityDecision], list[dict[str, Any]]]:
    rows = db.execute(text("""
        SELECT sr.scan_run_id, sr.symbol_id, sr.action, sr.recommended_position_pct,
               sr.quality_score, sr.reason_tags, r.created_at
        FROM scan_results sr JOIN scan_runs r ON r.id = sr.scan_run_id
        WHERE r.portfolio_id=:pid AND DATE(r.created_at)=:td AND r.created_at<=:cutoff
        ORDER BY sr.symbol_id, sr.id
    """), {"pid": portfolio_id, "td": trade_date, "cutoff": cutoff}).mappings().all()
    decisions: dict[int, SecurityDecision] = {}
    manifest: list[dict[str, Any]] = []
    for row in rows:
        sid = int(row["symbol_id"])
        if sid in decisions:
            continue
        action = str(row.get("action") or "NO_ACTION").upper()
        decisions[sid] = SecurityDecision(symbol_id=sid, action=action, target_weight=row.get("recommended_position_pct"), score_value=row.get("quality_score"), reject_reason_code=None, reason_category="EXPECTED_STRUCTURAL_DIFF", checks_json={"legacy_scan_run_id": row["scan_run_id"]})
        manifest.append({"table": "scan_results", "scan_run_id": row["scan_run_id"], "symbol_id": sid, "created_at": str(row["created_at"])})
    return sorted(decisions), decisions, manifest


def _load_day(db: Any, portfolio_id: int, snapshot_id: str, trade_date: date) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
    cutoff = _cutoff(trade_date)
    problems: list[str] = []
    manifest: list[dict[str, Any]] = []
    snap = db.execute(text("SELECT id, member_snapshot_json, decision_clock_json FROM strategy_execution_snapshots WHERE id=:sid AND portfolio_id=:pid"), {"sid": snapshot_id, "pid": portfolio_id}).mappings().first()
    if snap is None:
        return None, ["strategy_snapshot_missing"], manifest
    members = _json(snap["member_snapshot_json"], [])
    member_ids = []
    for item in members if isinstance(members, list) else []:
        value = item.get("symbol_id") if isinstance(item, dict) else item
        if value is not None:
            member_ids.append(int(value))
    member_ids = sorted(set(member_ids))
    if not member_ids:
        problems.append("portfolio_member_snapshot_empty")
    manifest.append({"table": "strategy_execution_snapshots", "id": snapshot_id, "member_ids": member_ids, "decision_clock_json": snap["decision_clock_json"]})

    # Use the last completed bar strictly before the decision date. T-day close
    # is not available at a 15:00 decision cutoff.
    bars: dict[int, dict[str, Any]] = {}
    for sid in member_ids:
        bar = db.execute(text("SELECT id, trade_date, open, close, volume FROM daily_bars WHERE symbol_id=:sid AND trade_date<:td ORDER BY trade_date DESC LIMIT 1"), {"sid": sid, "td": trade_date}).mappings().first()
        if bar is None:
            problems.append(f"{trade_date.isoformat()}:missing_prior_close_bar:symbol={sid}")
        else:
            bars[sid] = dict(bar)
            manifest.append({"table": "daily_bars", "id": bar["id"], "symbol_id": sid, "trade_date": str(bar["trade_date"]), "close": bar["close"]})

    scores: dict[int, dict[str, Any]] = {}
    for sid in member_ids:
        score = db.execute(text("""
            SELECT id, trade_date, quality_score, priority_score, published_at, pit_safety
            FROM scores WHERE symbol_id=:sid AND trade_date<=:td AND published_at<=:cutoff
            ORDER BY trade_date DESC, published_at DESC, id DESC LIMIT 1
        """), {"sid": sid, "td": trade_date, "cutoff": cutoff}).mappings().first()
        if score is None or str(score.get("pit_safety") or "") != "PIT_VERIFIED":
            problems.append(f"{trade_date.isoformat()}:missing_pit_safe_score:symbol={sid}")
        else:
            scores[sid] = dict(score)
            manifest.append({"table": "scores", "id": score["id"], "symbol_id": sid, "trade_date": str(score["trade_date"]), "published_at": str(score["published_at"]), "pit_safety": score["pit_safety"]})

    legacy_u, legacy_d, legacy_manifest = _scan_legacy(db, portfolio_id, trade_date, cutoff)
    if not legacy_u:
        problems.append(f"{trade_date.isoformat()}:legacy_chain_input_missing")
    manifest.extend(legacy_manifest)
    evidence_rows = db.execute(text("""
        SELECT symbol_id, action, target_position_pct, target_quantity, target_qty_delta,
               rejection_reason, rejection_detail, blocking_reason, score_value,
               pit_safe_flag, decision_run_id, data_cutoff_at
        FROM decision_evidence WHERE portfolio_id=:pid AND trade_date=:td
        ORDER BY symbol_id
    """), {"pid": portfolio_id, "td": trade_date}).mappings().all()
    if not evidence_rows:
        problems.append(f"{trade_date.isoformat()}:unified_chain_input_missing")
    unified_u, unified_d = _decisions_from_evidence([dict(x) for x in evidence_rows])
    manifest.extend({"table": "decision_evidence", "decision_run_id": row["decision_run_id"], "symbol_id": row["symbol_id"], "data_cutoff_at": str(row["data_cutoff_at"])} for row in evidence_rows)

    # The state proof is intentionally separate from current positions. A
    # current snapshot cannot be used to reconstruct a historical cash state.
    state = db.execute(text("SELECT id, snapshot_date, cash_balance, market_value, total_equity FROM portfolio_equity_snapshots WHERE portfolio_id=:pid AND snapshot_date<=:td ORDER BY snapshot_date DESC LIMIT 1"), {"pid": portfolio_id, "td": trade_date}).mappings().first()
    if state is None:
        problems.append(f"{trade_date.isoformat()}:historical_cash_position_state_missing")
    else:
        manifest.append({"table": "portfolio_equity_snapshots", "id": state["id"], "snapshot_date": str(state["snapshot_date"]), "cash_balance": state["cash_balance"], "market_value": state["market_value"], "total_equity": state["total_equity"]})

    if problems:
        return None, problems, manifest
    cutoff_iso = cutoff.isoformat() + "Z"
    return {"trade_date": trade_date.isoformat(), "chain_a": {"universe_symbol_ids": legacy_u, "decisions": [d.to_audit_row() for d in legacy_d.values()], "blocking_status": "READY", "meta": {"capture_mode": "legacy_dry_run", "source_run_id": f"legacy-scan-{trade_date.isoformat()}", "data_cutoff_at": cutoff_iso, "source_manifest_sha256": _manifest_hash(legacy_manifest)}}, "chain_b": {"universe_symbol_ids": unified_u, "decisions": [d.to_audit_row() for d in unified_d.values()], "blocking_status": "READY", "meta": {"capture_mode": "unified_dry_run", "source_run_id": f"unified-evidence-{trade_date.isoformat()}", "data_cutoff_at": cutoff_iso, "source_manifest_sha256": _manifest_hash(manifest)}}, "state_continuity": {"verified": True, "source": "portfolio_equity_snapshots"}}, [], manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--portfolio-id", required=True, type=int)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit-db", required=True, type=Path)
    args = parser.parse_args()
    if args.end_date < args.start_date:
        raise SystemExit("end-date must be >= start-date")
    engine = create_engine(args.db_url, connect_args={"connect_timeout": 3} if args.db_url.startswith(("mysql://", "mysql+")) else {})
    if not {"portfolios", "strategy_execution_snapshots", "portfolio_members", "daily_bars", "scores"} <= set(inspect(engine).get_table_names()):
        raise SystemExit("business schema is incomplete")
    with engine.connect() as db:
        trade_dates = [row[0] for row in db.execute(text("SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date BETWEEN :start AND :end ORDER BY trade_date"), {"start": args.start_date, "end": args.end_date}).all()]
        days: list[dict[str, Any]] = []
        missing: dict[str, list[str]] = {}
        manifest: list[dict[str, Any]] = []
        for td in trade_dates:
            day, problems, day_manifest = _load_day(db, args.portfolio_id, args.snapshot_id, td)
            manifest.extend(day_manifest)
            if day is not None:
                days.append(day)
            else:
                missing[td.isoformat()] = problems
    expected = [td.isoformat() for td in trade_dates]
    replay_gate = len(days) >= 10 and not missing and len(expected) >= 10
    provenance = {"capture_mode": "historical_replay", "simulated": True, "real_dry_run": False, "exported_at": datetime.now(timezone.utc).isoformat(), "trading_calendar": "business_database_daily_bars", "source_manifest_sha256": _manifest_hash(manifest), "expected_trade_dates": expected, "replay_gate_passed": replay_gate, "state_continuity_verified": replay_gate, "pit_safe": replay_gate, "source_database": args.db_url, "portfolio_id": args.portfolio_id, "snapshot_id": args.snapshot_id}
    # Only complete days are replay snapshots. Missing days stay in the
    # manifest below; creating DATA_BLOCKED chain rows here would make the
    # report tool count a blocked day as a replayed day and could look like a
    # synthetic dual run.
    export_days = list(days)
    payload = {"portfolio_id": args.portfolio_id, "snapshot_id": args.snapshot_id, "start_date": args.start_date.isoformat(), "end_date": args.end_date.isoformat(), "days": export_days, "missing_days": missing, "provenance": provenance}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    # Create only the separate audit database. The business engine is never
    # initialized here, so no source migration or schema mutation can occur.
    args.audit_db.parent.mkdir(parents=True, exist_ok=True)
    audit_engine = create_engine("sqlite:///" + args.audit_db.resolve().as_posix())
    G5DualRunReport.__table__.create(audit_engine, checkfirst=True)
    if days:
        chains = {(date.fromisoformat(row["trade_date"]), chain): row[f"chain_{chain}"] for row in days for chain in ("a", "b")}
        def runner(td: date, chain: str) -> DailyChainResult:
            raw = chains.get((td, chain))
            if raw is None:
                raise KeyError(f"missing chain snapshot for {td}/{chain}")
            return DailyChainResult(chain=chain, trade_date=td, universe_symbol_ids=list(raw.get("universe_symbol_ids") or []), decisions={int(x["symbol_id"]): SecurityDecision(symbol_id=int(x["symbol_id"]), action=str(x.get("action") or "NO_ACTION"), target_weight=x.get("target_weight"), score_value=x.get("score_value"), reject_reason_code=x.get("reject_reason_code"), reject_reason_human=x.get("reject_reason_human"), reason_category=x.get("reason_category") or "UNMAPPED", checks_json=x.get("checks_json")) for x in raw.get("decisions") or []}, blocking_status=raw.get("blocking_status") or "READY", meta=raw.get("meta") or {})
        summary = run_g5_dual_run_replay(portfolio_id=args.portfolio_id, chain_runner=runner, explicit_dates=[date.fromisoformat(x) for x in expected])
        summary_payload = summary.as_dict()
    else:
        summary_payload = {"portfolio_id": args.portfolio_id, "start_date": args.start_date.isoformat(), "end_date": args.end_date.isoformat(), "total_days": len(expected), "days_replayed": 0, "skipped_days": expected, "total_p0_unexplained": 0, "total_p1_hold_noaction_flip": 0, "g5_eligible_for_g6": False, "daily_reports": [], "summary_notes": ["No complete historical day could be replayed."]}
    summary_payload["provenance"] = provenance
    with Session(audit_engine) as audit_session:
        record = persist_g5_summary(audit_session, summary_payload)
        audit_session.commit()
        print(json.dumps({"audit_report_id": record.id, "audit_report_hash": record.report_hash, "g5_eligible_for_g6": bool(summary_payload.get("g5_eligible_for_g6")), "missing_days": len(missing), "days_replayed": summary_payload.get("days_replayed")}, ensure_ascii=False))
    return 0 if replay_gate and bool(summary_payload.get("g5_eligible_for_g6")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
