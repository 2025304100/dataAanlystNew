"""Real score provenance is required before a model can produce orders."""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio, PortfolioRule
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.decision_engine import (
    DecisionEngine, LoadedSnapshot, ScoredUniverse, _query_scores_for_universe,
    _real_signal_generator,
)
from app.services.score_query_service import estimate_score_coverage, fetch_latest_pit_scores

pytestmark = pytest.mark.whitebox
DAY = date(2026, 9, 2)
CUTOFF = datetime(2026, 9, 2, 7)


def _binding(db):
    portfolio = Portfolio(name="factor-real-chain", account_type="simulated", total_capital=100000,
        investable_ratio=.9, cash_reserve_ratio=.1, currency="CNY", auto_trade_enabled=1)
    symbol = Symbol(symbol="600391", name="Factor chain", market="SH", asset_type="stock")
    db.add_all([portfolio, symbol])
    db.flush()
    from tests.test_stage3_factor_binding_contract import _ready_model
    _ready_model(db, set_id="real-set", model_id="real-model")
    snapshot = StrategyExecutionSnapshot(id="factor-execution-snapshot", portfolio_id=portfolio.id,
        snapshot_no=1, factor_model_run_id="real-model", factor_set_id="real-set", snapshot_type="save_and_apply",
        snapshot_hash="chain", effective_from=datetime(2026, 9, 1), decision_clock_json="{}",
        member_snapshot_json=json.dumps([{"symbol_id": symbol.id}]),
        versions_json=json.dumps({"run_mode": "production_pit", "pit_mode": "strict_pit_safe",
            "factor_member_versions": {"factor_real-set": 1},
            "signal_policy": {"score_source": "model_alpha", "score_scale": 100, "buy_threshold": 70, "sell_threshold": 35}}))
    db.add(snapshot)
    db.flush()
    return portfolio, symbol, snapshot


def _score(db, symbol, *, alpha=85, quality=20, input_cutoff=CUTOFF,
           set_id="real-set", published=datetime(2026, 9, 2, 6), batch="good", versions=None):
    row = Score(symbol_id=symbol.id, trade_date=DAY, quality_score=quality, timing_score=50,
        quality_grade="B", stage="growth", action="exit", priority_score=50,
        factor_model_run_id="real-model", factor_set_id=set_id, model_alpha_score=alpha,
        factor_member_versions_json=json.dumps(versions if versions is not None else {"factor_real-set": {"version": 1}}),
        weight_mode="ridge", factor_data_cutoff_at=input_cutoff, published_at=published, calc_batch_id=batch)
    db.add(row)
    db.flush()
    return row


def test_default_50_points_hold_and_real_alpha_changes_direction():
    manual = LoadedSnapshot(snapshot=SimpleNamespace(), factor_model_run_id="", factor_set_id=None,
        rule_id=None, rule_version=None)
    score = {"symbol_id": 1, "quality_score": 50, "recommended_action": "exit"}
    signal = _real_signal_generator(None, manual, ScoredUniverse(items=[score]))
    assert signal.items[0]["direction"] == "HOLD"
    model = LoadedSnapshot(snapshot=SimpleNamespace(), factor_model_run_id="model", factor_set_id="set",
        rule_id=None, rule_version=None)
    high = {"symbol_id": 1, "quality_score": 20, "model_alpha_score": 85}
    low = {"symbol_id": 2, "quality_score": 95, "model_alpha_score": 15}
    signals = _real_signal_generator(None, model, ScoredUniverse(items=[high, low]))
    assert [s["direction"] for s in signals.items] == ["BUY", "SELL"]
    assert [s["score_item"]["score_value"] for s in signals.items] == [85, 15]


