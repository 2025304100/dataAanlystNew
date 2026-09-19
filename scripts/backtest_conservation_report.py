"""Build a read-only daily five-vector conservation report for backtests.

The report deliberately reads only the immutable decision/evidence and fill
ledgers. It never changes a run status or creates synthetic evidence.

Usage::

    python scripts/backtest_conservation_report.py --db-url sqlite:///quant_workbench.db --run-id 12

For a snapshot-backed run, output contains one row per decision date with the
R1-R4 result from ``evaluate_five_vector_conservation``. Legacy runs are
reported explicitly as ``legacy/non_reproducible`` and are not silently
treated as a passed modern chain.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.backtest import (  # noqa: E402
    BacktestExecutionFill,
    BacktestRun,
    BacktestValuationSnapshot,
)
from app.models.decision_engine import DecisionEvidence, DecisionRun  # noqa: E402
from app.services.reconciliation_conservation import (  # noqa: E402
    FiveVectorConservationReport,
    evaluate_five_vector_conservation,
)


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _cash_delta(fill: BacktestExecutionFill) -> float:
    """Cash change caused by one fill; costs are always an outflow."""
    gross = float(fill.quantity or 0.0) * float(fill.executed_price or 0.0)
    cost = float(fill.cost or 0.0)
    return -gross - cost if str(fill.side).upper() == "BUY" else gross - cost


def _conservation_price(fill: BacktestExecutionFill) -> float:
    """Encode fees into the pure seam's price-only cash contract."""
    quantity = float(fill.quantity or 0.0)
    price = float(fill.executed_price or 0.0)
    if quantity <= 0:
        return price
    fee_per_share = float(fill.cost or 0.0) / quantity
    return price + fee_per_share if str(fill.side).upper() == "BUY" else price - fee_per_share


def _is_actionable_side(side: Any) -> bool:
    return str(side or "").upper() in {"BUY", "SELL"}


def _serialize_report(report: FiveVectorConservationReport) -> dict[str, Any]:
    return {
        "overall": report.overall,
        "nav_start": report.nav_start,
        "nav_end": report.nav_end,
        "diffs": [
            {
                "source_vector": diff.source_vector,
                "kind": diff.kind,
                "symbol_id": diff.symbol_id,
                "expected": diff.expected,
                "actual": diff.actual,
                "detail": diff.detail,
            }
            for diff in report.diffs
        ],
    }


