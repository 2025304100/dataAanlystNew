"""Run a bounded local-only factor warehouse warm-up."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date

from app.services.factors.bar_mirror import mirror_daily_bars
from app.services.factors.config import get_factor_system_config
from app.services.factors.data_sync import mirror_factor_inputs
from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.health import get_factor_health
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets
from scripts.initialize_factor_data import _initialize_manager


def run(
    *, start_date: date, end_date: date, skip_bars: bool = False
) -> dict:
    manager = _initialize_manager()
    # Importing init_db registers every ORM mapper used by the business DB.
    from app.db.init_db import init_db

    init_db()
    db = manager.get_session()
    try:
        config = get_factor_system_config(db)
        if not config.feature_enabled:
            raise RuntimeError("factor feature is not enabled")
        warehouse = FactorWarehouse(config.warehouse_path)
        bars = None
        if not skip_bars:
            bars = mirror_daily_bars(
                db,
                warehouse=warehouse,
                start_date=start_date,
                end_date=end_date,
                batch_size=5000,
            )
        inputs = mirror_factor_inputs(
            db,
            warehouse=warehouse,
            start_date=start_date,
            end_date=end_date,
            batch_size=5000,
        )
        factors = calculate_stock_factors(
            warehouse,
            start_date=start_date,
            end_date=end_date,
            valuation_max_age_days=7,
        )
        targets = calculate_targets(
            warehouse,
            start_date=start_date,
            end_date=end_date,
        )
        health = get_factor_health(
            warehouse, calc_batch_id=factors.calc_batch_id
        )
        return {
            "range": {
                "start_date": start_date,
                "end_date": end_date,
            },
            "bars": bars.to_dict() if bars is not None else {
                "skipped_by_request": True
            },
            "inputs": {
                **asdict(inputs),
                "sentiment_rows": inputs.sentiment_rows,
                "rows_written": inputs.rows_written,
            },
            "factors": asdict(factors),
            "targets": asdict(targets),
            "health": health.to_dict(),
        }
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--skip-bars", action="store_true")
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date")
    print(
        json.dumps(
            run(
                start_date=args.start_date,
                end_date=args.end_date,
                skip_bars=args.skip_bars,
            ),
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