@pytest.mark.parametrize("invalid,code", [
    ({"input_cutoff": datetime(2026, 10, 1)}, "SCORE_FACTOR_INPUT_AFTER_CUTOFF"),
    ({"input_cutoff": None}, "SCORE_FACTOR_INPUT_CUTOFF_MISSING"),
    ({"set_id": "different-set"}, "SCORE_FACTOR_SET_MISMATCH"),
    ({"alpha": None}, "SCORE_MODEL_ALPHA_MISSING"),
    ({"published": datetime(2026, 10, 1)}, "SCORE_PUBLICATION_AFTER_CUTOFF"),
    ({"versions": {"factor_real-set": 2}}, "SCORE_FACTOR_VERSION_MISMATCH"),
    ({"versions": {}}, "SCORE_FACTOR_VERSION_MISMATCH"),
])
def test_bad_lineage_is_retained_in_evidence_but_never_orders(db_session, invalid, code):
    portfolio, symbol, snapshot = _binding(db_session)
    row = _score(db_session, symbol, **invalid)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=DAY, run_type="backtest", dry_run=True)
    assert result.score_coverage_pct == 0
    assert result.evidence[0].score_id == row.id
    assert result.evidence[0].pit_safe_flag == "NOT_PIT_SAFE"
    assert code in result.evidence[0].reason_codes
    assert all(p.action not in {"BUY", "SELL"} for p in result.order_plans)


def test_older_complete_score_beats_newer_future_input_revision(db_session):
    _, symbol, _ = _binding(db_session)
    good = _score(db_session, symbol)
    _score(db_session, symbol, alpha=99, input_cutoff=datetime(2026, 10, 1), batch="future")
    rows = _query_scores_for_universe(db_session, factor_model_run_id="real-model", factor_set_id="real-set",
        trade_date=DAY, cutoff_utc=CUTOFF, symbol_ids=[symbol.id], require_factor_input=True)
    assert rows[symbol.id]["score_id"] == good.id
    strict = fetch_latest_pit_scores(db_session, factor_model_run_id="real-model", factor_set_id="real-set",
        symbol_ids=[symbol.id], decision_date=DAY, data_cutoff_at=CUTOFF, require_published_at=True)
    assert [r.score_id for r in strict] == [good.id]
    assert estimate_score_coverage(db_session, factor_model_run_id="real-model", factor_set_id="real-set",
        symbol_ids=[symbol.id], decision_date=DAY, data_cutoff_at=CUTOFF, require_published_at=True) == (1, 1, 0)


def test_recent_backfill_does_not_replace_newer_visible_trade_date(db_session):
    _, symbol, _ = _binding(db_session)
    current = _score(db_session, symbol, alpha=50)
    older = _score(db_session, symbol, alpha=99, batch="old-backfill")
    older.trade_date = date(2026, 9, 1)
    db_session.flush()
    rows = _query_scores_for_universe(db_session, factor_model_run_id="real-model", factor_set_id="real-set",
        trade_date=DAY, cutoff_utc=CUTOFF, symbol_ids=[symbol.id], require_factor_input=True)
    assert rows[symbol.id]["score_id"] == current.id


def test_strict_scorer_adapter_preserves_real_alpha_and_exact_factor_versions(db_session):
    from app.services.decision_engine import _stub_snapshot_loader, _stub_universe_builder
    from app.services.score_query_service import make_decision_scorer
    _, symbol, snapshot = _binding(db_session)
    row = _score(db_session, symbol, alpha=85, quality=20)
    loaded = _stub_snapshot_loader(db_session, snapshot.id)
    universe = _stub_universe_builder(db_session, loaded, CUTOFF)
    scored = make_decision_scorer(require_published_at=True)(db_session, loaded, universe, CUTOFF, DAY)
    assert scored.items[0]["score_id"] == row.id
    assert scored.items[0]["score_input_issues"] == []
    assert scored.items[0]["factor_member_versions_json"] == row.factor_member_versions_json
    signals = _real_signal_generator(db_session, loaded, scored)
    assert signals.items[0]["direction"] == "BUY"
    assert signals.items[0]["score_item"]["score_value"] == 85


