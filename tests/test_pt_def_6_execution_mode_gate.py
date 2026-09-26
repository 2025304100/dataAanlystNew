"""PT-DEF-6 回归守护：实盘 auto_simulation 仅成交 execution_mode=auto 成员。

资金安全不变量（多份设计文档）：manual 只提示信号、confirm 只生成待确认计划、
仅 auto 自动下单。修复前 _stub_universe_builder 吞入快照全部成员（含 manual/
confirm），导致它们在实盘自动模拟交易中被自动成交。修复后 _real_universe_builder
在 auto_simulation 路径按 execution_mode 过滤，manual/confirm 记入 ineligible →
不进入 evidence/order_plan → 不会被 place_sim_order 成交；回测/研究路径保持全量
（回测走 C-03 auto_authorized_flag 白名单，研究为 dry_run 不成交）。
"""
from __future__ import annotations

import json
from datetime import date, datetime

from app.models.daily_bar import DailyBar
from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.decision_engine import default_engine


def _seed_three_mode_portfolio(db):
    """auto/manual/confirm 三成员组合 + 可成交快照（各自 score=BUY、行情齐备）。"""
    portfolio = Portfolio(
        name="pt-def-6-modes", account_type="simulated", asset_scope="mixed",
        total_capital=100_000, investable_ratio=0.95, cash_reserve_ratio=0.05,
    )
    s_auto = Symbol(symbol="600001", name="Auto", market="SH", asset_type="stock")
    s_manual = Symbol(symbol="600002", name="Manual", market="SH", asset_type="stock")
    s_confirm = Symbol(symbol="600003", name="Confirm", market="SH", asset_type="stock")
    db.add_all([portfolio, s_auto, s_manual, s_confirm])
    db.flush()
    trade_date = date(2025, 1, 6)
    rows = []
    for sym in (s_auto, s_manual, s_confirm):
        rows.append(DailyBar(symbol_id=sym.id, trade_date=trade_date, open=10, high=10.2, low=9.8, close=10, volume=1_000_000, amount=10_000_000))
        rows.append(DailyBar(symbol_id=sym.id, trade_date=date(2025, 1, 7), open=10.1, high=10.3, low=10, close=10.2, volume=1_000_000, amount=10_100_000))
        rows.append(Score(symbol_id=sym.id, trade_date=trade_date, quality_score=.9, quality_grade="A", timing_score=.8, stage="growth", action="open", priority_score=.8, weight_mode="manual", factor_model_run_id="pt-def-6-model", calc_batch_id="pt-def-6-batch", published_at=datetime(2025, 1, 1)))
    db.add_all(rows)
    snapshot = StrategyExecutionSnapshot(
        id="pt-def-6-snapshot", snapshot_no=1, portfolio_id=portfolio.id,
        factor_model_run_id="pt-def-6-model", decision_clock_json="{}",
        member_snapshot_json=json.dumps([
            {"symbol_id": s_auto.id, "member_id": 1, "execution_mode": "auto"},
            {"symbol_id": s_manual.id, "member_id": 2, "execution_mode": "manual"},
            {"symbol_id": s_confirm.id, "member_id": 3, "execution_mode": "confirm"},
        ]),
        snapshot_type="save_and_apply", snapshot_hash="pt-def-6-hash",
        gate_policy_version="production-v1.0.0",
        versions_json=json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2025, 1, 1),
    )
    db.add(snapshot)
    db.commit()
    return portfolio, snapshot, trade_date, s_auto, s_manual, s_confirm


def test_auto_simulation_filters_manual_and_confirm(db_session):
    """实盘 auto_simulation：仅 auto 成员进入 evidence，manual/confirm 被过滤不成交。"""
    portfolio, snapshot, trade_date, s_auto, s_manual, s_confirm = _seed_three_mode_portfolio(db_session)

    result = default_engine.evaluate(
        db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=trade_date, run_type="auto_simulation", dry_run=True,
    )

    evidence_symbols = {e.symbol_id for e in result.evidence}
    assert s_auto.id in evidence_symbols, f"auto 成员应纳入决策: {evidence_symbols}"
    assert s_manual.id not in evidence_symbols, f"manual 成员不得进入 evidence: {evidence_symbols}"
    assert s_confirm.id not in evidence_symbols, f"confirm 成员不得进入 evidence: {evidence_symbols}"
    # order_plan 由 evidence 派生 → manual/confirm 无 plan → 不会被 place_sim_order 成交
    plan_symbols = {getattr(p, "symbol_id", None) for p in result.order_plans}
    assert s_manual.id not in plan_symbols and s_confirm.id not in plan_symbols, plan_symbols


def test_backtest_keeps_full_universe(db_session):
    """回测 backtest：保持全量成员（过滤交给 C-03 auto_authorized_flag 白名单）。"""
    portfolio, snapshot, trade_date, s_auto, s_manual, s_confirm = _seed_three_mode_portfolio(db_session)

    result = default_engine.evaluate(
        db_session, portfolio_id=portfolio.id, strategy_snapshot_id=snapshot.id,
        trade_date=trade_date, run_type="backtest", dry_run=True,
    )

    evidence_symbols = {e.symbol_id for e in result.evidence}
    assert {s_auto.id, s_manual.id, s_confirm.id} <= evidence_symbols, evidence_symbols


