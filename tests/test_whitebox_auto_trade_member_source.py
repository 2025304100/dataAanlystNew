"""白盒测试 - WP6.2 基于成员的新执行逻辑。

守护 auto_trade_member_source 的关键行为，确保与旧逻辑（auto_trade_task）双轨并存：
1. get_buy_candidates 基本流程 + 各种过滤条件
2. get_sell_candidates 覆盖所有持仓（含暂停/归档/无成员）
3. decide_trades 冲突优先级（卖出 > 买入）+ 手动锁定 + 拒绝决策
4. execute_member_source 三种执行模式互不混淆（auto 下单 / confirm 待确认 / manual 只提示）
5. dry_run 不下单
6. 三种模式互不混淆
7. client_order_key 格式
8. 归因字段填充
9. 拒绝决策记录

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
    STATUS_PAUSED,
)
from app.models.score import Score
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import (
    build_client_order_key,
    decide_trades,
    execute_member_source,
    get_buy_candidates,
    get_sell_candidates,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================


def _make_portfolio(
    db_session,
    name: str = "QA-MemberSource",
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
    """写入一条日线行情（latest_price_for_symbol 取最新一条）。"""
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


def _make_position(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    quantity: float = 100,
    avg_cost: float = 10.0,
    latest_price: float = 10.0,
    asset_type: str = "stock",
    opened_at_days_ago: int = 5,
) -> Position:
    """手动造一个持仓（绕过 place_sim_order，避免 T+1 等市场规则干扰）。"""
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=avg_cost,
        latest_price=latest_price,
        market_value=quantity * latest_price,
        position_pct=round(quantity * latest_price / 100000.0, 4),
        asset_type=asset_type,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=opened_at_days_ago),
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


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
    """造一个 PortfolioMember。"""
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


def _apply_member_snapshot(
    db_session,
    portfolio,
    members: list[dict],
    *,
    factor_model_run_id: str = "ms-model",
):
    """为 execute_member_source（迁移后走 DecisionEngine）seed 一个 save_and_apply 快照。

    迁移后 execute_member_source 不再走 decide_trades，而是 fail-closed 要求已应用
    快照（STRATEGY_SNAPSHOT_REQUIRED）。DecisionEngine scorer 按 factor_model_run_id
    过滤 Score，故此处把相关 score 的 factor_model_run_id 对齐到快照，保证命中。
    members: [{"symbol_id": int, "execution_mode": str, "entry_rule_version_id": int|None}]
    """
    import hashlib
    import json as _json

    from app.models.decision_engine import StrategyExecutionSnapshot

    sym_ids = [int(m["symbol_id"]) for m in members if m.get("symbol_id") is not None]
    if sym_ids:
        db_session.query(Score).filter(Score.symbol_id.in_(sym_ids)).update(
            {"factor_model_run_id": factor_model_run_id}, synchronize_session=False,
        )
    snap_id = f"ms-snap-{portfolio.id}"
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
# 1. get_buy_candidates 基本流程
# ============================================================================


class TestGetBuyCandidates:
    """守护买入候选筛选逻辑。"""

    def test_basic_flow_returns_one_candidate(self, db_session):
        """【WP6.2】active+auto 成员 + Score(action=open) + 无持仓 → 返回 1 个候选。"""
        p = _make_portfolio(db_session, name="QA-Buy-Basic")
        sym = _make_symbol(db_session, symbol="600001")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            status=STATUS_ACTIVE,
            execution_mode=EXECUTION_AUTO,
        )

        candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.member is not None
        assert cand.member.execution_mode == EXECUTION_AUTO
        assert cand.symbol_id == sym.id
        assert cand.symbol == "600001"
        assert cand.action == "open"
        assert cand.signal_id is not None
        assert cand.signal_snapshot is not None
        # WP6.2 占位：data_health_ok / risk_check_ok 默认 True
        assert cand.data_health_ok is True
        assert cand.risk_check_ok is True
        assert cand.rejection_code is None

    # ========================================================================
    # 2. 过滤 paused 成员
    # ========================================================================

    def test_filters_paused_member(self, db_session):
        """【WP6.2】paused 成员不进入买入候选（仅 active）。"""
        p = _make_portfolio(db_session, name="QA-Buy-Paused")
        sym = _make_symbol(db_session, symbol="600002")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            status=STATUS_PAUSED,
            execution_mode=EXECUTION_AUTO,
        )

        candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 0  # paused 不算 active

    # ========================================================================
    # 3. 过滤已持仓
    # ========================================================================

    def test_filters_existing_position(self, db_session):
        """【WP6.2】已有持仓的标的，跳过买入候选。"""
        p = _make_portfolio(db_session, name="QA-Buy-HasPos")
        sym = _make_symbol(db_session, symbol="600003")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )
        # 已有持仓
        _make_position(db_session, p.id, sym.id, quantity=100)

        candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 0

    # ========================================================================
    # 4. 过滤非 auto 模式
    # ========================================================================

    def test_filters_non_auto_mode(self, db_session):
        """【WP6.2】manual/confirm 成员不进入 auto 买入候选。"""
        p = _make_portfolio(db_session, name="QA-Buy-NonAuto")
        sym_manual = _make_symbol(db_session, symbol="600004")
        _make_daily_bar(db_session, sym_manual.id)
        _make_score(db_session, sym_manual.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_manual.id,
            execution_mode=EXECUTION_MANUAL,
        )

        sym_confirm = _make_symbol(db_session, symbol="600005")
        _make_daily_bar(db_session, sym_confirm.id)
        _make_score(db_session, sym_confirm.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_confirm.id,
            execution_mode=EXECUTION_CONFIRM,
        )

        candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 0  # manual/confirm 不进入 auto 买入候选

    # ========================================================================
    # 5. 信号不允许买入
    # ========================================================================

    def test_filters_signal_not_allow_buy(self, db_session):
        """【WP6.2】Score(action=hold) 不在 _BUY_ACTIONS，跳过。"""
        p = _make_portfolio(db_session, name="QA-Buy-Hold")
        sym = _make_symbol(db_session, symbol="600006")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="hold")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 0

    def test_buy_dip_action_also_returns_candidate(self, db_session):
        """【WP6.2】Score(action=buy_dip) 也允许买入。"""
        p = _make_portfolio(db_session, name="QA-Buy-Dip")
        sym = _make_symbol(db_session, symbol="600007")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="buy_dip")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        candidates = get_buy_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 1
        assert candidates[0].action == "buy_dip"


# ============================================================================
# 6. get_sell_candidates 覆盖所有持仓
# ============================================================================


class TestGetSellCandidates:
    """守护卖出侧覆盖所有持仓（不因成员状态漏掉止损/退出信号）。"""

    def test_covers_all_positions(self, db_session):
        """【WP6.2】卖出侧覆盖 3 类持仓：active 成员、paused 成员、无成员。

        spec line 230：卖出侧覆盖所有当前持仓，即使成员暂停或归档，
        不因成员状态漏掉止损/退出信号。
        """
        p = _make_portfolio(db_session, name="QA-Sell-All")

        # 持仓 1：active 成员
        sym1 = _make_symbol(db_session, symbol="600010")
        _make_daily_bar(db_session, sym1.id)
        _make_score(db_session, sym1.id, action="exit")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym1.id,
            status=STATUS_ACTIVE,
            execution_mode=EXECUTION_AUTO,
        )
        _make_position(db_session, p.id, sym1.id, quantity=100)

        # 持仓 2：paused 成员（成员仍 effective_to=None，仅 status=paused）
        sym2 = _make_symbol(db_session, symbol="600011")
        _make_daily_bar(db_session, sym2.id)
        _make_score(db_session, sym2.id, action="exit")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym2.id,
            status=STATUS_PAUSED,
            execution_mode=EXECUTION_AUTO,
        )
        _make_position(db_session, p.id, sym2.id, quantity=200)

        # 持仓 3：无成员（历史持仓）
        sym3 = _make_symbol(db_session, symbol="600012")
        _make_daily_bar(db_session, sym3.id)
        _make_score(db_session, sym3.id, action="exit")
        # 不创建 PortfolioMember
        _make_position(db_session, p.id, sym3.id, quantity=300)

        candidates = get_sell_candidates(db_session, portfolio_id=p.id)

        # 应返回 3 个，不因成员状态漏掉
        assert len(candidates) == 3
        symbol_ids = {c.symbol_id for c in candidates}
        assert symbol_ids == {sym1.id, sym2.id, sym3.id}

        # 验证成员关联
        cand_by_sym = {c.symbol_id: c for c in candidates}
        assert cand_by_sym[sym1.id].member is not None
        assert cand_by_sym[sym2.id].member is not None  # paused 成员也能找到
        assert cand_by_sym[sym3.id].member is None  # 无成员

    # ========================================================================
    # 7. 信号不允许卖出
    # ========================================================================

    def test_filters_signal_not_allow_sell(self, db_session):
        """【WP6.2】Score(action=hold) 不在 _SELL_ACTIONS，跳过。"""
        p = _make_portfolio(db_session, name="QA-Sell-Hold")
        sym = _make_symbol(db_session, symbol="600020")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="hold")
        _make_position(db_session, p.id, sym.id, quantity=100)

        candidates = get_sell_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 0

    def test_reduce_action_also_returns_candidate(self, db_session):
        """【WP6.2】Score(action=reduce) 也触发卖出候选。"""
        p = _make_portfolio(db_session, name="QA-Sell-Reduce")
        sym = _make_symbol(db_session, symbol="600021")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="reduce")
        _make_position(db_session, p.id, sym.id, quantity=100)

        candidates = get_sell_candidates(db_session, portfolio_id=p.id)

        assert len(candidates) == 1
        assert candidates[0].action == "reduce"


# ============================================================================
# 8. decide_trades 冲突优先级
# ============================================================================


class TestDecideTradesPriority:
    """守护冲突优先级（spec line 233）：
    手动锁定 → 组合级风险 → 自动卖出规则 → 自动买入规则 → 普通信号建议。
    """

    def test_sell_before_buy(self, db_session):
        """【WP6.2】卖出决策排在买入决策之前。"""
        p = _make_portfolio(db_session, name="QA-Priority-SellBuy")
        # 卖出：持仓 + Score(action=exit)
        sym_sell = _make_symbol(db_session, symbol="600030")
        _make_daily_bar(db_session, sym_sell.id)
        _make_score(db_session, sym_sell.id, action="exit")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_sell.id,
            execution_mode=EXECUTION_AUTO,
        )
        _make_position(db_session, p.id, sym_sell.id, quantity=100)

        # 买入：另一个标的（无持仓）+ Score(action=open)
        sym_buy = _make_symbol(db_session, symbol="600031")
        _make_daily_bar(db_session, sym_buy.id)
        _make_score(db_session, sym_buy.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_buy.id,
            execution_mode=EXECUTION_AUTO,
        )

        decisions = decide_trades(db_session, portfolio_id=p.id)

        # 至少有 1 个卖出 + 1 个买入
        sell_decisions = [d for d in decisions if d.side == "sell"]
        buy_decisions = [d for d in decisions if d.side == "buy"]
        assert len(sell_decisions) >= 1
        assert len(buy_decisions) >= 1

        # 验证顺序：第一个 sell 出现在第一个 buy 之前
        first_sell_idx = next(i for i, d in enumerate(decisions) if d.side == "sell")
        first_buy_idx = next(i for i, d in enumerate(decisions) if d.side == "buy")
        assert first_sell_idx < first_buy_idx

    # ========================================================================
    # 9. 手动锁定跳过
    # ========================================================================

    def test_manual_lock_skipped(self, db_session):
        """【WP6.2】manual_lock=True 的成员，不生成决策。"""
        p = _make_portfolio(db_session, name="QA-Priority-Lock")
        sym = _make_symbol(db_session, symbol="600040")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
            manual_lock=True,
        )

        decisions = decide_trades(db_session, portfolio_id=p.id)

        # manual_lock 跳过：不生成任何决策
        assert len(decisions) == 0

    def test_manual_lock_skipped_for_sell_too(self, db_session):
        """【WP6.2】manual_lock 的成员持仓也跳过卖出（手动锁定最高优先级）。"""
        p = _make_portfolio(db_session, name="QA-Priority-LockSell")
        sym = _make_symbol(db_session, symbol="600041")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="exit")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
            manual_lock=True,
        )
        _make_position(db_session, p.id, sym.id, quantity=100)

        decisions = decide_trades(db_session, portfolio_id=p.id)

        assert len(decisions) == 0

    # ========================================================================
    # 10. 数据健康不通过 → 拒绝决策
    # ========================================================================

    def test_data_health_blocked_generates_rejection(self, db_session):
        """【WP6.2】数据健康检查不通过 → 生成拒绝决策（rejection_code="BLOCKED"）。"""
        p = _make_portfolio(db_session, name="QA-Priority-DataHealth")
        sym = _make_symbol(db_session, symbol="600050")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        # mock _check_data_health 返回 False
        with patch(
            "app.services.auto_trade_member_source._check_data_health",
            return_value=(False, "K线数据过期"),
        ):
            decisions = decide_trades(db_session, portfolio_id=p.id)

        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.rejection_code == "BLOCKED"
        assert decision.rejection_detail is not None
        assert "K线数据过期" in decision.rejection_detail

    def test_risk_blocked_generates_rejection(self, db_session):
        """【WP6.2】组合风控不通过 → 生成拒绝决策。"""
        p = _make_portfolio(db_session, name="QA-Priority-Risk")
        sym = _make_symbol(db_session, symbol="600051")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
        )

        with patch(
            "app.services.auto_trade_member_source._check_portfolio_risk",
            return_value=(False, "组合超限"),
        ):
            decisions = decide_trades(db_session, portfolio_id=p.id)

        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.rejection_code == "BLOCKED"
        assert "组合超限" in decision.rejection_detail


# ============================================================================
# 11. execute_member_source auto 模式下单
# ============================================================================


class TestExecuteAutoMode:
    """守护 auto 模式实际下单 + 归因字段填充。"""

    def test_auto_mode_places_order(self, db_session):
        """【WP6.2】auto 模式 + dry_run=False → 实际下单创建 SimOrder。"""
        p = _make_portfolio(db_session, name="QA-Exec-Auto")
        sym = _make_symbol(db_session, symbol="600060")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
            entry_rule_version_id=1001,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO, "entry_rule_version_id": 1001},
        ])

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        assert after == before + 1
        # buy_decisions 应有 1 条
        assert len(result["buy_decisions"]) == 1
        # executed_orders 应有 1 条 status="filled"
        assert len(result["executed_orders"]) == 1
        assert result["executed_orders"][0]["status"] == "filled"
        assert result["executed_orders"][0]["order_id"] is not None

    def test_auto_mode_attribution_fields_populated(self, db_session):
        """【WP6.2/C 簇】auto 下单后 SimOrder 按新契约归因（追溯走 evidence/plan）。"""
        p = _make_portfolio(db_session, name="QA-Exec-AutoAttr")
        sym = _make_symbol(db_session, symbol="600061")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
            entry_rule_version_id=1002,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO, "entry_rule_version_id": 1002},
        ])

        execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )

        order = (
            db_session.query(SimOrder)
            .filter_by(portfolio_id=p.id, symbol_id=sym.id)
            .one()
        )
        # C 簇新契约：归因移至 DecisionEvidence/plan ledger，追溯走不可变标识
        assert order.source_type == "decision_engine"
        assert order.decision_evidence_id is not None
        assert order.client_order_key is not None
        assert order.client_order_key != ""
        assert order.decision_snapshot_json is not None
        assert "action" in order.decision_snapshot_json
        # legacy 整型归因字段在新契约下不再填充（已移至 evidence）
        assert order.source_id is None
        assert order.member_id is None
        assert order.signal_id is None
        assert order.rule_version_id is None

    # ========================================================================
    # 12. confirm 模式只生成计划
    # ========================================================================

    def test_confirm_mode_no_order_only_pending(self, db_session):
        """【WP6.2/Q1】confirm 模式 → 落一张待确认占位单（pending_confirmation，不成交）。"""
        p = _make_portfolio(db_session, name="QA-Exec-Confirm")
        sym = _make_symbol(db_session, symbol="600070")
        _make_daily_bar(db_session, sym.id)
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

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        # Q1 决策：confirm 落一张 pending_confirmation 占位单（quantity=0，不成交）
        assert after == before + 1
        assert len(result["signal_decisions"]) == 1
        assert result["signal_decisions"][0]["execution_mode"] == EXECUTION_CONFIRM
        assert len(result["executed_orders"]) == 1
        assert result["executed_orders"][0]["status"] == "pending_confirmation"
        assert result["executed_orders"][0]["order_id"] is not None
        pending = db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="pending_confirmation").all()
        assert len(pending) == 1 and pending[0].symbol_id == sym.id
        # 待确认单不产生成交（filled 仍 0）
        assert db_session.query(SimOrder).filter_by(
            portfolio_id=p.id, status="filled").count() == 0

    # ========================================================================
    # 13. manual 模式只提示信号
    # ========================================================================

    def test_manual_mode_no_order_only_signal(self, db_session):
        """【WP6.2】manual 模式 → 只提示信号不下单，不创建 SimOrder。"""
        p = _make_portfolio(db_session, name="QA-Exec-Manual")
        sym = _make_symbol(db_session, symbol="600080")
        _make_daily_bar(db_session, sym.id)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_MANUAL,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_MANUAL},
        ])

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        assert after == before  # 不创建 SimOrder
        # signal_decisions 应有 1 条
        assert len(result["signal_decisions"]) == 1
        assert result["signal_decisions"][0]["execution_mode"] == EXECUTION_MANUAL
        # executed_orders 应有 1 条 status="signal_only"
        assert len(result["executed_orders"]) == 1
        assert result["executed_orders"][0]["status"] == "signal_only"
        assert "order_id" not in result["executed_orders"][0]

    # ========================================================================
    # 14. dry_run 不下单
    # ========================================================================

    def test_dry_run_does_not_place_order(self, db_session):
        """【WP6.2】dry_run=True → 只返回决策，不实际下单。"""
        p = _make_portfolio(db_session, name="QA-Exec-DryRun")
        sym = _make_symbol(db_session, symbol="600090")
        _make_daily_bar(db_session, sym.id)
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

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=True, trade_date=date(2026, 7, 17),
        )
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        assert after == before  # dry_run 不下单
        # 但应返回决策
        assert len(result["buy_decisions"]) == 1
        # executed_orders 应为空（dry_run 跳过执行）
        assert len(result["executed_orders"]) == 0


# ============================================================================
# 15. 三种执行模式结果互不混淆
# ============================================================================


class TestThreeModesDontInterfere:
    """守护 spec line 234：三种执行模式结果互不混淆。"""

    def test_three_modes_in_one_portfolio(self, db_session):
        """【WP6.2】同一组合内三种模式的成员分别产生不同结果。"""
        p = _make_portfolio(db_session, name="QA-Three-Modes")

        # auto 成员 → 实际下单
        sym_auto = _make_symbol(db_session, symbol="600100")
        _make_daily_bar(db_session, sym_auto.id, close=10.0)
        _make_daily_bar(db_session, sym_auto.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym_auto.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_auto.id,
            execution_mode=EXECUTION_AUTO,
        )

        # confirm 成员 → 待确认
        sym_confirm = _make_symbol(db_session, symbol="600101")
        _make_daily_bar(db_session, sym_confirm.id)
        _make_score(db_session, sym_confirm.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_confirm.id,
            execution_mode=EXECUTION_CONFIRM,
        )

        # manual 成员 → 只提示
        sym_manual = _make_symbol(db_session, symbol="600102")
        _make_daily_bar(db_session, sym_manual.id)
        _make_score(db_session, sym_manual.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym_manual.id,
            execution_mode=EXECUTION_MANUAL,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym_auto.id, "execution_mode": EXECUTION_AUTO},
            {"symbol_id": sym_confirm.id, "execution_mode": EXECUTION_CONFIRM},
            {"symbol_id": sym_manual.id, "execution_mode": EXECUTION_MANUAL},
        ])

        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )

        # auto 实际下单（创建 SimOrder）
        auto_orders = [
            o for o in result["executed_orders"]
            if o["symbol_id"] == sym_auto.id
        ]
        assert len(auto_orders) == 1
        assert auto_orders[0]["status"] == "filled"
        assert "order_id" in auto_orders[0]

        # confirm 待确认 → Q1：落一张 pending 单，有 order_id
        confirm_orders = [
            o for o in result["executed_orders"]
            if o["symbol_id"] == sym_confirm.id
        ]
        assert len(confirm_orders) == 1
        assert confirm_orders[0]["status"] == "pending_confirmation"
        assert "order_id" in confirm_orders[0]

        # manual 只提示（Q1 决策：manual 不建单）→ 无 order_id
        manual_orders = [
            o for o in result["executed_orders"]
            if o["symbol_id"] == sym_manual.id
        ]
        assert len(manual_orders) == 1
        assert manual_orders[0]["status"] == "signal_only"
        assert "order_id" not in manual_orders[0]

        # Q1：SimOrder = auto 成交单 + confirm 待确认单（manual 不建单）
        by_status: dict = {}
        for o in db_session.query(SimOrder).filter_by(portfolio_id=p.id).all():
            by_status.setdefault(o.status, []).append(o)
        assert [o.symbol_id for o in by_status.get("filled", [])] == [sym_auto.id]
        assert [o.symbol_id for o in by_status.get("pending_confirmation", [])] == [sym_confirm.id]
        assert sym_manual.id not in {o.symbol_id for o in db_session.query(SimOrder).filter_by(portfolio_id=p.id).all()}
        # C 簇：auto 成交单通过 source_type=decision_engine + decision_evidence_id 追溯
        auto_sim = by_status["filled"][0]
        assert auto_sim.source_type == "decision_engine"
        assert auto_sim.decision_evidence_id is not None


# ============================================================================
# 16. build_client_order_key 格式
# ============================================================================


class TestClientOrderKey:
    """守护 client_order_key 格式（WP6.3 幂等基础）。"""

    def test_key_format_with_member_and_rule(self):
        """【WP6.2】完整参数：portfolio_id:member_id:signal_date:side:rule_version_id。"""
        key = build_client_order_key(
            portfolio_id=1,
            member_id=2,
            signal_date="2024-01-15",
            side="buy",
            rule_version_id=3,
        )
        assert key == "1:2:2024-01-15:buy:3"

    def test_key_format_with_null_member(self):
        """【WP6.2】无成员（持仓卖出）：member_id 用 0 占位。"""
        key = build_client_order_key(
            portfolio_id=1,
            member_id=None,
            signal_date="2024-01-15",
            side="sell",
            rule_version_id=None,
        )
        assert key == "1:0:2024-01-15:sell:0"

    def test_key_format_sell_side(self):
        """【WP6.2】卖出侧 key：side=sell。"""
        key = build_client_order_key(
            portfolio_id=10,
            member_id=20,
            signal_date="2024-12-31",
            side="sell",
            rule_version_id=5,
        )
        assert key == "10:20:2024-12-31:sell:5"

    def test_key_deterministic(self):
        """【WP6.2】相同参数生成相同 key（幂等基础）。"""
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


# ============================================================================
# 17. 归因字段填充（深度验证）
# ============================================================================


class TestAttributionFields:
    """守护归因字段全部填充（WP6.1 字段在 WP6.2 被使用）。"""

    def test_all_attribution_fields_non_null(self, db_session):
        """【WP6.2/C 簇】auto 下单后 SimOrder 新契约归因字段（追溯走 evidence/plan）。"""
        p = _make_portfolio(db_session, name="QA-Attr-AllFields")
        sym = _make_symbol(db_session, symbol="600200")
        _make_daily_bar(db_session, sym.id, close=10.0)
        _make_daily_bar(db_session, sym.id, trade_date=date(2026, 7, 18), close=10.1)
        _make_score(db_session, sym.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            execution_mode=EXECUTION_AUTO,
            entry_rule_version_id=2001,
        )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym.id, "execution_mode": EXECUTION_AUTO, "entry_rule_version_id": 2001},
        ])

        execute_member_source(
            db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
        )

        order = (
            db_session.query(SimOrder)
            .filter_by(portfolio_id=p.id, symbol_id=sym.id)
            .one()
        )

        # C 簇新契约：不可变归因标识非空（追溯走 evidence/plan ledger）
        assert order.source_type == "decision_engine"
        assert order.decision_evidence_id is not None
        assert order.client_order_key is not None
        assert order.decision_snapshot_json is not None
        # legacy 整型归因字段在新契约下不再填充（已移至 evidence）
        assert order.member_id is None
        assert order.source_id is None
        assert order.signal_id is None
        assert order.rule_version_id is None

    def test_client_order_key_unique_per_decision(self, db_session):
        """【WP6.2】不同决策生成不同 client_order_key。"""
        p = _make_portfolio(db_session, name="QA-Attr-UniqueKey")
        # 两个不同标的的 auto 成员
        sym1 = _make_symbol(db_session, symbol="600210")
        _make_daily_bar(db_session, sym1.id, close=10.0)
        _make_score(db_session, sym1.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym1.id,
            execution_mode=EXECUTION_AUTO,
        )

        sym2 = _make_symbol(db_session, symbol="600211")
        _make_daily_bar(db_session, sym2.id, close=10.0)
        _make_score(db_session, sym2.id, action="open")
        _make_member(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym2.id,
            execution_mode=EXECUTION_AUTO,
        )

        decisions = decide_trades(db_session, portfolio_id=p.id)
        buy_decisions = [d for d in decisions if d.side == "buy"]
        assert len(buy_decisions) == 2

        keys = {d.client_order_key for d in buy_decisions}
        assert len(keys) == 2  # 两个不同的 key


# ============================================================================
# 18. 拒绝决策记录
# ============================================================================


class TestRejectionDecisions:
    """守护拒绝决策被记录到 rejected_decisions。"""

    def test_rejection_recorded_in_result(self, db_session):
        """【新契约】组合处于阻断状态 → BUY plan 进 rejected_decisions（治理留痕）。"""
        p = _make_portfolio(db_session, name="QA-Reject-Result")
        sym = _make_symbol(db_session, symbol="600300")
        _make_daily_bar(db_session, sym.id)
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

        # 新路径 rejection：portfolio 处于阻断态（ADMIN_PAUSED）→ 新买入被治理门禁拦截
        with patch(
            "app.services.portfolio_state_machine._get_status",
            return_value="ADMIN_PAUSED",
        ):
            result = execute_member_source(
                db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
            )

        # 拒绝决策进入 rejected_decisions（新契约 rejection_code）
        assert len(result["rejected_decisions"]) == 1
        rejected = result["rejected_decisions"][0]
        assert rejected["rejection_code"] == "PORTFOLIO_STATE_NEW_BUY_BLOCKED"
        assert "ADMIN_PAUSED" in (rejected["rejection_detail"] or "")
        # 不计入成交列表
        assert len(result["executed_orders"]) == 0
        # 新契约：治理拒绝会建 status=rejected 的留痕订单（不再是“零 SimOrder”）
        rej_orders = (
            db_session.query(SimOrder)
            .filter_by(portfolio_id=p.id, symbol_id=sym.id).all()
        )
        assert len(rej_orders) == 1
        assert rej_orders[0].status == "rejected"

    def test_portfolio_level_block_rejects_all_buys(self, db_session):
        """【Q2·组合级熔断】组合暂停时名下所有新买入一起拒（不做 per-标的 部分放行）。"""
        p = _make_portfolio(db_session, name="QA-Reject-PortfolioLevel")
        sym1 = _make_symbol(db_session, symbol="600310")
        sym2 = _make_symbol(db_session, symbol="600311")
        for _s in (sym1, sym2):
            _make_daily_bar(db_session, _s.id)
            _make_daily_bar(db_session, _s.id, trade_date=date(2026, 7, 18), close=10.1)
            _make_score(db_session, _s.id, action="open")
            _make_member(
                db_session, portfolio_id=p.id, symbol_id=_s.id,
                execution_mode=EXECUTION_AUTO,
            )
        _apply_member_snapshot(db_session, p, [
            {"symbol_id": sym1.id, "execution_mode": EXECUTION_AUTO},
            {"symbol_id": sym2.id, "execution_mode": EXECUTION_AUTO},
        ])

        # 组合处于阻断态 → 新买入按组合级全拒（Q2 决策：无 per-标的 部分放行）
        with patch(
            "app.services.portfolio_state_machine._get_status",
            return_value="ADMIN_PAUSED",
        ):
            result = execute_member_source(
                db_session, portfolio_id=p.id, dry_run=False, trade_date=date(2026, 7, 17),
            )

        rejected_syms = {r["symbol_id"] for r in result["rejected_decisions"]}
        assert rejected_syms == {sym1.id, sym2.id}
        assert all(
            r["rejection_code"] == "PORTFOLIO_STATE_NEW_BUY_BLOCKED"
            for r in result["rejected_decisions"]
        )
        # 组合级熔断：没有任何成交
        assert len(result["executed_orders"]) == 0