def test_strict_query_and_coverage_select_older_correct_factor_version(db_session):
    _, symbol, _ = _binding(db_session)
    good = _score(db_session, symbol)
    _score(db_session, symbol, alpha=99, versions={"factor_real-set": 2}, batch="different-version")
    kwargs = dict(factor_model_run_id="real-model", factor_set_id="real-set",
        symbol_ids=[symbol.id], decision_date=DAY, data_cutoff_at=CUTOFF,
        require_published_at=True, factor_member_versions={"factor_real-set": 1})
    assert [r.score_id for r in fetch_latest_pit_scores(db_session, **kwargs)] == [good.id]
    assert estimate_score_coverage(db_session, **kwargs) == (1, 1, 0)


def test_explicit_manual_query_never_selects_a_model_score(db_session):
    _, symbol, _ = _binding(db_session)
    _score(db_session, symbol)
    assert _query_scores_for_universe(db_session, factor_model_run_id=None, trade_date=DAY,
        cutoff_utc=CUTOFF, symbol_ids=[symbol.id]) == {}


def test_snapshot_model_wins_over_current_rule_and_runtime(db_session, monkeypatch):
    from app.services import portfolio_backtest as service
    from app.models.backtest import BacktestRun
    portfolio, symbol, snapshot = _binding(db_session)
    rule = PortfolioRule(portfolio_id=portfolio.id, rule_name="new-unapplied-rule",
        is_active=1, max_single_position_pct=.1, max_open_positions=5,
        max_sector_position_pct=.3, max_stock_position_pct=.8, max_etf_position_pct=.2, max_loss_per_trade_pct=.05,
        stage_limits_json=json.dumps({"factor_model_run_id": "new-model", "factor_set_id": "new-set"}))
    db_session.add(rule)
    db_session.flush()
    observed = {}
    monkeypatch.setattr(service, "_resolve_symbol_ids", lambda *a, **k: ([symbol.id], "legacy"))
    monkeypatch.setattr(service, "_filter_symbol_ids_by_rule", lambda *a, **k: pytest.fail("snapshot replay cannot filter with window-end scores"))
    def fake_run(**kwargs):
        observed.update(kwargs)
        run = BacktestRun(portfolio_id=portfolio.id, run_name="locked", start_date=DAY, end_date=DAY,
            initial_capital=100000, symbols_json="[]", rule_config_json="{}", cost_config_json="{}",
            status="completed", strategy_snapshot_id=snapshot.id)
        db_session.add(run)
        db_session.flush()
        return run
    monkeypatch.setattr(service, "run_backtest", fake_run)
    service.run_portfolio_backtest(db_session, portfolio_id=portfolio.id, start_date=DAY, end_date=DAY,
        strategy_snapshot_id=snapshot.id, pit_mode="production_pit", price_type="NEXT_OPEN")
    assert observed["factor_model_run_id"] == "real-model"
    assert observed["factor_set_id"] == "real-set"
    assert observed["strategy_snapshot_id"] == snapshot.id
    assert observed["rule_config"].get("stage_limits", {}).get("factor_model_run_id") != "new-model"


def test_formal_backtest_cannot_promote_a_research_snapshot(db_session):
    from app.services.backtest import run_backtest
    portfolio, symbol, snapshot = _binding(db_session)
    snapshot.versions_json = json.dumps({"run_mode": "research", "pit_mode": "best_effort"})
    db_session.flush()
    with pytest.raises(ValueError, match="cannot promote"):
        run_backtest(db_session, portfolio_id=portfolio.id, symbol_ids=[symbol.id], start_date=DAY, end_date=DAY,
            rule_config={}, strategy_snapshot_id=snapshot.id, pit_mode="production_pit")


