from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select

from app.models.decision_engine import DecisionEvidence, DecisionOrderPlanRecord, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.decision_engine import default_engine


def _seed_persistable_decision(db):
    portfolio = Portfolio(
        name="order-plan-ledger", account_type="simulated", asset_scope="mixed",
        total_capital=100_000, investable_ratio=0.95, cash_reserve_ratio=0.05,
    )
    symbol = Symbol(symbol="600777", name="Order Plan Ledger", market="SH", asset_type="stock")
    db.add_all([portfolio, symbol])
    db.flush()
    trade_date = date(2025, 1, 6)
    from app.models.daily_bar import DailyBar
    db.add_all([
        DailyBar(symbol_id=symbol.id, trade_date=trade_date, open=10, high=10.2, low=9.8, close=10, volume=1_000_000, amount=10_000_000),
        DailyBar(symbol_id=symbol.id, trade_date=date(2025, 1, 7), open=10.1, high=10.3, low=10, close=10.2, volume=1_000_000, amount=10_100_000),
        Score(symbol_id=symbol.id, trade_date=trade_date, quality_score=.9, quality_grade="A", timing_score=.8, stage="growth", action="open", priority_score=.8, weight_mode="manual", factor_model_run_id="ledger-model", calc_batch_id="ledger-batch", published_at=datetime(2025, 1, 1)),
    ])
    snapshot = StrategyExecutionSnapshot(
        id="order-plan-ledger-snapshot", snapshot_no=1, portfolio_id=portfolio.id,
        factor_model_run_id="ledger-model", decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id, "member_id": 1}]),
        snapshot_type="save_and_apply", snapshot_hash="order-plan-ledger-hash",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2025, 1, 1),
    )
    db.add(snapshot)
    db.commit()
    return portfolio, snapshot, trade_date


def test_persisted_decision_writes_one_immutable_plan_per_evidence(db_session):
    portfolio, snapshot, trade_date = _seed_persistable_decision(db_session)

    result = default_engine.evaluate(
        db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=trade_date, run_type="backtest", dry_run=False,
    )
    db_session.commit()
    plans = db_session.execute(
        select(DecisionOrderPlanRecord).where(
            DecisionOrderPlanRecord.decision_run_id == result.decision_run_id,
        )
    ).scalars().all()
    evidence = db_session.execute(
        select(DecisionEvidence).where(DecisionEvidence.decision_run_id == result.decision_run_id)
    ).scalars().all()

    assert len(plans) == len(evidence) == len(result.order_plans)
    by_evidence = {plan.evidence_id: plan for plan in plans}
    for row in evidence:
        envelope = json.loads(row.versions_json or "{}")
        assert by_evidence[row.id].order_plan_id == envelope["order_plan_id"]
        assert by_evidence[row.id].execution_date.isoformat() == envelope["order_plan_execution_date"]


def test_order_plan_ledger_reuses_existing_decision_without_duplicate_rows(db_session):
    portfolio, snapshot, trade_date = _seed_persistable_decision(db_session)
    first = default_engine.evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=trade_date, run_type="backtest", dry_run=False)
    db_session.commit()
    second = default_engine.evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id, trade_date=trade_date, run_type="backtest", dry_run=False)
    db_session.commit()

    assert first.decision_run_id == second.decision_run_id
    assert db_session.execute(
        select(DecisionOrderPlanRecord).where(
            DecisionOrderPlanRecord.decision_run_id == first.decision_run_id,
        )
    ).scalars().all().__len__() == len(first.order_plans)


def test_order_plan_query_route_reads_relational_ledger(db_session):
    portfolio, snapshot, trade_date = _seed_persistable_decision(db_session)
    result = default_engine.evaluate(
        db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=trade_date, run_type="backtest", dry_run=False,
    )
    db_session.commit()
    from app.api.routes.decision_engine import list_run_order_plans

    payload = list_run_order_plans(result.decision_run_id, limit=10, offset=0, db=db_session)
    assert payload["total"] == len(result.order_plans)
    assert payload["items"][0]["order_plan_id"] == result.order_plans[0].order_plan_id
    assert payload["items"][0]["evidence_id"] == result.order_plans[0].evidence_id