def build_report(db: Session, run: BacktestRun) -> dict[str, Any]:
    status = getattr(run, "reproducibility_status", None)
    if not status:
        status = "reproducible" if run.strategy_snapshot_id else "legacy/non_reproducible"

    if not run.strategy_snapshot_id:
        return {
            "run_id": run.id,
            "run_name": run.run_name,
            "reproducibility_status": "legacy/non_reproducible",
            "reason": getattr(run, "reproducibility_reason", None)
                or "历史运行未绑定策略执行快照和统一决策链",
            "daily": [],
        }

    decision_ids = [str(value) for value in _json_list(run.decision_run_ids_json) if value]
    decision_runs = db.execute(
        select(DecisionRun)
        .where(DecisionRun.id.in_(decision_ids))
        .order_by(DecisionRun.trade_date)
    ).scalars().all() if decision_ids else []
    evidence_rows = db.execute(
        select(DecisionEvidence)
        .where(DecisionEvidence.decision_run_id.in_(decision_ids))
    ).scalars().all() if decision_ids else []
    evidence_by_date: dict[date, list[DecisionEvidence]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_date[row.trade_date].append(row)

    fills = db.execute(
        select(BacktestExecutionFill)
        .where(BacktestExecutionFill.run_id == run.id)
        .order_by(BacktestExecutionFill.execution_date, BacktestExecutionFill.id)
    ).scalars().all()
    fills_by_date: dict[date, list[BacktestExecutionFill]] = defaultdict(list)
    fills_by_evidence: dict[str, list[BacktestExecutionFill]] = defaultdict(list)
    for fill in fills:
        fills_by_date[fill.execution_date].append(fill)
        if fill.decision_evidence_id:
            fills_by_evidence[str(fill.decision_evidence_id)].append(fill)

    valuations = db.execute(
        select(BacktestValuationSnapshot)
        .where(BacktestValuationSnapshot.run_id == run.id)
    ).scalars().all()
    prices_by_date: dict[date, dict[int, float]] = defaultdict(dict)
    for row in valuations:
        if row.mark_price is not None:
            prices_by_date[row.trade_date][int(row.symbol_id)] = float(row.mark_price)

    plans_by_date: dict[date, list[tuple[DecisionEvidence, dict[str, Any]]]] = defaultdict(list)
    rejected_matches_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for evidence in evidence_rows:
        versions = _json_object(evidence.versions_json)
        plan_id = str(versions.get("order_plan_id") or evidence.id)
        fill_dates = sorted({fill.execution_date for fill in fills_by_evidence.get(str(evidence.id), [])})
        raw_execution_date = versions.get("order_plan_execution_date")
        try:
            planned_date = date.fromisoformat(str(raw_execution_date))
        except (TypeError, ValueError):
            planned_date = evidence.trade_date
        report_date = fill_dates[0] if fill_dates else planned_date
        plan = {
            "order_plan_id": plan_id,
            "symbol_id": evidence.symbol_id,
            "side": versions.get("order_plan_direction") or evidence.action,
            "plan_qty": float(
                versions.get("order_plan_target_quantity")
                or abs(float(evidence.target_qty_delta or evidence.target_quantity or 0.0))
            ),
        }
        if _is_actionable_side(plan["side"]):
            plans_by_date[report_date].append((evidence, plan))
        order_status = str(versions.get("order_plan_status") or "").upper()
        if not fill_dates and (
            order_status == "REJECTED"
            or evidence.rejection_reason
            or evidence.action in {"REJECTED", "DATA_BLOCKED"}
        ):
            if _is_actionable_side(plan["side"]):
                rejected_matches_by_date[report_date].append({
                    **plan,
                    "status": "REJECTED",
                    "filled_qty": 0.0,
                    "avg_price": 0.0,
                })

    dates = sorted(set(fills_by_date) | set(plans_by_date))
    daily: list[dict[str, Any]] = []
    start_positions: dict[int, float] = defaultdict(float)
    start_cash = float(run.initial_capital or 0.0)
    for trade_date in dates:
        day_fills = fills_by_date.get(trade_date, [])
        decisions = []
        order_plans = []
        for evidence, plan in plans_by_date.get(trade_date, []):
            decisions.append({
                "symbol_id": evidence.symbol_id,
                "action": evidence.action,
                "target_qty": abs(float(evidence.target_qty_delta or evidence.target_quantity or 0.0)),
                "price_ref": evidence.intended_price,
            })
            order_plans.append(plan)

        end_positions = dict(start_positions)
        end_cash = start_cash
        for fill in day_fills:
            quantity = float(fill.quantity or 0.0)
            symbol_id = int(fill.symbol_id)
            if str(fill.side).upper() == "BUY":
                end_positions[symbol_id] = end_positions.get(symbol_id, 0.0) + quantity
            elif str(fill.side).upper() == "SELL":
                end_positions[symbol_id] = end_positions.get(symbol_id, 0.0) - quantity
            end_cash += _cash_delta(fill)

        report = evaluate_five_vector_conservation({
            "trade_date": trade_date,
            "start_cash": start_cash,
            "end_cash": end_cash,
            "start_positions": dict(start_positions),
            "end_positions": end_positions,
            "end_prices": prices_by_date.get(trade_date, {}),
            "decisions": decisions,
            "order_plans": order_plans,
            "match_results": [
                {
                    "order_plan_id": fill.order_plan_id,
                    "symbol_id": fill.symbol_id,
                    "side": fill.side,
                    "status": "FILLED",
                    "filled_qty": fill.quantity,
                    "avg_price": _conservation_price(fill),
                }
                for fill in day_fills
            ] + rejected_matches_by_date.get(trade_date, []),
        })
        daily.append({"trade_date": trade_date.isoformat(), **_serialize_report(report)})
        start_positions = defaultdict(float, {sid: qty for sid, qty in end_positions.items() if abs(qty) > 1e-12})
        start_cash = end_cash

    return {
        "run_id": run.id,
        "run_name": run.run_name,
        "reproducibility_status": status,
        "decision_run_count": len(decision_runs),
        "fill_count": len(fills),
        "daily": daily,
        "overall": "PASSED" if all(row["overall"] == "PASSED" for row in daily) else "BLOCKED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL or --db-url is required")
    engine = create_engine(args.db_url)
    with Session(engine) as db:
        run = db.get(BacktestRun, args.run_id)
        if run is None:
            raise SystemExit(f"backtest run {args.run_id} not found")
        print(json.dumps(build_report(db, run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