def test_old_quality_policy_decision_is_auditable_but_not_reused(db_session, monkeypatch):
    from app.models.daily_bar import DailyBar
    from app.models.backtest import BacktestTrade
    from app.models.decision_engine import DecisionRun
    from app.services import backtest as service
    portfolio, symbol, snapshot = _binding(db_session)
    snapshot.versions_json = json.dumps({"run_mode": "research", "pit_mode": "best_effort",
        "factor_member_versions": {"factor_real-set": 1}})
    _score(db_session, symbol, quality=90, alpha=50)
    days = [DAY, date(2026, 9, 3)]
    for day in days:
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=10, high=10.1, low=9.9,
            close=10, volume=1000000, amount=10000000))
    legacy = DecisionRun(id="legacy-quality-policy", portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        run_type="backtest", status="SUCCEEDED", trade_date=DAY, decision_at=CUTOFF,
        data_cutoff_at=CUTOFF, execution_at=datetime(2026, 9, 3, 1, 30), versions_json="{}")
    db_session.add(legacy)
    db_session.flush()
    monkeypatch.setattr(service, "_rehydrate_persisted_order_plans", lambda *a, **k: pytest.fail("old incorrect BUY cache was replayed"))
    run = service.run_backtest(db_session, portfolio_id=portfolio.id, symbol_ids=[symbol.id], start_date=days[0], end_date=days[-1],
        rule_config={}, strategy_snapshot_id=snapshot.id)
    assert legacy.id not in json.loads(run.decision_run_ids_json)
    assert db_session.get(DecisionRun, legacy.id).status == "SUCCEEDED"
    assert db_session.execute(select(BacktestTrade).where(BacktestTrade.run_id == run.id)).scalars().all() == []


def test_model_rank_allocates_limited_open_slots_to_stronger_alpha(db_session, monkeypatch):
    from app.services import decision_engine as service
    portfolio, low, snapshot = _binding(db_session)
    high = Symbol(symbol="600392", name="Stronger alpha", market="SH", asset_type="stock")
    db_session.add(high)
    db_session.flush()
    snapshot.member_snapshot_json = json.dumps([{"symbol_id": low.id}, {"symbol_id": high.id}])
    _score(db_session, low, quality=99, alpha=75, batch="low")
    _score(db_session, high, quality=10, alpha=95, batch="high")
    loaded = service._stub_snapshot_loader(db_session, snapshot.id)
    loaded.price_hints = {low.id: 10, high.id: 10}
    loaded.allocation_state_override = {"total_capital": 100000, "available_cash": 100000,
        "total_position_pct": 0, "current_by_sym": {}, "current_asset_pct": {}, "current_sector_pct": {}}
    constraints = service._get_rule_constraints
    def one_slot(snap, db):
        value = constraints(snap, db)
        value["open_slot_max"] = 1
        return value
    monkeypatch.setattr(service, "_get_rule_constraints", one_slot)
    universe = service._stub_universe_builder(db_session, loaded, CUTOFF)
    scored = service._real_scorer(db_session, loaded, universe, CUTOFF, DAY)
    signal = service._real_signal_generator(db_session, loaded, scored)
    allocated = service._real_allocator(db_session, loaded, signal, CUTOFF)
    by_symbol = {item["symbol_id"]: item for item in allocated.items}
    assert high.id > low.id  # The old ID tie-break would have picked low.
    assert by_symbol[high.id]["target_quantity"] > 0
    assert by_symbol[low.id]["target_quantity"] == 0


