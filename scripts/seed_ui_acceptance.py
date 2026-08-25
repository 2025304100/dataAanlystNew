"""Create an isolated SQLite fixture for real backtest UI acceptance.

The fixture intentionally contains a persisted backtest, linked DecisionRun,
trades, and more than one evidence page.  It is not a product data seeder and
refuses non-SQLite URLs or an existing database file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from app.db.init_db import init_db
from app.db.manager import DatabaseManager
from app.db.session import get_session_local
from app.models.backtest import (
    BacktestExecutionFill,
    BacktestRun,
    BacktestTrade,
    BacktestValuationSnapshot,
)
from app.models.decision_engine import (
    DecisionEvidence,
    DecisionRun,
    StrategyExecutionSnapshot,
)
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol


TRADE_DATE = date(2026, 8, 20)
DECISION_AT = datetime(2026, 8, 20, 15, 5)
CUTOFF_AT = datetime(2026, 8, 20, 15, 0)
EXECUTION_AT = datetime(2026, 8, 21, 9, 30)
SNAPSHOT_ID = "ui-acceptance-snapshot-20260821"
DECISION_RUN_ID = "ui-acceptance-decision-run-20260821"


def sqlite_path_from_url(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("Only sqlite:/// database URLs are accepted")
    raw_path = database_url[len(prefix):]
    if not raw_path or raw_path == ":memory:":
        raise ValueError("A file-backed SQLite URL is required")
    return Path(raw_path)


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seed(database_url: str) -> dict[str, int | str]:
    db_path = sqlite_path_from_url(database_url)
    if db_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing database: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    manager = DatabaseManager.get()
    manager.initialize(database_url, db_type="sqlite")
    try:
        init_db()
        SessionLocal = get_session_local()
        with SessionLocal() as db:
            # Prevent the application lifespan from triggering initial external
            # universe synchronization when this fixture starts its API server.
            db.add(
                UniverseSymbol(
                    symbol="000001",
                    name="Fixture Universe Symbol",
                    asset_type="stock",
                    market="sz",
                    region="cn",
                    board="main",
                    is_synced=1,
                    bar_count=1,
                    last_bar_date=TRADE_DATE,
                )
            )
            symbols = [
                Symbol(
                    symbol=f"UI{i:04d}",
                    name=f"Evidence Fixture {i:02d}",
                    asset_type="stock",
                    market="sz",
                    board="main",
                    industry="fixture",
                )
                for i in range(1, 56)
            ]
            db.add_all(symbols)
            portfolio = Portfolio(
                name="Backtest Showcase",
                account_type="simulated",
                asset_scope="stock",
                total_capital=1_000_000,
                investable_ratio=0.95,
                cash_reserve_ratio=0.05,
                is_default=1,
                auto_trade_enabled=1,
                buy_fee_pct=0.0003,
                sell_fee_pct=0.0013,
                benchmark_code="000300",
                default_single_position_pct=0.2,
                is_test=0,
            )
            db.add(portfolio)
            db.flush()

            actions = ["BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED"]
            run_specs = [
                ("PIT Backtest Fixture", "completed", "READY"),
                ("PIT Backtest Fixture Secondary", "completed", "READY"),
                ("PIT Backtest Fixture Blocked", "blocked", "DATA_INCOMPLETE_PAUSED"),
            ]
            all_runs: list[BacktestRun] = []
            all_evidence: list[DecisionEvidence] = []
            total_partial_fills = 0
            total_execution_fills = 0

            for run_index, (run_name, run_status, blocking_status) in enumerate(run_specs, start=1):
                snapshot_id = f"{SNAPSHOT_ID}-{run_index}"
                decision_run_id = f"{DECISION_RUN_ID}-{run_index}"
                run_trade_date = TRADE_DATE - timedelta(days=run_index - 1)
                run_decision_at = DECISION_AT - timedelta(days=run_index - 1)
                run_cutoff_at = CUTOFF_AT - timedelta(days=run_index - 1)
                run_execution_at = EXECUTION_AT - timedelta(days=run_index - 1)
                snapshot = StrategyExecutionSnapshot(
                    id=snapshot_id,
                    snapshot_no=run_index,
                    portfolio_id=portfolio.id,
                    decision_clock_json=json.dumps({"decision_at": "15:05", "data_cutoff_at": "15:00", "execution_at": "T+1 09:30"}),
                    cost_config_json=json.dumps({"commission_rate": 0.0003, "slippage_bps": 5}),
                    member_snapshot_json=json.dumps([]),
                    candidate_pool_json=json.dumps([]),
                    universe_type="portfolio_members",
                    benchmark_code="000300",
                    snapshot_type="save_and_apply",
                    snapshot_hash=content_hash(f"ui-acceptance-snapshot-{run_index}"),
                    effective_from=run_decision_at,
                    gate_policy_version="ui-acceptance",
                    versions_json=json.dumps({"fixture": True, "run_index": run_index}),
                    created_by="ui_acceptance",
                )
                db.add(snapshot)
                db.flush()
                decision_run = DecisionRun(
                    id=decision_run_id,
                    strategy_snapshot_id=snapshot_id,
                    portfolio_id=portfolio.id,
                    run_type="backtest",
                    status="SUCCEEDED",
                    summary="Persisted fixture for real UI acceptance.",
                    checks_json=json.dumps({"score_coverage_pct": 100}),
                    result_hash=content_hash(f"ui-acceptance-decision-run-{run_index}"),
                    trade_date=run_trade_date,
                    decision_at=run_decision_at,
                    data_cutoff_at=run_cutoff_at,
                    execution_at=run_execution_at,
                    run_mode="research",
                    pit_mode="best_effort",
                    universe_count=len(symbols),
                    member_count=len(symbols),
                    score_count_expected=len(symbols),
                    score_count_actual=len(symbols),
                    score_coverage_pct=100.0,
                    score_max_age_days=0,
                    blocking_status=blocking_status,
                    blocking_reasons_json=json.dumps([] if blocking_status == "READY" else [{"code": "DATA_INCOMPLETE_PAUSED", "message": "Fixture blocked run"}]),
                    versions_json=json.dumps({"strategy_snapshot_id": snapshot_id, "fixture": True}),
                    idempotency_key=content_hash(f"ui-acceptance-decision-run-key-{run_index}"),
                    started_at=run_decision_at,
                    finished_at=run_execution_at,
                    duration_ms=1_000,
                    match_mode="NEXT_OPEN",
                )
                db.add(decision_run)
                db.flush()

                evidence_by_action: dict[str, list[DecisionEvidence]] = {action: [] for action in actions}
                for symbol_index, symbol in enumerate(symbols, start=1):
                    action = actions[(symbol_index - 1) % len(actions)]
                    rejected = action in {"REJECTED", "DATA_BLOCKED"}
                    partial = run_index == 1 and symbol_index == 1
                    versions = {"order_plan_id": f"ui-plan-{run_index:02d}-{symbol_index:03d}"}
                    if partial:
                        versions.update({
                            "order_plan_status": "PARTIAL_FILL_PENDING",
                            "order_plan_requested_quantity": 1_000.0,
                            "order_plan_filled_quantity": 600.0,
                            "order_plan_remaining_quantity": 400.0,
                            "order_plan_unfilled_reason": "VOLUME_LIMIT",
                        })
                        total_partial_fills += 1
                    evidence = DecisionEvidence(
                        id=f"ui-acceptance-evidence-{run_index:02d}-{symbol_index:03d}",
                        decision_run_id=decision_run_id,
                        strategy_snapshot_id=snapshot_id,
                        portfolio_id=portfolio.id,
                        symbol_id=symbol.id,
                        trade_date=run_trade_date,
                        decision_at=run_decision_at,
                        data_cutoff_at=run_cutoff_at,
                        execution_at=run_execution_at,
                        action=action,
                        action_subtype=("REJECTED_ORDER_BELOW_LOT_SIZE" if action == "REJECTED" else None),
                        target_position_pct=0.0 if rejected else 0.1,
                        min_lot_size=100,
                        target_quantity=0.0 if rejected else 1_000.0,
                        target_qty_delta=0.0 if rejected else 1_000.0,
                        intended_price=10.0 + symbol_index / 100,
                        executed_price=None if rejected else 10.05 + symbol_index / 100,
                        slippage_bps=5.0,
                        rejection_reason=("ORDER_BELOW_LOT_SIZE" if action == "REJECTED" else "SOURCE_MISSING" if action == "DATA_BLOCKED" else None),
                        rejection_detail=("Fixture rejection for UI pagination." if rejected else None),
                        blocking_reason=("Fixture data block" if action == "DATA_BLOCKED" else None),
                        roll_forward_days=0,
                        rejections_trace_json=json.dumps([]),
                        exit_rules_hit_json=json.dumps([]),
                        score_value=round(0.5 + symbol_index / 1000, 4),
                        score_rank=symbol_index,
                        score_published_at=run_cutoff_at,
                        pit_safe_flag="PIT_SAFE",
                        constraints_json=json.dumps({"fixture_constraint": "none"}),
                        versions_json=json.dumps(versions),
                        reason_codes_json=json.dumps(["UI_ACCEPTANCE_FIXTURE"]),
                        factor_contributions_json=json.dumps([{"factor": "fixture_factor", "contribution": 0.1}]),
                        content_hash=content_hash(f"ui-acceptance-evidence-{run_index}-{symbol_index}"),
                        match_mode="NEXT_OPEN",
                    )
                    db.add(evidence)
                    evidence_by_action[action].append(evidence)
                    all_evidence.append(evidence)
                db.flush()

                backtest = BacktestRun(
                    portfolio_id=portfolio.id,
                    run_name=run_name,
                    symbols_json=json.dumps([symbol.id for symbol in symbols]),
                    symbol_ids_json=json.dumps([symbol.id for symbol in symbols]),
                    rule_config_json=json.dumps({"fixture": True, "run_index": run_index}),
                    cost_config_json=json.dumps({"commission_rate": 0.0003, "slippage_bps": 5}),
                    score_weight_mode="manual",
                    start_date=date(2026, 7, 1),
                    end_date=run_trade_date,
                    initial_capital=1_000_000,
                    total_return=12_500 if run_status == "completed" else None,
                    total_return_pct=0.0125 if run_status == "completed" else None,
                    max_drawdown=-4_800 if run_status == "completed" else None,
                    max_drawdown_pct=-0.0048 if run_status == "completed" else None,
                    sharpe_ratio=1.42 if run_status == "completed" else None,
                    win_rate=0.6 if run_status == "completed" else None,
                    profit_factor=1.8 if run_status == "completed" else None,
                    trade_count=42,
                    avg_holding_days=8 if run_status == "completed" else None,
                    equity_curve_json=json.dumps([] if run_status != "completed" else [{"date": "2026-07-01", "equity": 1_000_000}, {"date": run_trade_date.isoformat(), "equity": 1_012_500}]),
                    status=run_status,
                    error_message=None if run_status == "completed" else "Fixture blocked run",
                    reproducibility_status="reproducible",
                    reproducibility_reason=None,
                    created_at=run_execution_at,
                    started_at=run_decision_at,
                    finished_at=run_execution_at,
                    member_snapshot_json=json.dumps([]),
                    data_cutoff_at=run_cutoff_at,
                    engine_name="simulation_matching_engine",
                    engine_version="ui-acceptance",
                    source_type="portfolio_members",
                    strategy_snapshot_id=snapshot_id,
                    pit_mode="strict_pit_safe",
                    decision_run_ids_json=json.dumps([decision_run_id]),
                    benchmark_equity_json=json.dumps([]),
                    benchmark_status="SOURCE_MISSING",
                    benchmark_gap_days=0,
                    match_mode="NEXT_OPEN",
                )
                db.add(backtest)
                db.flush()
                created_trades: list[BacktestTrade] = []
                used_exit_evidence_ids: set[str] = set()
                for trade_index in range(42):
                    entry_evidence = evidence_by_action["BUY"][trade_index % len(evidence_by_action["BUY"])]
                    exit_evidence = evidence_by_action["SELL"][trade_index % len(evidence_by_action["SELL"])]
                    trade = BacktestTrade(
                        run_id=backtest.id,
                        symbol_id=entry_evidence.symbol_id,
                        entry_date=date(2026, 8, 3) + timedelta(days=trade_index % 10),
                        entry_price=10.0 + trade_index / 100,
                        quantity=600 if run_index == 1 and trade_index == 0 else 1_000,
                        exit_date=date(2026, 8, 12) + timedelta(days=trade_index % 10) if trade_index % 3 == 0 else None,
                        exit_price=10.5 + trade_index / 100 if trade_index % 3 == 0 else None,
                        exit_reason="TAKE_PROFIT" if trade_index % 3 == 0 else None,
                        pnl=480 if trade_index % 3 == 0 else None,
                        pnl_pct=0.048 if trade_index % 3 == 0 else None,
                        hold_days=9 if trade_index % 3 == 0 else None,
                        entry_cost=3,
                        exit_cost=17 if trade_index % 3 == 0 else None,
                        decision_evidence_id=entry_evidence.id,
                        exit_evidence_id=exit_evidence.id if trade_index % 3 == 0 else None,
                        intended_entry_price=10.005 + trade_index / 100,
                        slippage_bps=5,
                    )
                    db.add(trade)
                    created_trades.append(trade)
                    if trade.exit_date and trade.exit_price is not None:
                        used_exit_evidence_ids.add(exit_evidence.id)
                db.flush()

                # Evidence rows that represent a sell decision but never got
                # an aggregate exit lot are explicit rejected plans. This
                # keeps the conservation report fail-closed without treating
                # an intentionally rejected order as a missing fill.
                for evidence in evidence_by_action["SELL"]:
                    if evidence.id in used_exit_evidence_ids:
                        continue
                    versions = json.loads(evidence.versions_json or "{}")
                    versions["order_plan_status"] = "REJECTED"
                    versions["order_plan_rejection_reason"] = "FIXTURE_NO_EXIT_LOT"
                    evidence.versions_json = json.dumps(versions)
                    evidence.rejection_reason = "FIXTURE_NO_EXIT_LOT"
                db.flush()

                # Keep the aggregate trade view and immutable execution ledger
                # both populated. The first run includes a real volume-limited
                # partial fill; the remaining runs use complete executions.
                for trade_index, trade in enumerate(created_trades):
                    entry_evidence = evidence_by_action["BUY"][trade_index % len(evidence_by_action["BUY"])]
                    entry_quantity = float(trade.quantity)
                    first_quantity = 600.0 if run_index == 1 and trade_index == 0 else entry_quantity
                    db.add(BacktestExecutionFill(
                        run_id=backtest.id,
                        backtest_trade_id=trade.id,
                        symbol_id=trade.symbol_id,
                        execution_date=trade.entry_date,
                        side="BUY",
                        quantity=first_quantity,
                        executed_price=trade.entry_price,
                        cost=trade.entry_cost,
                        decision_evidence_id=entry_evidence.id,
                        order_plan_id=json.loads(entry_evidence.versions_json or "{}").get(
                            "order_plan_id", entry_evidence.id
                        ),
                    ))
                    total_execution_fills += 1
                    if trade.exit_date and trade.exit_price is not None:
                        exit_evidence = evidence_by_action["SELL"][trade_index % len(evidence_by_action["SELL"])]
                        db.add(BacktestExecutionFill(
                            run_id=backtest.id,
                            backtest_trade_id=trade.id,
                            symbol_id=trade.symbol_id,
                            execution_date=trade.exit_date,
                            side="SELL",
                            quantity=entry_quantity,
                            executed_price=trade.exit_price,
                            cost=trade.exit_cost or 0.0,
                            decision_evidence_id=exit_evidence.id,
                            order_plan_id=json.loads(exit_evidence.versions_json or "{}").get(
                                "order_plan_id", exit_evidence.id
                            ),
                        ))
                        total_execution_fills += 1

                # Persist marks for symbols touched by the run so the report
                # has immutable valuation metadata even when source bars move.
                valuation_dates = {trade.entry_date for trade in created_trades}
                valuation_dates.update(trade.exit_date for trade in created_trades if trade.exit_date)
                for valuation_date in valuation_dates:
                    for symbol in symbols:
                        mark = 10.0 + symbol.id / 100.0
                        db.add(BacktestValuationSnapshot(
                            run_id=backtest.id,
                            trade_date=valuation_date,
                            symbol_id=symbol.id,
                            mark_price=mark,
                            market_value=0.0,
                            portfolio_equity=1_000_000.0,
                            weight=0.0,
                            price_source="ui_acceptance_fixture",
                            price_available_at=run_execution_at,
                        ))
                all_runs.append(backtest)

            db.commit()
            return {
                "portfolio_id": portfolio.id,
                "backtest_run_id": all_runs[0].id,
                "decision_run_id": f"{DECISION_RUN_ID}-1",
                "evidence_count": len(all_evidence),
                "backtest_run_count": len(all_runs),
                "trade_count": 126,
                "rejected_evidence_count": sum(1 for evidence in all_evidence if evidence.action in {"REJECTED", "DATA_BLOCKED"}),
                "partial_fill_count": total_partial_fills,
                "execution_fill_count": total_execution_fills,
            }
    finally:
        manager.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    print(json.dumps(seed(args.database_url), sort_keys=True))


if __name__ == "__main__":
    main()
