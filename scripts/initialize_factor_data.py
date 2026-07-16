"""Safely initialize and warm the local factor data sources.

This command never activates shadow/ridge mode. It limits expensive per-stock
interfaces to the union of watchlist and positions, while market-wide feeds
remain bounded by their own API contracts.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from sqlalchemy import func, select

from app.core.config import build_mysql_url, load_db_config, settings
from app.db.init_db import init_db
from app.db.manager import DatabaseManager
from app.services.capital_flow_data import sync_symbol_capital_flow
from app.services.factors.config import update_factor_system_config
from app.services.factors.store import FactorWarehouse
from app.services.financial_data import (
    resolve_financial_report_symbols,
    sync_symbol_financial_reports,
)
from app.services.fundamental_data import sync_symbol_valuation
from app.services.hot_rank_data import sync_hot_rank_snapshot
from app.services.lhb_data import sync_lhb_institution_trades
from app.services.tail_proxy_data import sync_tail_proxy_snapshots
from app.models.universe import UniverseDailyBar, UniverseSymbol


def _initialize_manager() -> DatabaseManager:
    config = load_db_config()
    manager = DatabaseManager.get()
    mysql = config.get("mysql") or {}
    if config.get("use_mysql") and mysql.get("host"):
        manager.initialize(build_mysql_url(config), db_type="mysql")
    else:
        manager.initialize(settings.database_url, db_type="sqlite")
    return manager


def run(
    *,
    scope_limit: int,
    tail_limit: int,
    skip_tail: bool = False,
    only_valuations: bool = False,
) -> dict:
    manager = _initialize_manager()
    init_db()
    db = manager.get_session()
    try:
        config = update_factor_system_config(
            db,
            feature_enabled=True,
            actor="factor_initialization_script",
        )
        db.commit()
        warehouse = FactorWarehouse(config.warehouse_path)
        warehouse.initialize()
        watchlist = resolve_financial_report_symbols(
            db, source="watchlist", limit=scope_limit
        )
        positions = resolve_financial_report_symbols(
            db, source="positions", limit=scope_limit
        )
        symbols = list(
            {
                item.id: item
                for item in [*watchlist, *positions]
            }.values()
        )
        market_date = db.scalar(
            select(func.max(UniverseDailyBar.trade_date))
            .join(
                UniverseSymbol,
                UniverseSymbol.id
                == UniverseDailyBar.universe_symbol_id,
            )
            .where(
                UniverseSymbol.region == "cn",
                UniverseSymbol.asset_type == "stock",
            )
        ) or date.today()
        summary: dict = {
            "scope_symbols": [item.symbol for item in symbols],
            "market_date": market_date,
            "financial": {"ok": 0, "records": 0, "failed": 0},
            "valuation": {"ok": 0, "failed": 0},
            "flow": {"ok": 0, "failed": 0},
            "errors": [],
        }
        for symbol in symbols:
            if not only_valuations:
                try:
                    count = sync_symbol_financial_reports(db, symbol)
                    db.commit()
                    summary["financial"]["records"] += count
                    summary["financial"]["ok"] += int(count > 0)
                except Exception as exc:
                    db.rollback()
                    summary["financial"]["failed"] += 1
                    summary["errors"].append(
                        "financial:"
                        f"{symbol.symbol}:{type(exc).__name__}:{exc}"
                    )
            try:
                row = sync_symbol_valuation(db, symbol, market_date)
                db.commit()
                summary["valuation"]["ok"] += int(row is not None)
            except Exception as exc:
                db.rollback()
                summary["valuation"]["failed"] += 1
                summary["errors"].append(
                    "valuation:"
                    f"{symbol.symbol}:{type(exc).__name__}:{exc}"
                )
            if not only_valuations:
                try:
                    row = sync_symbol_capital_flow(
                        db, symbol, market_date
                    )
                    db.commit()
                    summary["flow"]["ok"] += int(row is not None)
                except Exception as exc:
                    db.rollback()
                    summary["flow"]["failed"] += 1
                    summary["errors"].append(
                        "flow:"
                        f"{symbol.symbol}:{type(exc).__name__}:{exc}"
                    )

        if only_valuations:
            summary["financial"] = {"skipped_by_request": True}
            summary["flow"] = {"skipped_by_request": True}
            summary["lhb"] = {"skipped_by_request": True}
            summary["hot_rank"] = {"skipped_by_request": True}
            summary["tail_proxy"] = {"skipped_by_request": True}
            summary["warehouse"] = warehouse.health().to_dict()
            return summary

        today = date.today()
        try:
            lhb = sync_lhb_institution_trades(
                db,
                start_date=today - timedelta(days=2),
                end_date=today,
            )
            db.commit()
            summary["lhb"] = {
                "received": lhb.received,
                "written": lhb.written,
                "unmatched": lhb.unmatched,
            }
        except Exception as exc:
            db.rollback()
            summary["lhb"] = {
                "error": f"{type(exc).__name__}:{exc}"
            }

        try:
            hot = sync_hot_rank_snapshot(db, as_of=today)
            db.commit()
            summary["hot_rank"] = {
                "received": hot.received,
                "written": hot.written,
                "unmatched": hot.unmatched,
            }
        except Exception as exc:
            db.rollback()
            summary["hot_rank"] = {
                "error": f"{type(exc).__name__}:{exc}"
            }

        if skip_tail:
            summary["tail_proxy"] = {"skipped_by_request": True}
        else:
            try:
                tail = sync_tail_proxy_snapshots(
                    db,
                    source="candidates",
                    limit=tail_limit,
                    trade_date=today,
                )
                db.commit()
                summary["tail_proxy"] = {
                    "total": tail.total,
                    "written": tail.written,
                    "skipped": tail.skipped,
                    "failed": tail.failed,
                    "errors": list(tail.errors),
                }
            except Exception as exc:
                db.rollback()
                summary["tail_proxy"] = {
                    "error": f"{type(exc).__name__}:{exc}"
                }
        summary["warehouse"] = warehouse.health().to_dict()
        return summary
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope-limit", type=int, default=20)
    parser.add_argument("--tail-limit", type=int, default=20)
    parser.add_argument("--skip-tail", action="store_true")
    parser.add_argument("--only-valuations", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.scope_limit <= 500:
        parser.error("--scope-limit must be between 1 and 500")
    if not 1 <= args.tail_limit <= 50:
        parser.error("--tail-limit must be between 1 and 50")
    print(
        json.dumps(
            run(
                scope_limit=args.scope_limit,
                tail_limit=args.tail_limit,
                skip_tail=args.skip_tail,
                only_valuations=args.only_valuations,
            ),
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