def test_new_nightly_snapshot_accepts_real_post_close_score_but_keeps_legacy_clock(db_session):
    from app.services.decision_engine import _resolve_snapshot_clock, _stub_snapshot_loader
    portfolio, symbol, snapshot = _binding(db_session)
    _score(db_session, symbol, published=datetime(2026, 9, 2, 11), input_cutoff=datetime(2026, 9, 2, 10))
    engine = DecisionEngine()
    old = engine.evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="backtest", dry_run=True)
    assert old.score_coverage_pct == 0
    versions = json.loads(snapshot.versions_json)
    versions["decision_clock_policy"] = {"timezone": "Asia/Shanghai", "decision_time": "20:30",
        "cutoff_time": "20:00", "execution_time": "09:30", "execution_offset_days": 1}
    snapshot.versions_json = json.dumps(versions)
    db_session.flush()
    nightly = engine.evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="backtest", dry_run=True)
    assert nightly.score_coverage_pct == 100
    assert nightly.clock.data_cutoff_at == datetime(2026, 9, 2, 12)
    assert nightly.clock.decision_at == datetime(2026, 9, 2, 12, 30)
    assert nightly.evidence[0].pit_safe_flag == "PIT_SAFE"
    # Changing a score's publication to after the frozen cutoff must reject it.
    row = db_session.execute(select(Score).where(Score.symbol_id == symbol.id)).scalar_one()
    row.published_at = datetime(2026, 9, 2, 12, 1)
    db_session.flush()
    late = engine.evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="backtest", dry_run=True)
    assert late.score_coverage_pct == 0
    assert "SCORE_PUBLICATION_AFTER_CUTOFF" in late.evidence[0].reason_codes


def test_frozen_constraints_keep_old_snapshot_allocation_when_live_rule_changes(db_session):
    from app.services import decision_engine as service
    portfolio, symbol, snapshot = _binding(db_session)
    _score(db_session, symbol)
    loaded = service._stub_snapshot_loader(db_session, snapshot.id)
    frozen = service._get_rule_constraints(loaded, db_session)
    frozen["single_limit_pct"] = .03
    loaded.versions["rule_constraints"] = frozen
    loaded.price_hints = {symbol.id: 10}
    loaded.allocation_state_override = {"total_capital": 100000, "available_cash": 100000,
        "total_position_pct": 0, "current_by_sym": {}, "current_asset_pct": {}, "current_sector_pct": {}}
    live_rule = PortfolioRule(portfolio_id=portfolio.id, rule_name="changed-rule", is_active=1,
        max_single_position_pct=.01, max_sector_position_pct=.3, max_stock_position_pct=.9,
        max_etf_position_pct=.2, max_loss_per_trade_pct=.05, max_open_positions=5, stage_limits_json="{}")
    db_session.add(live_rule)
    db_session.flush()
    loaded.rule_id = live_rule.id
    portfolio.investable_ratio = .01
    universe = service._stub_universe_builder(db_session, loaded, CUTOFF)
    scored = service._real_scorer(db_session, loaded, universe, CUTOFF, DAY)
    signals = service._real_signal_generator(db_session, loaded, scored)
    allocated = service._real_allocator(db_session, loaded, signals, CUTOFF)
    assert allocated.items[0]["target_quantity"] == 300
    assert allocated.items[0]["target_position_pct"] == .03


def _risk_snapshot(db):
    portfolio, symbol, snapshot = _binding(db)
    versions = json.loads(snapshot.versions_json)
    versions["risk_policy"] = {"stop_loss_basis": "average_cost", "stop_loss_pct": .05,
        "drawdown_circuit_pct": .10, "drawdown_action": "block_new_buys"}
    snapshot.versions_json = json.dumps(versions)
    db.flush()
    return portfolio, symbol, snapshot


def _risk_state(symbol, *, quantity=0, close=10, equity=100000, peak=100000):
    from app.services.decision_engine import DecisionStateContext
    return DecisionStateContext(current_by_symbol={symbol.id: {"qty": quantity, "average_cost": 10,
        "pct": quantity * close / 100000, "market_value": quantity * close, "asset_type": "stock", "sector": "unclassified"}},
        available_cash=100000 - quantity * 10, total_capital=100000, equity_value=equity, equity_high_watermark=peak,
        price_data_by_symbol={symbol.id: {"open_price": 10, "close_price": close, "high_price": 10,
            "low_price": close, "volume": 1000000, "available_at": datetime(2026, 9, 2, 6)}})