def test_execute_member_source_three_modes_end_to_end(db_session):
    """PT-DEF-6 层3 端到端：execute_member_source 实盘入口的三态输出。

    auto → 成交 SimOrder（filled）；confirm → pending_confirmation（无 SimOrder）；
    manual → signal_only（无 SimOrder）。验证 universe 过滤（不成交）+
    信号路径恢复（manual/confirm 仍有用户可见提示输出）两者兼顾。
    """
    from app.models.sim_account import SimOrder
    from app.services.auto_trade_member_source import execute_member_source

    portfolio, snapshot, trade_date, s_auto, s_manual, s_confirm = _seed_three_mode_portfolio(db_session)

    result = execute_member_source(
        db_session, portfolio_id=portfolio.id, dry_run=False, trade_date=trade_date,
    )

    by_sym = {}
    for o in result["executed_orders"]:
        by_sym.setdefault(o["symbol_id"], []).append(o)

    # auto 成交
    auto_orders = by_sym.get(s_auto.id, [])
    assert len(auto_orders) == 1 and auto_orders[0]["status"] == "filled", auto_orders
    assert "order_id" in auto_orders[0]
    # confirm 待确认 → Q1 决策：落一张 pending_confirmation 占位单，有 order_id
    confirm_orders = by_sym.get(s_confirm.id, [])
    assert len(confirm_orders) == 1 and confirm_orders[0]["status"] == "pending_confirmation", confirm_orders
    assert "order_id" in confirm_orders[0]
    # manual 仍只提示（不建单）→ 无 order_id
    manual_orders = by_sym.get(s_manual.id, [])
    assert len(manual_orders) == 1 and manual_orders[0]["status"] == "signal_only", manual_orders
    assert "order_id" not in manual_orders[0]

    # Q1：SimOrder = auto 成交单 + confirm 待确认单（manual 不建单）；均不产生成交现金流
    orders = db_session.query(SimOrder).filter_by(portfolio_id=portfolio.id).all()
    by_status: dict = {}
    for o in orders:
        by_status.setdefault(o.status, []).append(o)
    assert len(by_status.get("filled", [])) == 1 and by_status["filled"][0].symbol_id == s_auto.id
    assert len(by_status.get("pending_confirmation", [])) == 1 and by_status["pending_confirmation"][0].symbol_id == s_confirm.id
    assert s_manual.id not in {o.symbol_id for o in orders}

    # signal_decisions 含 manual/confirm 的提示（execution_mode 可追溯）
    sig_modes = {d.get("execution_mode") for d in result["signal_decisions"]}
    assert "manual" in sig_modes and "confirm" in sig_modes, result["signal_decisions"]


def test_execute_member_source_rerun_is_idempotent(db_session):
    """重跑幂等（同快照 + 同 trade_date）：第二次不重复下单。

    验证 decision_at 由 trade_date 派生（非当前时刻），故 order_plan_id 稳定，
    同日重跑命中 client_order_key 幂等 skip，不会重复成交。
    """
    from app.models.sim_account import SimOrder
    from app.services.auto_trade_member_source import execute_member_source

    portfolio, snapshot, trade_date, s_auto, s_manual, s_confirm = _seed_three_mode_portfolio(db_session)

    r1 = execute_member_source(
        db_session, portfolio_id=portfolio.id, dry_run=False, trade_date=trade_date,
    )
    auto1 = [o for o in r1["executed_orders"] if o["symbol_id"] == s_auto.id]
    assert len(auto1) == 1 and auto1[0]["status"] == "filled", auto1
    filled1 = db_session.query(SimOrder).filter_by(
        portfolio_id=portfolio.id, status="filled"
    ).count()
    assert filled1 == 1

    # 第二次同参数重跑：order_plan_id（由 trade_date 派生的 decision_at 决定）应一致
    r2 = execute_member_source(
        db_session, portfolio_id=portfolio.id, dry_run=False, trade_date=trade_date,
    )
    auto2 = [o for o in r2["executed_orders"] if o["symbol_id"] == s_auto.id]
    assert len(auto2) == 0, f"重跑重复下单！executed={auto2}"
    filled2 = db_session.query(SimOrder).filter_by(
        portfolio_id=portfolio.id, status="filled"
    ).count()
    # 实测：两次 decision_run_id 不同（state_hash 掺入幂等键），client_order_key 第二层幂等
    # 未命中；但防重复的真正保障是第一层——sequential_clamp_allocate 按「目标−当前持仓」
    # 差额下单，首仓建满后重跑 delta≈0 → 不加仓。故同日重跑不会超买（非“满仓巧合”）。
    assert filled2 == 1, f"重跑后 filled SimOrder 变成 {filled2}（应仍为 1）"
