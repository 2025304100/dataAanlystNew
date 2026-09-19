"""Run the G5 decision-level dual-run comparison from exported chain snapshots.

The input is a JSON object with ``portfolio_id`` and ``days``. Each day must
contain ``trade_date``, ``chain_a`` and ``chain_b`` objects matching the
``DailyChainResult`` fields from ``app.services.g5_dual_run_replay``. This
script is deliberately read-only: it does not call an order endpoint or write
to the database.

Example::

    python scripts/g5_dual_run_report.py \
      --input test_output/g5/portfolio-12-20260822.json \
      --output test_output/g5/portfolio-12-summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.g5_dual_run_replay import (  # noqa: E402
    DailyChainResult,
    SecurityDecision,
    run_g5_dual_run_replay,
)


def _decision(raw: dict[str, Any]) -> SecurityDecision:
    return SecurityDecision(
        symbol_id=int(raw["symbol_id"]),
        symbol=raw.get("symbol"),
        action=str(raw.get("action") or "NO_ACTION").upper(),
        target_weight=raw.get("target_weight"),
        score_value=raw.get("score_value"),
        reject_reason_code=raw.get("reject_reason_code") or raw.get("reject_code"),
        reject_reason_human=raw.get("reject_reason_human"),
        reason_category=raw.get("reason_category") or "UNMAPPED",
        checks_json=raw.get("checks_json"),
    )


def _chain(raw: dict[str, Any], trade_date: date, chain: str) -> DailyChainResult:
    decisions = {
        int(item["symbol_id"]): _decision(item)
        for item in (raw.get("decisions") or [])
        if isinstance(item, dict) and item.get("symbol_id") is not None
    }
    return DailyChainResult(
        chain=chain, trade_date=trade_date,
        universe_symbol_ids=[int(value) for value in (raw.get("universe_symbol_ids") or [])],
        decisions=decisions,
        coverage_pct=float(raw.get("coverage_pct") or 0.0),
        blocking_status=str(raw.get("blocking_status") or "READY"),
        blocking_reasons=list(raw.get("blocking_reasons") or []),
        meta=dict(raw.get("meta") or {}),
    )


def load_input(
    path: Path,
) -> tuple[int, dict[date, tuple[DailyChainResult, DailyChainResult]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("G5 input must be a JSON object")
    portfolio_id = int(payload["portfolio_id"])
    days: dict[date, tuple[DailyChainResult, DailyChainResult]] = {}
    for row in payload.get("days") or []:
        if not isinstance(row, dict):
            raise ValueError("each G5 day must be an object")
        trade_date = date.fromisoformat(str(row["trade_date"]))
        if trade_date in days:
            raise ValueError(f"duplicate trade_date: {trade_date}")
        days[trade_date] = (
            _chain(row["chain_a"], trade_date, "A"),
            _chain(row["chain_b"], trade_date, "B"),
        )
    provenance = payload.get("provenance")
    if provenance is None:
        provenance = {}
    if not isinstance(provenance, dict):
        raise ValueError("G5 provenance must be an object")
    if not days:
        expected_dates = provenance.get("expected_trade_dates")
        missing_days = payload.get("missing_days")
        if not isinstance(expected_dates, list) or not expected_dates or not isinstance(missing_days, dict):
            raise ValueError("G5 input contains no days and no complete missing-day manifest")
    return portfolio_id, days, dict(provenance)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--db-url",
        help="Optional SQLite/MySQL URL. When supplied, persist the immutable audit report.",
    )
    args = parser.parse_args()
    portfolio_id, chains, provenance = load_input(args.input)

    def runner(trade_date: date, chain: str) -> DailyChainResult:
        pair = chains.get(trade_date)
        if pair is None:
            raise KeyError(f"missing chain snapshot for {trade_date}")
        return pair[0] if chain == "A" else pair[1]

    if chains:
        summary = run_g5_dual_run_replay(
            portfolio_id=portfolio_id,
            chain_runner=runner,
            explicit_dates=sorted(chains),
        ).as_dict()
    else:
        expected_dates = [date.fromisoformat(str(value)) for value in provenance["expected_trade_dates"]]
        summary = {
            "portfolio_id": portfolio_id,
            "start_date": expected_dates[0].isoformat(),
            "end_date": expected_dates[-1].isoformat(),
            "total_days": len(expected_dates),
            "days_replayed": 0,
            "skipped_days": [value.isoformat() for value in expected_dates],
            "daily_reports": [],
            "avg_action_match_rate": 0.0,
            "avg_universe_jaccard": 0.0,
            "total_p0_unexplained": 0,
            "total_p1_hold_noaction_flip": 0,
            "failing_days_p0": [],
            "failing_days_p1": [],
            "g5_eligible_for_g6": False,
            "summary_notes": ["No complete historical day could be replayed."],
        }
    summary["provenance"] = {
        **provenance,
        "expected_trade_dates": (
            [trade_date.isoformat() for trade_date in sorted(chains)]
            if chains
            else list(provenance.get("expected_trade_dates") or [])
        ),
    }
    if args.db_url:
        from app.services.g5_dual_run_audit import persist_g5_summary

        with Session(create_engine(args.db_url)) as db:
            record = persist_g5_summary(db, summary)
            db.commit()
            summary["audit_report_id"] = record.id
            summary["audit_report_hash"] = record.report_hash
            # 以不可变审计记录中的最终 payload 作为脚本输出和退出码依据。
            # 这样来源证明失败时不会被内存中的 replay eligible=true 绕过。
            try:
                persisted_payload = json.loads(record.report_json)
            except (TypeError, ValueError):
                persisted_payload = {}
            if isinstance(persisted_payload, dict):
                summary.update(persisted_payload)
                summary["audit_report_id"] = record.id
                summary["audit_report_hash"] = record.report_hash
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if summary["g5_eligible_for_g6"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