@pytest.mark.parametrize("close,triggered", [(9.5, True), (9.6, False)])
def test_average_cost_price_stop_uses_visible_close_and_keeps_verified_fill_separate(db_session, close, triggered):
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    _score(db_session, symbol, alpha=50)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="backtest", dry_run=True, state_context=_risk_state(symbol, quantity=100, close=close))
    ev = result.evidence[0]
    assert ev.stop_loss_triggered is triggered
    if triggered:
        assert ev.action == "SELL"
        assert ev.action_subtype == "SELL_STOP_LOSS"
        assert ev.target_qty_delta == -100
        assert ev.stop_loss_verified_price_source == "DECISION_CLOSE"
        assert ev.executed_price is None
        assert result.order_plans[0].execution_date == date(2026, 9, 3)
    else:
        assert ev.action == "HOLD"


@pytest.mark.parametrize("alpha,quantity,expected", [(90, 0, "REJECTED"), (20, 100, "SELL")])
def test_drawdown_circuit_only_blocks_new_buy_and_retains_sell(db_session, alpha, quantity, expected):
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    _score(db_session, symbol, alpha=alpha)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="backtest", dry_run=True, state_context=_risk_state(symbol, quantity=quantity, equity=90000, peak=100000))
    assert result.evidence[0].action == expected
    if expected == "REJECTED":
        assert result.evidence[0].rejection_reason == "DRAWDOWN_CIRCUIT_TRIGGERED"
        assert result.order_plans[0].target_quantity == 0
    else:
        assert result.order_plans[0].target_quantity == 100


def test_future_equity_peak_does_not_change_current_drawdown(db_session):
    from app.models.daily_bar import DailyBar
    from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
    from app.services.auto_trade_member_source import _build_snapshot_decision_state_context
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    db_session.add_all([
        DailyBar(symbol_id=symbol.id, trade_date=DAY, open=10, high=10, low=10, close=10, volume=100000,
            created_at=datetime(2026, 9, 2, 6)),
        PortfolioEquitySnapshot(portfolio_id=portfolio.id, snapshot_date=date(2026, 9, 1), total_equity=100000,
            created_at=datetime(2026, 9, 1, 6)),
        PortfolioEquitySnapshot(portfolio_id=portfolio.id, snapshot_date=DAY, total_equity=200000,
            created_at=datetime(2026, 9, 2, 8)),
        PortfolioEquitySnapshot(portfolio_id=portfolio.id, snapshot_date=date(2026, 9, 3), total_equity=300000,
            created_at=datetime(2026, 9, 2, 6)),
    ])
    db_session.flush()
    state = _build_snapshot_decision_state_context(db_session, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=DAY, dry_run=True)
    assert state.equity_value == 100000
    assert state.equity_high_watermark == 100000


def test_model_acceptance_lock_keeps_average_cost_exit_under_drawdown(db_session):
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    portfolio.status_model = "MODEL_INACTIVE"
    _score(db_session, symbol, alpha=90)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="auto_simulation", dry_run=False, governance_exit_only=True,
        state_context=_risk_state(symbol, quantity=100, close=9.5, equity=90000, peak=100000))
    assert any(reason["code"] == "MODEL_INACTIVE" for reason in result.blocking_reasons)
    assert result.evidence[0].action == "SELL"
    assert result.evidence[0].action_subtype == "SELL_STOP_LOSS"
    assert result.order_plans[0].target_quantity == 100


def test_snapshot_position_exit_authority_does_not_allow_alpha_entry(db_session):
    portfolio, symbol, snapshot = _binding(db_session)
    snapshot.member_snapshot_json = json.dumps([{"symbol_id": symbol.id, "execution_mode": "auto", "entry_authorized": False}])
    _score(db_session, symbol, alpha=90)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="backtest", dry_run=True, state_context=_risk_state(symbol, quantity=100))
    assert result.evidence[0].action != "BUY"
    assert result.evidence[0].target_qty_delta == 0


