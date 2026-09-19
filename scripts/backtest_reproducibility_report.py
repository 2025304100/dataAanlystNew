"""Report legacy backtests and their replay readiness.

Usage: python scripts/backtest_reproducibility_report.py [--db-url URL]
The report is read-only and emits JSON to stdout, so it is safe for SQLite
and MySQL and can be archived as a migration evidence artifact.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.backtest import BacktestRun  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.db_url:
        raise SystemExit("DATABASE_URL or --db-url is required")
    engine = create_engine(args.db_url)
    with Session(engine) as db:
        rows = db.execute(
            select(BacktestRun).order_by(BacktestRun.created_at, BacktestRun.id)
        ).scalars().all()
        counts: dict[str, int] = {}
        items = []
        for run in rows:
            status = getattr(run, "reproducibility_status", None)
            if not status:
                status = "reproducible" if run.strategy_snapshot_id else "legacy/non_reproducible"
            counts[status] = counts.get(status, 0) + 1
            items.append({
                "id": run.id,
                "run_name": run.run_name,
                "status": run.status,
                "reproducibility_status": status,
                "reason": getattr(run, "reproducibility_reason", None),
                "strategy_snapshot_id": run.strategy_snapshot_id,
                "decision_run_ids_json": run.decision_run_ids_json,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            })
    print(json.dumps({"total": len(items), "counts": counts, "runs": items}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
