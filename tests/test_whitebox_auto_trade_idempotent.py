"""白盒测试 - WP6.3 幂等订单。

守护 client_order_key 幂等防重，确保调度重跑不重复下单：
1. find_existing_order_by_client_key 存在/不存在
2. execute_order_idempotent 首次执行/重复执行跳过
3. run_idempotent_member_source 首次运行/重复运行/不同 key/dry_run
4. client_order_key 格式 + 确定性
5. 同一信号日重复执行跳过 / 不同信号日可分别下单
6. confirm 模式幂等
7. 失败不占用 client_order_key

测试用 SQLite 内存库（通过 conftest.db_session fixture）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    EXECUTION_MANUAL,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.score import Score
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import (
    TradeDecision,
    build_client_order_key,
    decide_trades,
    execute_order_idempotent,
    find_existing_order_by_client_key,
    run_idempotent_member_source,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助（与 test_whitebox_auto_trade_member_source.py 保持一致）
# ============================================================================


def _make_portfolio(
    db_session,
    name: str = "QA-Idempotent",
    *,
    account_type: str = "simulated",
    total_capital: float = 100000.0,
    auto_trade_enabled: bool = True,
) -> Portfolio:
    """造一个组合并初始化 CashLedger。"""
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
        auto_trade_enabled=int(auto_trade_enabled),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    if account_type == "simulated":
        ensure_sim_account_seed(db_session, p)
        db_session.commit()
    return p


def _make_symbol(
    db_session,
    symbol: str = "600010",
    name: str = "测试标的",
    market: str = "sh",
    asset_type: str = "stock",
) -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type=asset_type, market=market)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(
    db_session,
    symbol_id: int,
    trade_date: date = date(2026, 7, 17),
    close: float = 10.0,
):
    from app.models.daily_bar import DailyBar

    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=close,
        high=close * 1.02,
        low=close * 0.98,
        close=close,
        volume=100000.0,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_score(
    db_session,
    symbol_id: int,
    *,
    action: str = "open",
    stage: str = "start",
    trade_date: date = date(2026, 7, 17),
    priority_score: float = 75.0,
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=70.0,
        quality_grade="B",
        timing_score=65.0,
        stage=stage,
        action=action,
        priority_score=priority_score,
    )
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def _make_member(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    status: str = STATUS_ACTIVE,
    execution_mode: str = EXECUTION_AUTO,
    manual_lock: bool = False,
    entry_rule_version_id: int | None = None,
    exit_rule_version_id: int | None = None,
) -> PortfolioMember:
    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=execution_mode,
        manual_lock=manual_lock,
        entry_rule_version_id=entry_rule_version_id,
        exit_rule_version_id=exit_rule_version_id,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _make_decision(
    *,
    portfolio_id: int,
    member: PortfolioMember | None,
    symbol_id: int,
    side: str = "buy",
    action: str = "open",
    execution_mode: str = EXECUTION_AUTO,
    signal_id: int | None = None,
    rule_version_id: int | None = None,
    client_order_key: str | None = None,
    signal_date: str = "2024-01-15",
) -> TradeDecision:
    """构造一个 TradeDecision（用于直接测试 execute_order_idempotent）。"""
    if client_order_key is None:
        client_order_key = build_client_order_key(
            portfolio_id=portfolio_id,
            member_id=member.id if member else None,
            signal_date=signal_date,
            side=side,
            rule_version_id=rule_version_id,
        )
    return TradeDecision(
        portfolio_id=portfolio_id,
        member=member,
        symbol_id=symbol_id,
        side=side,
        quantity=0,
        price=0,
        action=action,
        execution_mode=execution_mode,
        signal_id=signal_id,
        signal_snapshot=None,
        rule_version_id=rule_version_id,
        client_order_key=client_order_key,
        decision_snapshot={},
    )


def _apply_member_snapshot(
    db_session,
    portfolio,
    members: list[dict],
    *,
    factor_model_run_id: str = "idem-model",
):
    """为 run_idempotent_member_source（迁移后走 DecisionEngine）seed save_and_apply 快照。

    迁移后幂等键为 DecisionOrderPlan.order_plan_id（非旧 client_order_key），且入口
    fail-closed 要求已应用快照。DecisionEngine scorer 按 factor_model_run_id 过滤 Score，
    故此处对齐相关 score 的 factor_model_run_id。
    """
    import hashlib
    import json as _json

    from app.models.decision_engine import StrategyExecutionSnapshot

    sym_ids = [int(m["symbol_id"]) for m in members if m.get("symbol_id") is not None]
    if sym_ids:
        db_session.query(Score).filter(Score.symbol_id.in_(sym_ids)).update(
            {"factor_model_run_id": factor_model_run_id}, synchronize_session=False,
        )
    snap_id = f"idem-snap-{portfolio.id}"
    snap = StrategyExecutionSnapshot(
        id=snap_id, snapshot_no=1, portfolio_id=portfolio.id,
        factor_model_run_id=factor_model_run_id, decision_clock_json="{}",
        member_snapshot_json=_json.dumps(members),
        snapshot_type="save_and_apply",
        snapshot_hash=hashlib.sha256(snap_id.encode()).hexdigest(),
        gate_policy_version="production-v1.0.0",
        versions_json=_json.dumps({"run_mode": "research", "pit_mode": "best_effort"}),
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(snap)
    db_session.commit()
    return snap


# ============================================================================
# 1. find_existing_order_by_client_key
# ============================================================================


class TestFindExistingOrderByClientKey:
    """守护按 client_order_key 查找已存在订单。"""

    def test_find_existing_returns_order(self, db_session):
        """【WP6.3】已存在的 client_order_key → 返回订单。"""
        p = _make_portfolio(db_session, name="QA-Find-Exist")
        sym = _make_symbol(db_session, symbol="700001")
        order = SimOrder(
            portfolio_id=p.id,
            symbol_id=sym.id,
            side="buy",
            order_type="market",
            quantity=100,
            submitted_price=10.0,
            status="filled",
            client_order_key="key1",
        )
        db_session.add(order)
        db_session.commit()

        found = find_existing_order_by_client_key(db_session, client_order_key="key1")
        assert found is not None
        assert found.id == order.id
        assert found.client_order_key == "key1"

    def test_find_nonexistent_returns_none(self, db_session):
        """【WP6.3】不存在的 client_order_key → 返回 None。"""
        found = find_existing_order_by_client_key(
            db_session, client_order_key="nonexistent_key"
        )
        assert found is None


# ============================================================================
# 2. execute_order_idempotent
# ============================================================================


class TestExecuteOrderIdempotent:
    """守护 execute_order_idempotent 幂等执行。"""

    def test_first_execution_creates_order(self, db_session):
        """【WP6.3】首次执行 → status=executed，SimOrder 创建。"""
        p = _make_portfolio(db_session, name="QA-Idempotent-First")
        sym = _make_symbol(db_session, symbol="700010")
        _make_daily_bar(db_session, sym.id, close=10.0)
        member = _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        decision = _make_decision(
            portfolio_id=p.id,
            member=member,
            symbol_id=sym.id,
        )

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        order, status = execute_order_idempotent(db_session, decision=decision)
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        assert status == "executed"
        assert order is not None
        assert order.id is not None
        assert after == before + 1
        # 归因字段 client_order_key 已填充
        assert order.client_order_key == decision.client_order_key

    def test_duplicate_execution_skipped(self, db_session):
        """【WP6.3】重复执行 → status=skipped_existing，不创建新订单。"""
        p = _make_portfolio(db_session, name="QA-Idempotent-Dup")
        sym = _make_symbol(db_session, symbol="700011")
        _make_daily_bar(db_session, sym.id, close=10.0)
        member = _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        decision = _make_decision(
            portfolio_id=p.id,
            member=member,
            symbol_id=sym.id,
        )

        # 首次执行
        order1, status1 = execute_order_idempotent(db_session, decision=decision)
        assert status1 == "executed"
        assert order1 is not None

        # 重复执行相同 decision
        order2, status2 = execute_order_idempotent(db_session, decision=decision)
        assert status2 == "skipped_existing"
        assert order2 is not None
        assert order2.id == order1.id  # 返回的是已存在的订单

        # DB 中只有 1 个订单（不重复创建）
        count = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        assert count == 1


# ============================================================================
# 3. run_idempotent_member_source 主入口
# ============================================================================


class TestRunIdempotentMemberSource:
    """守护 run_idempotent_member_source 主入口幂等行为。"""

    def test_first_run_executes_one_order(self, db_session):
        """【WP6.3】首次运行 → executed_orders 含 1 个（新契约：order_plan_id 为幂等键）。"""
        p = _make_portfolio(db_session, name="QA-Run-First")
        sym = _make_symbol(db_session, symbol="700020")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO},
        ])

        result = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )

        assert len(result["executed_orders"]) == 1
        assert result["executed_orders"][0]["order_id"] is not None
        # 新契约：order_plan_id 取代 client_order_key 作为幂等键
        assert result["executed_orders"][0]["order_plan_id"] is not None
        assert (
            db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1
        )

    def test_rerun_does_not_duplicate(self, db_session):
        """【新契约·分配器 delta 收敛】同日重跑不重复建仓。

        真实防重复机制：sequential_clamp_allocate 按「目标仓位 − 当前持仓」下单，
        首次建仓后 current 到位 → 重跑 delta≈0 → target_quantity≤0 → 不加仓。
        （decision_run_id/order_plan_id 因 state_hash 变化会变，client_order_key
        幂等只是第二层；本用例锁定第一层——不超买。）
        """
        p = _make_portfolio(db_session, name="QA-Run-Rerun")
        sym = _make_symbol(db_session, symbol="700021")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO},
        ])

        # 首次：建仓到目标
        r1 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )
        filled1 = db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="filled"
        ).count()
        assert filled1 == 1

        # 第二次同日重跑：持仓已到目标 → delta 收敛 → 不新增成交
        r2 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )
        filled2 = db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="filled"
        ).count()
        assert filled2 == 1, f"同日重跑重复建仓：filled 从 1 变为 {filled2}"

    def test_different_client_keys_can_execute_separately(self, db_session):
        """【WP6.3】不同 client_order_key 可分别下单。"""
        p = _make_portfolio(db_session, name="QA-Run-DiffKeys")
        # 两个不同标的 + 成员
        sym1 = _make_symbol(db_session, symbol="700030")
        _make_daily_bar(db_session, sym1.id, close=10.0)
        _make_daily_bar(db_session, sym1.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym1.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym1.id,
            execution_mode=EXECUTION_AUTO,
        )

        sym2 = _make_symbol(db_session, symbol="700031")
        _make_daily_bar(db_session, sym2.id, close=10.0)
        _make_daily_bar(db_session, sym2.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym2.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym2.id,
            execution_mode=EXECUTION_AUTO,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym1.id, "execution_mode": EXECUTION_AUTO},
            {"symbol_id": sym2.id, "execution_mode": EXECUTION_AUTO},
        ])

        result = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )

        # 两个不同 order_plan_id 都执行
        assert len(result["executed_orders"]) == 2
        keys = {o["order_plan_id"] for o in result["executed_orders"]}
        assert len(keys) == 2
        assert (
            db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 2
        )

    def test_dry_run_does_not_place_order(self, db_session):
        """【WP6.3】dry_run=True 不创建 SimOrder，不占用幂等键。"""
        p = _make_portfolio(db_session, name="QA-Run-DryRun")
        sym = _make_symbol(db_session, symbol="700040")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO},
        ])

        # dry_run=True 不创建 SimOrder
        result1 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, dry_run=True, trade_date=date(2026, 7, 17),
        )
        assert len(result1["executed_orders"]) == 0
        assert (
            db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 0
        )

        # 再次 dry_run=False 应能执行（幂等键未被占用）
        result2 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )
        assert len(result2["executed_orders"]) == 1
        assert (
            db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1
        )


# ============================================================================
# 4. client_order_key 格式验证
# ============================================================================


class TestClientOrderKeyFormat:
    """守护 client_order_key 格式（WP6.3 幂等基础）。"""

    def test_key_format(self):
        """【WP6.3】格式：portfolio_id:member_id:signal_date:side:rule_version_id。"""
        key = build_client_order_key(
            portfolio_id=1,
            member_id=2,
            signal_date="2024-01-15",
            side="buy",
            rule_version_id=3,
        )
        assert key == "1:2:2024-01-15:buy:3"

    def test_same_input_same_key(self):
        """【WP6.3】相同输入生成相同 key（幂等基础）。"""
        kwargs = dict(
            portfolio_id=1,
            member_id=2,
            signal_date="2024-01-15",
            side="buy",
            rule_version_id=3,
        )
        key1 = build_client_order_key(**kwargs)
        key2 = build_client_order_key(**kwargs)
        assert key1 == key2

    def test_different_input_different_key(self):
        """【WP6.3】不同输入生成不同 key。"""
        key1 = build_client_order_key(
            portfolio_id=1,
            member_id=2,
            signal_date="2024-01-15",
            side="buy",
            rule_version_id=3,
        )
        # 不同信号日
        key2 = build_client_order_key(
            portfolio_id=1,
            member_id=2,
            signal_date="2024-01-16",
            side="buy",
            rule_version_id=3,
        )
        assert key1 != key2
        # 不同 side
        key3 = build_client_order_key(
            portfolio_id=1,
            member_id=2,
            signal_date="2024-01-15",
            side="sell",
            rule_version_id=3,
        )
        assert key1 != key3


# ============================================================================
# 5. 同一/不同信号日幂等
# ============================================================================


class TestSignalDateIdempotency:
    """守护同一信号日重复执行跳过，不同信号日可分别下单。"""

    def test_same_signal_date_skipped(self, db_session):
        """【WP6.3】同一信号日重复执行 → 跳过（client_order_key 相同）。"""
        p = _make_portfolio(db_session, name="QA-SignalDate-Same")
        sym = _make_symbol(db_session, symbol="700050")
        _make_daily_bar(db_session, sym.id, close=10.0)
        member = _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        # 同一信号日的 decision
        decision = _make_decision(
            portfolio_id=p.id,
            member=member,
            symbol_id=sym.id,
            signal_date="2024-01-15",
        )

        # 首次执行
        order1, status1 = execute_order_idempotent(db_session, decision=decision)
        assert status1 == "executed"

        # 同一信号日再次执行（相同 client_order_key）
        order2, status2 = execute_order_idempotent(db_session, decision=decision)
        assert status2 == "skipped_existing"
        assert order2.id == order1.id

        # DB 中只有 1 个订单
        assert (
            db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1
        )

    def test_different_signal_date_can_execute(self, db_session):
        """【WP6.3】不同信号日可分别下单（client_order_key 不同）。"""
        p = _make_portfolio(db_session, name="QA-SignalDate-Diff")
        sym = _make_symbol(db_session, symbol="700051")
        _make_daily_bar(db_session, sym.id, close=10.0)
        member = _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        # 信号日 1
        decision1 = _make_decision(
            portfolio_id=p.id,
            member=member,
            symbol_id=sym.id,
            signal_date="2024-01-15",
        )
        order1, status1 = execute_order_idempotent(db_session, decision=decision1)
        assert status1 == "executed"
        assert order1 is not None

        # 信号日 2（不同 client_order_key）
        decision2 = _make_decision(
            portfolio_id=p.id,
            member=member,
            symbol_id=sym.id,
            signal_date="2024-01-16",
        )
        order2, status2 = execute_order_idempotent(db_session, decision=decision2)
        assert status2 == "executed"
        assert order2 is not None
        assert order2.id != order1.id

        # DB 中有 2 个订单
        assert (
            db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 2
        )


# ============================================================================
# 6. confirm 模式幂等
# ============================================================================


class TestConfirmModeIdempotent:
    """守护 confirm 模式幂等（待确认订单不重复生成）。"""

    def test_confirm_mode_idempotent(self, db_session):
        """【Q1·新契约】confirm 建待确认单；同日重跑 pending_key 幂等不重建。"""
        p = _make_portfolio(db_session, name="QA-Confirm-Idempotent")
        sym = _make_symbol(db_session, symbol="700060")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_CONFIRM,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_CONFIRM},
        ])

        # 首次：confirm 建一张 pending 单 + signal_decisions 1，不成交
        r1 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )
        assert len(r1["signal_decisions"]) == 1
        assert r1["signal_decisions"][0]["execution_mode"] == EXECUTION_CONFIRM
        assert db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="pending_confirmation").count() == 1
        assert db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="filled").count() == 0

        # 第二次同日重跑：pending_key 命中 → 不重复建待确认单
        r2 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )
        pending2 = db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="pending_confirmation").count()
        assert pending2 == 1, f"重跑重复建待确认单：{pending2}"
        conf = [o for o in r2["executed_orders"]
                if o.get("execution_mode") == EXECUTION_CONFIRM]
        assert len(conf) == 1 and conf[0]["status"] == "pending_confirmation"


# ============================================================================
# 7. 失败不占用 client_order_key
# ============================================================================


class TestFailureDoesNotOccupyKey:
    """守护失败不占用 client_order_key（重试可执行）。"""

    def test_failure_then_retry_can_execute(self, db_session):
        """【新契约】下单失败不占 client_order_key → 可重试；并验证 errors 已脱敏。"""
        p = _make_portfolio(db_session, name="QA-Fail-Retry")
        sym = _make_symbol(db_session, symbol="700070")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO},
        ])

        # 第一次：真实成交落库点 place_sim_order 抛异常（含内部细节，验证不泄漏）
        with patch(
            "app.services.sim_accounts.place_sim_order",
            side_effect=RuntimeError("内部堆栈细节 cash_ledger boom"),
        ):
            result1 = run_idempotent_member_source(
                db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
            )

        # errors 含 1 个，且已脱敏为固定码（不泄漏异常原文）
        assert len(result1["errors"]) == 1
        assert result1["errors"][0]["error"] == "order_execution_failed"
        assert "cash_ledger boom" not in result1["errors"][0]["error"]
        # DB 中无 SimOrder（失败回滚，不占用 client_order_key）
        assert db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 0

        # 第二次：不 mock，重试成功（client_order_key 未被占用）
        result2 = run_idempotent_member_source(
            db_session, portfolio_id=p.id, trade_date=date(2026, 7, 17),
        )
        auto2 = [o for o in result2["executed_orders"] if o["symbol_id"] == sym.id]
        assert len(auto2) == 1 and auto2[0]["status"] == "filled"
        assert db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1