@pytest.mark.parametrize("close,action", [(9.4, "SELL"), (10, "HOLD")])
def test_post_application_orphan_holding_is_exit_only_without_changing_snapshot(db_session, close, action):
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    snapshot.member_snapshot_json = "[]"
    _score(db_session, symbol, alpha=90)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="auto_simulation", dry_run=True,
        state_context=_risk_state(symbol, quantity=100, close=close))
    assert len(result.evidence) == 1
    assert result.evidence[0].action == action
    assert result.evidence[0].target_qty_delta == (-100 if action == "SELL" else 0)
    assert snapshot.member_snapshot_json == "[]"


def test_state_holding_does_not_override_frozen_manual_member_authorization(db_session):
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    snapshot.member_snapshot_json = json.dumps([{"symbol_id": symbol.id, "execution_mode": "manual"}])
    _score(db_session, symbol, alpha=90)
    result = DecisionEngine().evaluate(db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=DAY, run_type="auto_simulation", dry_run=True,
        state_context=_risk_state(symbol, quantity=100, close=9.4))
    assert result.evidence == []


def test_formal_backtest_average_cost_stop_decides_on_close_and_fills_next_open(db_session):
    from app.models.backtest import BacktestExecutionFill, BacktestTrade
    from app.models.daily_bar import DailyBar
    from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
    from app.services.backtest import run_backtest
    portfolio, symbol, snapshot = _risk_snapshot(db_session)
    snapshot.cost_config_json = json.dumps({"slippage_buy_bps": 0, "slippage_sell_bps": 0})
    days = [DAY, date(2026, 9, 3), date(2026, 9, 4)]
    for day, opening, close, alpha in zip(days, [10, 10, 9.2], [10, 9.4, 9.3], [90, 50, 50]):
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=opening, high=max(opening, close) + .1,
            low=min(opening, close) - .1, close=close, volume=1000000, amount=10000000,
            created_at=datetime.combine(day, datetime.min.time())))
        db_session.add(Score(symbol_id=symbol.id, trade_date=day, quality_score=50, quality_grade="C",
            timing_score=50, priority_score=50, stage="neutral", action="hold", weight_mode="ridge",
            factor_model_run_id="real-model", factor_set_id="real-set", model_alpha_score=alpha,
            factor_member_versions_json=json.dumps({"factor_real-set": 1}), calc_batch_id=f"risk-{day}",
            factor_data_cutoff_at=datetime.combine(day, datetime.min.time()),
            published_at=datetime.combine(day, datetime.min.time())))
    # A replay must not borrow an unrelated live account peak, even when it
    # is earlier than the replay's cutoff.
    db_session.add(PortfolioEquitySnapshot(portfolio_id=portfolio.id, snapshot_date=date(2026, 9, 1),
        total_equity=1000000, created_at=datetime(2026, 9, 1)))
    db_session.commit()
    run = run_backtest(db_session, portfolio.id, [symbol.id], days[0], days[-1], {},
        initial_capital=100000, strategy_snapshot_id=snapshot.id, pit_mode="production_pit",
        cost_config={"slippage_buy_bps": 0, "slippage_sell_bps": 0})
    trade = db_session.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run.id)).one()
    assert trade.entry_date == days[1] and trade.entry_price == 10
    assert trade.exit_date == days[2] and trade.exit_price == 9.2
    from app.models.decision_engine import DecisionEvidence
    exit_evidence = db_session.get(DecisionEvidence, trade.exit_evidence_id)
    assert exit_evidence.stop_loss_triggered == 1
    assert exit_evidence.stop_loss_verified_price_source == "DECISION_CLOSE"
    assert exit_evidence.executed_price == 9.2
    fills = db_session.scalars(select(BacktestExecutionFill).where(BacktestExecutionFill.run_id == run.id)
        .order_by(BacktestExecutionFill.execution_date)).all()
    assert [fill.side for fill in fills] == ["BUY", "SELL"]
    assert fills[-1].executed_price == 9.2
