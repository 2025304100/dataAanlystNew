"""Focused contracts for the backtest result echo surface (G3 BT-UI-20)."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app.api.routes.backtest import _enrich_backtest_result_contract
from app.models.backtest import BacktestRun
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.schemas.backtest import BacktestRunDetail, PortfolioBacktestResult


pytestmark = pytest.mark.whitebox


def test_portfolio_result_schema_keeps_execution_snapshot_and_gate_fields():
    result = PortfolioBacktestResult(
        run_id=1,
        portfolio_id=2,
        symbol_ids=[10],
        symbol_count=1,
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        initial_capital=100_000,
        status="completed",
        run_name="contract",
        benchmark="沪深300",
        commission_rate=0.0003,
        stamp_tax_rate=0.001,
        slippage_bps=5,
        price_type="NEXT_OPEN",
        volume_limit_pct=0.1,
        rebalance_frequency="on_signal",
        pit_mode="production_pit",
        strategy_snapshot_id="snapshot-1",
        factor_set_id="factor-set-1",
        benchmark_status="SOURCE_MISSING",
        gate_policy_version="production-v1.0.0",
        gate_result="blocked",
        blocking_status="DATA_INCOMPLETE_PAUSED",
        decision_snapshot={"snapshot_hash": "a" * 64},
    )
    payload = result.model_dump(mode="json")
    assert payload["benchmark"] == "沪深300"
    assert payload["price_type"] == "NEXT_OPEN"
    assert payload["strategy_snapshot_id"] == "snapshot-1"
    assert payload["benchmark_status"] == "SOURCE_MISSING"
    assert payload["gate_result"] == "blocked"
    assert payload["blocking_status"] == "DATA_INCOMPLETE_PAUSED"


def test_result_projection_reads_persisted_cost_benchmark_and_gate(db_session):
    portfolio = Portfolio(
        name="result-contract",
        account_type="simulated",
        asset_scope="stock",
        total_capital=100_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )
    db_session.add(portfolio)
    db_session.flush()
    snapshot = StrategyExecutionSnapshot(
        id="result-contract-snapshot",
        snapshot_no=7,
        portfolio_id=portfolio.id,
        decision_clock_json="{}",
        cost_config_json=json.dumps({"commission_rate": 0.0006, "stamp_duty_rate": 0.0012}),
        member_snapshot_json=json.dumps([{"symbol_id": 11}]),
        snapshot_type="save_and_apply",
        snapshot_hash="b" * 64,
        benchmark_code="000300",
        gate_policy_version="production-v1.0.0",
        gate_result_json=json.dumps({"pass": False, "warnings": [{"severity": "blocking", "code": "PIT"}]}),
        effective_from=datetime(2026, 8, 1, 15, 5),
    )
    db_session.add(snapshot)
    db_session.flush()
    run = BacktestRun(
        portfolio_id=portfolio.id,
        run_name="result-contract-run",
        symbols_json="[11]",
        rule_config_json="{}",
        cost_config_json=json.dumps({
            "commission_rate": 0.0006,
            "stamp_duty_rate": 0.0012,
            "slippage_buy_bps": 7,
            "volume_limit_pct": 0.2,
            "price_type": "NEXT_OPEN",
        }),
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        initial_capital=100_000,
        status="completed",
        strategy_snapshot_id=snapshot.id,
        factor_set_id="factor-set-1",
        pit_mode="strict_pit_safe",
        benchmark_status="SOURCE_MISSING",
        benchmark_gap_days=2,
        benchmark_equity_json=json.dumps([{"date": "2026-08-02", "benchmark": None}]),
        member_snapshot_json=json.dumps([{"symbol_id": 11}]),
        data_cutoff_at=datetime(2026, 8, 2, 15),
    )
    db_session.add(run)
    db_session.commit()

    projected = _enrich_backtest_result_contract(
        db_session,
        {"run_id": run.id},
        benchmark_name="沪深300",
    )
    assert projected["benchmark"] == "沪深300"
    assert projected["benchmark_code"] == "000300"
    assert projected["commission_rate"] == pytest.approx(0.0006)
    assert projected["stamp_tax_rate"] == pytest.approx(0.0012)
    assert projected["slippage_bps"] == 7
    assert projected["pit_mode"] == "strict_pit_safe"
    assert projected["strategy_snapshot_id"] == snapshot.id
    assert projected["snapshot_no"] == 7
    assert projected["snapshot_hash"] == "b" * 64
    assert projected["benchmark_status"] == "SOURCE_MISSING"
    assert projected["benchmark_gap_days"] == 2
    assert projected["gate_policy_version"] == "production-v1.0.0"
    assert projected["gate_result"] == "blocked"
    assert projected["blocking_status"] == "GATE_BLOCKED"
    assert projected["is_result_production_eligible"] is False
    assert projected["data_snapshot"]["member_count"] == 1


def test_backtest_detail_schema_accepts_benchmark_and_data_snapshot():
    detail = BacktestRunDetail(
        id=1,
        portfolio_id=2,
        run_name="detail",
        symbols_json="[]",
        rule_config_json="{}",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        initial_capital=1_000,
        status="failed",
        created_at=datetime(2026, 8, 2),
        benchmark_equity=[{"date": "2026-08-02", "benchmark": None}],
        data_snapshot={"pit_mode": "research_pit"},
        gate_result="blocked",
    )
    assert detail.benchmark_equity[0]["benchmark"] is None
    assert detail.data_snapshot == {"pit_mode": "research_pit"}


def test_result_projection_marks_legacy_runs_non_reproducible(db_session):
    portfolio = Portfolio(
        name="legacy-reproducibility",
        account_type="simulated",
        asset_scope="stock",
        total_capital=100_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )
    db_session.add(portfolio)
    db_session.flush()
    run = BacktestRun(
        portfolio_id=portfolio.id,
        run_name="pre-decision-chain",
        symbols_json="[]",
        rule_config_json="{}",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 2),
        initial_capital=100_000,
        status="completed",
    )
    db_session.add(run)
    db_session.commit()

    projected = _enrich_backtest_result_contract(db_session, {"run_id": run.id})
    assert projected["reproducibility_status"] == "legacy/non_reproducible"


def test_result_projection_keeps_reproducible_snapshot_backed_run(db_session):
    portfolio = Portfolio(
        name="reproducible-run",
        account_type="simulated",
        asset_scope="stock",
        total_capital=100_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )
    db_session.add(portfolio)
    db_session.flush()
    snapshot = StrategyExecutionSnapshot(
        id="reproducible-snapshot",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json="{}",
        cost_config_json="{}",
        member_snapshot_json="[]",
        snapshot_type="save_and_apply",
        snapshot_hash="c" * 64,
        effective_from=datetime(2026, 8, 1, 15, 0),
    )
    db_session.add(snapshot)
    db_session.flush()
    run = BacktestRun(
        portfolio_id=portfolio.id,
        run_name="decision-driven",
        symbols_json="[]",
        rule_config_json="{}",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        initial_capital=100_000,
        status="completed",
        strategy_snapshot_id=snapshot.id,
        decision_run_ids_json="[]",
        reproducibility_status="reproducible",
    )
    db_session.add(run)
    db_session.commit()

    projected = _enrich_backtest_result_contract(db_session, {"run_id": run.id})
    assert projected["reproducibility_status"] == "reproducible"
