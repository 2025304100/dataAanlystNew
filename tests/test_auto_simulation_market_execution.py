"""Market execution behavior through the real automatic-simulation entry."""
from datetime import date, datetime
import json

import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.decision_engine import DecisionEvidence, DecisionOrderPlanRecord, DecisionRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import execute_member_source
from app.services.sim_accounts import build_sim_account_summary, recent_sim_trades
from tests.test_whitebox_portfolio_confirmation import binding, _legacy_state

pytestmark = pytest.mark.whitebox
SIGNAL_DATE = date(2026, 9, 2)
EXECUTION_DATE = date(2026, 9, 3)


@pytest.mark.parametrize("symbol_code,board,execution_open,execution_time", [
    ("600981", "main", 10.1, "09:30"),
    ("300981", "chinext", 11.5, "09:30"),
    ("600981", "main", 10.1, "10:00"),
])
def test_missing_execution_bar_waits_without_simulated_cash_or_fills(
    binding, monkeypatch, symbol_code, board, execution_open, execution_time,
):
    db, portfolio, model, _, snapshot = binding
    portfolio.account_type = "simulated"
    portfolio.status_model = "READY"
    portfolio.auto_trade_enabled = 1
    _legacy_state(db, portfolio, "READY")
    symbol = Symbol(symbol=symbol_code, board=board, name="Await execution",
        market="SZ" if board == "chinext" else "SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    snapshot.member_snapshot_json = json.dumps([
        {"symbol_id": symbol.id, "execution_mode": "auto", "asset_type": "stock"},
    ])
    snapshot.cost_config_json = json.dumps({"slippage_buy_bps": 0})
    snapshot.versions_json = json.dumps({"decision_clock_policy": {
        "timezone": "Asia/Shanghai", "decision_time": "15:05", "cutoff_time": "15:00",
        "execution_time": execution_time, "execution_offset_days": 1,
    }})
    db.add_all([
        DailyBar(symbol_id=symbol.id, trade_date=SIGNAL_DATE,
            open=10, high=10.2, low=9.8, close=10, volume=1000000,
            created_at=datetime(2026, 9, 2, 6)),
        Score(symbol_id=symbol.id, trade_date=SIGNAL_DATE,
            quality_score=90, quality_grade="A", timing_score=90, stage="growth",
            action="open", priority_score=90, model_alpha_score=90,
            weight_mode="ridge", factor_model_run_id=model.id,
            factor_set_id=snapshot.factor_set_id,
            factor_data_cutoff_at=datetime(2026, 9, 2, 6),
            published_at=datetime(2026, 9, 2, 6)),
    ])
    db.commit()

    result = execute_member_source(db, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=SIGNAL_DATE, dry_run=False)

    assert result["errors"] == []
    assert result["executed_orders"] == []
    assert result["pending_orders"][0]["reason"] == "EXECUTION_BAR_MISSING"
    assert recent_sim_trades(db, portfolio.id) == []
    account = build_sim_account_summary(db, portfolio, update_positions=False)
    assert account["cash_balance"] == portfolio.total_capital

    # The same saved intent can execute once a real next-session bar arrives.
    limit_up = 12.0 if board == "chinext" else 11.0
    execution_bar = DailyBar(symbol_id=symbol.id, trade_date=EXECUTION_DATE,
        open=limit_up, high=limit_up, low=limit_up, close=limit_up, volume=1000000,
        created_at=datetime(2026, 9, 3, 1, 30))
    db.add(execution_bar)
    db.commit()
    locked = execute_member_source(db, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=SIGNAL_DATE, dry_run=False)
    assert locked["executed_orders"] == []
    assert locked["pending_orders"][0]["reason"] == "PRICE_LIMIT_LOCKED_BUY_SIDE"
    execution_bar.open = execution_bar.close = execution_open
    execution_bar.high = execution_open + 0.1
    execution_bar.low = execution_open - 0.1
    db.commit()
    monkeypatch.setattr("app.services.auto_trade_member_source.utcnow_naive",
        lambda: datetime(2026, 9, 3, 1, 45 if execution_time == "10:00" else 29))
    early = execute_member_source(db, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=SIGNAL_DATE, dry_run=False)
    assert early["executed_orders"] == []
    assert early["pending_orders"][0]["reason"] == "EXECUTION_TIME_NOT_REACHED"
    monkeypatch.setattr("app.services.auto_trade_member_source.utcnow_naive",
        lambda: datetime(2026, 9, 3, 2, 0) if execution_time == "10:00" else datetime(2026, 9, 3, 1, 30))
    completed = execute_member_source(db, portfolio_id=portfolio.id,
        strategy_snapshot_id=snapshot.id, trade_date=SIGNAL_DATE, dry_run=False)
    assert len(completed["executed_orders"]) == 1, completed
    trades = recent_sim_trades(db, portfolio.id)
    assert len(trades) == 1
    assert trades[0]["price"] == execution_open
    # The 5,000-yuan signal-day budget / known close 10 fixes 500 shares.
    # Receiving a later opening price must not rewrite that saved quantity.
    assert trades[0]["quantity"] == 500


