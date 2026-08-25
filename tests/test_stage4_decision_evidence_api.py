"""Stage 4 API contract: single-symbol DecisionEvidence detail."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.decision_engine import (
    DecisionEvidence,
    DecisionRun,
    StrategyExecutionSnapshot,
)
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol


def _seed_evidence_detail(db_session):
    timestamp = datetime(2026, 8, 20, 20, 30)
    portfolio = Portfolio(
        name="stage4-evidence-detail",
        account_type="simulated",
        total_capital=100_000.0,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        currency="CNY",
    )
    symbol = Symbol(
        symbol="600000",
        name="Evidence Detail",
        market="SH",
        asset_type="stock",
    )
    db_session.add_all([portfolio, symbol])
    db_session.flush()

    snapshot = StrategyExecutionSnapshot(
        id="stage4-evidence-snapshot",
        snapshot_no=1,
        portfolio_id=portfolio.id,
        decision_clock_json="{}",
        member_snapshot_json="[]",
        snapshot_hash="a" * 64,
        effective_from=timestamp,
    )
    decision_run = DecisionRun(
        id="stage4-evidence-run",
        strategy_snapshot_id=snapshot.id,
        portfolio_id=portfolio.id,
        run_type="backtest",
        status="SUCCEEDED",
        trade_date=date(2026, 8, 20),
        decision_at=timestamp,
        data_cutoff_at=timestamp - timedelta(minutes=30),
        execution_at=timestamp + timedelta(days=1),
        universe_count=1,
        member_count=1,
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(decision_run)
    db_session.flush()
    evidence = DecisionEvidence(
        id="stage4-evidence-id",
        decision_run_id=decision_run.id,
        strategy_snapshot_id=snapshot.id,
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        trade_date=decision_run.trade_date,
        decision_at=decision_run.decision_at,
        data_cutoff_at=decision_run.data_cutoff_at,
        execution_at=decision_run.execution_at,
        action="REJECTED",
        action_subtype="REJECTED_ORDER_BELOW_LOT_SIZE",
        target_position_pct=0.03,
        min_lot_size=100,
        target_quantity=0.0,
        target_qty_delta=0.0,
        intended_price=10.25,
        slippage_bps=5.0,
        rejection_reason="ORDER_BELOW_LOT_SIZE",
        rejection_detail="available cash cannot fund one board lot",
        blocking_reason="cash reserve preserved",
        roll_forward_days=2,
        rejections_trace_json=json.dumps([
            {"date": "2026-08-21", "reason": "LIMIT_UP_DOWN", "intended_open": 10.25},
            {"date": "2026-08-22", "reason": "ORDER_BELOW_LOT_SIZE", "intended_open": 10.4},
        ]),
        exit_rules_hit_json=json.dumps([
            {"rule_code": "STOP_LOSS", "rule_priority": 1, "requested_exit_qty": 100},
        ]),
        constraints_json=json.dumps({"available_cash": 99.0}),
        versions_json=json.dumps({"factor_set_id": "fs-stage4"}),
        reason_codes_json=json.dumps(["ORDER_BELOW_LOT_SIZE"]),
        factor_contributions_json=json.dumps([{"factor_code": "quality", "contribution": 0.3}]),
        manual_price_flag=1,
        content_hash="b" * 64,
    )
    other_run = DecisionRun(
        id="stage4-evidence-other-run",
        strategy_snapshot_id=snapshot.id,
        portfolio_id=portfolio.id,
        run_type="backtest",
        status="SUCCEEDED",
        trade_date=date(2026, 8, 21),
        decision_at=timestamp + timedelta(days=1),
        data_cutoff_at=timestamp + timedelta(days=1, minutes=-30),
        execution_at=timestamp + timedelta(days=2),
        universe_count=1,
        member_count=1,
    )
    db_session.add(other_run)
    db_session.flush()
    other_evidence = DecisionEvidence(
        id="stage4-evidence-other-id",
        decision_run_id=other_run.id,
        strategy_snapshot_id=snapshot.id,
        portfolio_id=portfolio.id,
        symbol_id=symbol.id,
        trade_date=other_run.trade_date,
        decision_at=other_run.decision_at,
        data_cutoff_at=other_run.data_cutoff_at,
        execution_at=other_run.execution_at,
        action="HOLD",
        min_lot_size=100,
        target_quantity=100.0,
        target_qty_delta=0.0,
        content_hash="c" * 64,
    )
    db_session.add(evidence)
    db_session.add(other_evidence)
    db_session.commit()
    return decision_run.id, other_run.id, symbol.id


def test_get_single_decision_evidence_returns_complete_execution_detail(db_session):
    """The detail seam exposes data needed to explain a rejected order end-to-end."""
    from app.api.routes.decision_engine import get_run_symbol_evidence, router

    run_id, _, symbol_id = _seed_evidence_detail(db_session)

    detail = get_run_symbol_evidence(run_id, symbol_id, db_session)
    payload = detail.model_dump(mode="json")
    assert "/decision-runs/{run_id}/evidence/{symbol_id}" in {
        route.path for route in router.routes
    }
    assert payload["id"] == "stage4-evidence-id"
    assert payload["target_qty_delta"] == 0.0
    assert payload["blocking_reason"] == "cash reserve preserved"
    assert payload["roll_forward_days"] == 2
    assert payload["manual_price_flag"] is True
    assert payload["rejections_trace_json"] == [
        {"date": "2026-08-21", "reason": "LIMIT_UP_DOWN", "intended_open": 10.25},
        {"date": "2026-08-22", "reason": "ORDER_BELOW_LOT_SIZE", "intended_open": 10.4},
    ]
    assert payload["exit_rules_hit_json"] == [
        {"rule_code": "STOP_LOSS", "rule_priority": 1, "requested_exit_qty": 100},
    ]


def test_get_single_decision_evidence_is_scoped_to_run_and_returns_404_when_absent(db_session):
    """The same symbol has distinct evidence per run; absent rows are explicit 404s."""
    from app.api.routes.decision_engine import get_run_symbol_evidence

    run_id, other_run_id, symbol_id = _seed_evidence_detail(db_session)

    other_detail = get_run_symbol_evidence(other_run_id, symbol_id, db_session)
    assert other_detail.id == "stage4-evidence-other-id"
    assert other_detail.action == "HOLD"

    with pytest.raises(HTTPException) as exc_info:
        get_run_symbol_evidence(run_id, symbol_id + 1, db_session)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == {
        "error": "DECISION_EVIDENCE_NOT_FOUND",
        "decision_run_id": run_id,
        "symbol_id": symbol_id + 1,
    }
