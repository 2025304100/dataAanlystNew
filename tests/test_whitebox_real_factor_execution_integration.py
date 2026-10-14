"""Actual frozen input calculation and Ridge fitting reach the trading engine."""
import json
from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.portfolio_member import PortfolioMember
from app.models.score import Score
from app.services.decision_engine import default_engine
from app.services.factors.runtime import activate_factor_model
from app.services.factors.scoring_bridge import materialize_factor_scores
from tests.factor_real_pipeline_helpers import build_real_factor_pipeline
from tests.test_factor_release_and_strategy_transaction import _request
from tests.test_whitebox_portfolio_confirmation import _confirm, _legacy_state

pytestmark = pytest.mark.whitebox


def test_real_fit_apply_confirm_and_default_alpha_decisions(db_session, tmp_path, monkeypatch):
    from app.models import factor_model, score
    from app.services.factors import factor_engine, target_engine, scoring_bridge, runtime
    from app.services import factor_usage_service
    from app.services.decision_clock import resolve
    from app.api.routes.portfolios import save_strategy_configuration
    from app.services.auto_trade_member_source import _build_snapshot_decision_state_context

    # Control the producer clock for a reproducible isolated session. Market
    # inputs, factor formulas, target returns, fitting and scores remain real.
    today = date(2026, 10, 13)
    publication = datetime.combine(today, datetime.min.time())

    class ProducerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return publication.replace(tzinfo=timezone.utc).astimezone(tz) if tz else publication

    for producer in (factor_engine, target_engine, scoring_bridge):
        monkeypatch.setattr(producer, "_utcnow_naive", lambda: publication)
    monkeypatch.setattr(factor_model, "datetime", ProducerClock)
    monkeypatch.setattr(score, "datetime", ProducerClock)
    monkeypatch.setattr(runtime, "_now", lambda: publication)
    monkeypatch.setattr(factor_usage_service, "_utcnow_naive", lambda: publication)
    monkeypatch.setattr(factor_usage_service, "_resolve_clock_for_now", lambda run_mode="research": resolve(
        today, decision_time=time(20, 30), cutoff_time=time(20)))

    pipeline = build_real_factor_pipeline(db_session, tmp_path, model_run_id="real-execution-model", end_date=today)
    activate_factor_model(db_session, pipeline["model_run_id"], mode="ridge", actor="isolated_test")
    db_session.commit()
    scored = materialize_factor_scores(db_session, pipeline["warehouse"], trade_date=pipeline["trade_date"],
        factor_calc_batch_id=pipeline["factors"].calc_batch_id, model_run_id=pipeline["model_run_id"], mode="ridge")
    db_session.commit()
    rows = db_session.scalars(select(Score).where(Score.calc_batch_id == scored.calc_batch_id)).all()
    assert len(rows) == len(pipeline["symbols"])
    assert min(row.model_alpha_score for row in rows) < 35 < 70 < max(row.model_alpha_score for row in rows)

    portfolio = Portfolio(name="Real factor execution", account_type="simulated", total_capital=100000,
        investable_ratio=.95, cash_reserve_ratio=.05, auto_trade_enabled=1, auto_trade_source_mode="members_only")
    db_session.add(portfolio)
    db_session.flush()
    for symbol in pipeline["symbols"]:
        db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id, execution_mode="auto",
            effective_from=publication, source_type="manual"))
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=pipeline["trade_date"], open=10, high=10.1,
            low=9.9, close=10, volume=1000000, amount=10000000, created_at=publication))
    _legacy_state(db_session, portfolio, "READY")
    db_session.commit()

    saved = save_strategy_configuration(portfolio.id, _request(pipeline["model_run_id"], pipeline["factor_set"].id), db_session)
    snapshot = db_session.get(StrategyExecutionSnapshot, saved.binding_result.strategy_snapshot_id)
    assert saved.applied and saved.requires_confirmation and portfolio.status_model == "MODEL_INACTIVE"
    assert json.loads(snapshot.versions_json)["factor_member_versions"] == {"ep_ttm": 2}
    _confirm(db_session, portfolio, snapshot)
    assert portfolio.status_model == "READY"

    state = _build_snapshot_decision_state_context(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=pipeline["trade_date"], dry_run=True)
    evaluated = default_engine.evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=pipeline["trade_date"], run_type="auto_simulation", dry_run=True, state_context=state)
    assert evaluated.score_coverage_pct == 100
    assert all(ev.pit_safe_flag == "PIT_SAFE" for ev in evaluated.evidence)
    assert any(plan.action == "BUY" and plan.target_quantity > 0 for plan in evaluated.order_plans)
    by_id = {row.id: row for row in rows}
    for ev in evaluated.evidence:
        alpha = by_id[ev.score_id].model_alpha_score
        assert ev.score_value == alpha
        if alpha >= 70 and ev.action == "BUY":
            assert "MODEL_ALPHA_GE_70" in ev.reason_codes
        elif 35 < alpha < 70:
            assert ev.action == "HOLD"