@pytest.mark.parametrize("ledger_state,old_marker,old_status", [
    ("none", None, "SUCCEEDED"),
    ("none", "next-open-v0", "FAILED"),
    ("filled", None, "SUCCEEDED"),
    ("filled", "next-open-v0", "SUCCEEDED"),
    ("partial", None, "SUCCEEDED"),
    ("partial", "next-open-v0", "SUCCEEDED"),
])
def test_pre_fix_auto_decisions_require_new_snapshot_without_reissuing_ledger(
    binding, ledger_state, old_marker, old_status,
):
    db, portfolio, model, _, snapshot = binding
    portfolio.account_type = "simulated"
    portfolio.status_model = "READY"
    portfolio.auto_trade_enabled = 1
    _legacy_state(db, portfolio, "READY")
    symbol = Symbol(symbol="600982", name="Legacy price policy", market="SH", asset_type="stock")
    db.add(symbol)
    db.flush()
    snapshot.member_snapshot_json = json.dumps([
        {"symbol_id": symbol.id, "execution_mode": "auto", "asset_type": "stock"},
    ])
    snapshot.cost_config_json = json.dumps({"slippage_buy_bps": 0, "volume_limit_pct": 0.1})
    db.add_all([
        DailyBar(symbol_id=symbol.id, trade_date=SIGNAL_DATE, open=10, high=10.2,
            low=9.8, close=10, volume=1000000, created_at=datetime(2026, 9, 2, 6)),
        Score(symbol_id=symbol.id, trade_date=SIGNAL_DATE, quality_score=90,
            quality_grade="A", timing_score=90, stage="growth", action="open",
            priority_score=90, model_alpha_score=90, weight_mode="ridge",
            factor_model_run_id=model.id, factor_set_id=snapshot.factor_set_id,
            factor_data_cutoff_at=datetime(2026, 9, 2, 6), published_at=datetime(2026, 9, 2, 6)),
    ])
    if ledger_state != "none":
        db.add(DailyBar(symbol_id=symbol.id, trade_date=EXECUTION_DATE,
            open=10.5, high=10.6, low=10.4, close=10.5,
            volume=1000 if ledger_state == "partial" else 1000000,
            created_at=datetime(2026, 9, 3, 1, 30)))
    db.commit()
    arguments = dict(portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=SIGNAL_DATE, dry_run=False)
    original = execute_member_source(db, **arguments)
    assert original["errors"] == []
    if ledger_state != "none":
        assert original["executed_orders"][0]["status"] == ledger_state

    # Arrange an immutable pre-upgrade run. For the unexecuted case its old
    # opening-price quantity differs from today's corrected close-price plan.
    old_run = db.get(DecisionRun, original["decision_run_id"])
    versions = json.loads(old_run.versions_json)
    versions.pop("price_reference_policy_version", None)
    if old_marker is not None:
        versions["price_reference_policy_version"] = old_marker
    old_run.versions_json = json.dumps(versions)
    old_run.status = old_status
    if ledger_state == "none":
        for evidence in db.scalars(select(DecisionEvidence).where(DecisionEvidence.decision_run_id == old_run.id)):
            evidence.target_quantity = evidence.target_qty_delta = 400
        for plan in db.scalars(select(DecisionOrderPlanRecord).where(DecisionOrderPlanRecord.decision_run_id == old_run.id)):
            plan.target_quantity = 400
        db.add(DailyBar(symbol_id=symbol.id, trade_date=EXECUTION_DATE,
            open=10.5, high=10.6, low=10.4, close=10.5, volume=1000000,
            created_at=datetime(2026, 9, 3, 1, 30)))
    if ledger_state == "partial":
        # A later scheduler day must not bypass the old partial plan guard.
        db.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026, 9, 4),
            open=10.5, high=10.6, low=10.4, close=10.5, volume=1000,
            created_at=datetime(2026, 9, 4, 1, 30)))
        arguments["trade_date"] = EXECUTION_DATE
    db.commit()
    before_trades = recent_sim_trades(db, portfolio.id)
    before_cash = build_sim_account_summary(db, portfolio, update_positions=False)["cash_balance"]

    result = execute_member_source(db, **arguments)

    assert result.get("blocked") is True, result
    assert result["rejected_decisions"][0]["rejection_code"] == "PRICE_REFERENCE_POLICY_REAPPLY_REQUIRED"
    assert "保存" in result["rejected_decisions"][0]["rejection_detail"]
    assert result["executed_orders"] == []
    assert recent_sim_trades(db, portfolio.id) == before_trades
    assert build_sim_account_summary(db, portfolio, update_positions=False)["cash_balance"] == before_cash
    db.refresh(old_run)
    assert old_run.status == old_status
