"""白盒测试 - 组合自动交易（P2-3）。

守护 run_auto_trade 与调度入口的关键行为，防止重构/优化时退化：
1. 错误场景：组合不存在/非模拟/未开启 auto_trade_enabled → ValueError
2. 卖出侧：action=exit 全卖 / action=reduce 卖一半（归一化手数） / action=hold 跳过 / 无 Score 跳过
3. 买入侧：action=open + can_open=True 生成买入计划 / can_open=False 记录 blocked_reasons / 已持仓跳过
4. dry_run=True 只返回计划不实际下单（无 SimOrder 创建）
5. dry_run=False 实际下单（mock place_sim_order 验证调用参数 + last_run_at 更新）
6. last_run_at：dry_run=True 不更新 / dry_run=False 更新
7. API 端点：200 成功 / 404 不存在 / 400 非模拟 / 409 未开启自动交易
8. create_portfolio_auto_trade_task 去重（已有 queued/running 时返回现有任务）
9. scheduled_tasks 接入：TASK_DEFINITIONS / validate_task_payload / _dispatch_task 路由
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.auto_trade_task import (
    TASK_TYPE,
    create_portfolio_auto_trade_task,
    run_auto_trade,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================

def _make_portfolio(
    db_session,
    name="QA-AutoTrade",
    account_type="simulated",
    total_capital=100000.0,
    auto_trade_enabled=True,
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


def _make_symbol(db_session, symbol="600010", name="测试标的", market="sh", asset_type="stock") -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type=asset_type, market=market)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id: int, trade_date: date, close: float = 10.0):
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
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=70.0,
        quality_grade="B",
        timing_score=65.0,
        stage=stage,
        action=action,
        priority_score=75.0,
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


def _make_scan_run_with_candidate(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    rank_no: int = 1,
    priority_score: float = 80.0,
    stage: str = "start",
    action: str = "open",
) -> tuple[ScanRun, ScanResult]:
    run = ScanRun(
        portfolio_id=portfolio_id,
        run_name=f"QA-Scan-{datetime.now().strftime('%H%M%S%f')}",
        scope_snapshot="cn-stock",
        status="done",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    result = ScanResult(
        scan_run_id=run.id,
        symbol_id=symbol_id,
        result_type="executable",
        rank_no=rank_no,
        quality_score=70.0,
        timing_score=65.0,
        priority_score=priority_score,
        stage=stage,
        action=action,
        recommended_position_pct=0.2,
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(result)
    return run, result


def _make_active_rule(db_session, portfolio_id: int) -> PortfolioRule:
    """造一个激活的风控规则，允许 start 阶段买入 stock。"""
    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="QA-Rule",
        max_single_position_pct=0.2,
        max_sector_position_pct=0.4,
        max_stock_position_pct=0.6,
        max_etf_position_pct=0.3,
        max_loss_per_trade_pct=0.02,
        max_open_positions=10,
        stage_limits_json=json.dumps({
            "stock": {"start": 0.2, "pullback": 0.15, "breakout": 0.1},
            "etf": {"start": 0.2, "pullback": 0.15, "breakout": 0.1},
        }),
        is_active=1,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


# ============================================================================
# 1. 错误场景
# ============================================================================

class TestAutoTradeErrors:
    """守护前置校验的边界处理。"""

    def test_portfolio_not_found_raises(self, db_session):
        """组合不存在 → ValueError('not found')。"""
        with pytest.raises(ValueError, match="not found"):
            run_auto_trade(db_session, portfolio_id=99999, dry_run=True)

    def test_portfolio_not_simulated_raises(self, db_session):
        """组合不是 simulated → ValueError('not simulated')。"""
        p = _make_portfolio(db_session, name="QA-Real", account_type="real")
        with pytest.raises(ValueError, match="not simulated"):
            run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

    def test_auto_trade_disabled_raises(self, db_session):
        """auto_trade_enabled=0 → ValueError('auto_trade_enabled')。"""
        p = _make_portfolio(db_session, name="QA-Disabled", auto_trade_enabled=False)
        with pytest.raises(ValueError, match="auto_trade_enabled"):
            run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)


# ============================================================================
# 2. 卖出侧
# ============================================================================

class TestSellSide:
    """守护卖出侧的信号识别与数量计算。"""

    def test_exit_action_sells_all(self, db_session):
        """action=exit → sell_quantity = held_quantity（全部卖出）。"""
        p = _make_portfolio(db_session, name="QA-SellExit")
        sym = _make_symbol(db_session, symbol="600011")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="exit", stage="overheat")
        _make_position(db_session, p.id, sym.id, quantity=100, avg_cost=10.0, latest_price=10.0)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["sells"]) == 1
        sell = result["sells"][0]
        assert sell["symbol"] == "600011"
        assert sell["action"] == "exit"
        assert sell["sell_quantity"] == 100
        assert sell["held_quantity"] == 100
        assert "exit" in sell["reason"]
        assert sell["executed"] is False  # dry_run
        assert len(result["buys"]) == 0

    def test_reduce_action_sells_half_normalized(self, db_session):
        """action=reduce → sell_quantity = normalize(held/2)。

        A 股 100 股/手：held=1000 → half=500 → normalized=500。
        """
        p = _make_portfolio(db_session, name="QA-SellReduce")
        sym = _make_symbol(db_session, symbol="600012")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="reduce", stage="pullback")
        _make_position(db_session, p.id, sym.id, quantity=1000, avg_cost=10.0, latest_price=10.0)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["sells"]) == 1
        sell = result["sells"][0]
        assert sell["action"] == "reduce"
        assert sell["sell_quantity"] == 500  # 1000/2=500，已是 100 的倍数
        assert sell["held_quantity"] == 1000
        assert "reduce" in sell["reason"]

    def test_hold_action_skipped(self, db_session):
        """action=hold → 不在 _SELL_ACTIONS，跳过。"""
        p = _make_portfolio(db_session, name="QA-SellHold")
        sym = _make_symbol(db_session, symbol="600013")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="hold", stage="start")
        _make_position(db_session, p.id, sym.id, quantity=100)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["sells"]) == 0
        assert len(result["buys"]) == 0

    def test_no_score_skips_position(self, db_session):
        """持仓无 Score → 跳过（不抛异常）。"""
        p = _make_portfolio(db_session, name="QA-SellNoScore")
        sym = _make_symbol(db_session, symbol="600014")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        # 不创建 Score
        _make_position(db_session, p.id, sym.id, quantity=100)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["sells"]) == 0
        # 无 ScanResult 也无买入
        assert len(result["buys"]) == 0

    def test_dry_run_does_not_create_sim_order(self, db_session):
        """dry_run=True → 不创建 SimOrder。"""
        p = _make_portfolio(db_session, name="QA-SellDryRun")
        sym = _make_symbol(db_session, symbol="600015")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="exit", stage="overheat")
        _make_position(db_session, p.id, sym.id, quantity=100)

        before = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()
        run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)
        after = db_session.query(SimOrder).filter_by(portfolio_id=p.id).count()

        assert before == 0
        assert after == 0  # dry_run 不下单


# ============================================================================
# 3. 买入侧
# ============================================================================

class TestBuySide:
    """守护买入侧的候选遍历、风控与计划生成。"""

    def test_buy_open_with_active_rule(self, db_session):
        """action=open + can_open=True → 生成买入计划。"""
        p = _make_portfolio(db_session, name="QA-BuyOpen")
        sym = _make_symbol(db_session, symbol="600020")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="open", stage="start")
        _make_scan_run_with_candidate(db_session, p.id, sym.id, action="open", stage="start")
        _make_active_rule(db_session, p.id)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["buys"]) == 1
        buy = result["buys"][0]
        assert buy["symbol"] == "600020"
        assert buy["action"] == "open"
        assert buy["can_open"] is True
        # recommended_pct = min(0.2, 0.2, 0.4, 0.6, 0.9) = 0.2 → amount = 100000 * 0.2 = 20000
        # buy_qty = 20000 // 10 = 2000（A 股 100/手，2000 已是倍数）
        assert buy["buy_quantity"] == 2000
        assert buy["recommended_amount"] == 20000.0
        assert buy["executed"] is False  # dry_run

    def test_buy_blocked_without_active_rule(self, db_session):
        """无 active rule → can_open=False，记录 blocked_reasons=['no_active_rule']。"""
        p = _make_portfolio(db_session, name="QA-BuyBlocked")
        sym = _make_symbol(db_session, symbol="600021")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="open", stage="start")
        _make_scan_run_with_candidate(db_session, p.id, sym.id)
        # 不创建 PortfolioRule

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["buys"]) == 1
        buy = result["buys"][0]
        assert buy["can_open"] is False
        assert buy["decision"] == "blocked"
        assert "no_active_rule" in buy["blocked_reasons"]
        assert buy["executed"] is False

    def test_buy_skips_existing_position(self, db_session):
        """已持仓的候选 → 跳过（不生成买入计划，避免加仓逻辑）。"""
        p = _make_portfolio(db_session, name="QA-BuySkip")
        sym = _make_symbol(db_session, symbol="600022")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="open", stage="start")
        _make_scan_run_with_candidate(db_session, p.id, sym.id)
        _make_active_rule(db_session, p.id)
        # 已有持仓
        _make_position(db_session, p.id, sym.id, quantity=100)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["buys"]) == 0  # 跳过已持仓

    def test_buy_skips_when_action_not_in_buy_actions(self, db_session):
        """候选 Score.action=hold → 不在 _BUY_ACTIONS，跳过。"""
        p = _make_portfolio(db_session, name="QA-BuyHold")
        sym = _make_symbol(db_session, symbol="600023")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="hold", stage="start")
        _make_scan_run_with_candidate(db_session, p.id, sym.id, action="hold")
        _make_active_rule(db_session, p.id)

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["buys"]) == 0

    def test_buy_skips_when_no_scan_run(self, db_session):
        """无 ScanRun → 买入侧为空（不抛异常）。"""
        p = _make_portfolio(db_session, name="QA-BuyNoScan")
        sym = _make_symbol(db_session, symbol="600024")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="open", stage="start")
        _make_active_rule(db_session, p.id)
        # 不创建 ScanRun

        result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        assert len(result["buys"]) == 0


# ============================================================================
# 4. dry_run=False 实际下单（mock place_sim_order 避免 T+1/涨跌停干扰）
# ============================================================================

class TestActualExecution:
    """守护 dry_run=False 时实际下单的行为。"""

    def test_dry_run_false_calls_place_sim_order_for_sell(self, db_session):
        """dry_run=False 卖出 → place_sim_order 被调用，order_id 记录到计划。"""
        p = _make_portfolio(db_session, name="QA-ExecSell")
        sym = _make_symbol(db_session, symbol="600030")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="exit", stage="overheat")
        _make_position(db_session, p.id, sym.id, quantity=100)

        fake_order = SimpleNamespace(id=999, filled_price=10.05, fee=5.0)
        fake_trade = SimpleNamespace(id=888)
        with patch(
            "app.services.auto_trade_task.place_sim_order",
            return_value=(fake_order, fake_trade),
        ) as mock_place:
            result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=False)

        assert len(result["sells"]) == 1
        sell = result["sells"][0]
        assert sell["executed"] is True
        assert sell["order_id"] == 999
        assert sell["filled_price"] == 10.05
        assert sell["fee"] == 5.0
        mock_place.assert_called_once()
        # 验证调用参数
        call_kwargs = mock_place.call_args.kwargs
        assert call_kwargs["side"] == "sell"
        assert call_kwargs["quantity"] == 100
        assert call_kwargs["enforce_rules"] is True
        assert call_kwargs["apply_fees"] is True

    def test_dry_run_false_calls_place_sim_order_for_buy(self, db_session):
        """dry_run=False 买入 → place_sim_order 被调用。"""
        p = _make_portfolio(db_session, name="QA-ExecBuy")
        sym = _make_symbol(db_session, symbol="600031")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 17), close=10.0)
        _make_score(db_session, sym.id, action="open", stage="start")
        _make_scan_run_with_candidate(db_session, p.id, sym.id)
        _make_active_rule(db_session, p.id)

        fake_order = SimpleNamespace(id=998, filled_price=10.1, fee=3.0)
        fake_trade = SimpleNamespace(id=887)
        with patch(
            "app.services.auto_trade_task.place_sim_order",
            return_value=(fake_order, fake_trade),
        ) as mock_place:
            result = run_auto_trade(db_session, portfolio_id=p.id, dry_run=False)

        assert len(result["buys"]) == 1
        buy = result["buys"][0]
        assert buy["executed"] is True
        assert buy["order_id"] == 998
        assert buy["filled_price"] == 10.1
        assert buy["fee"] == 3.0
        mock_place.assert_called_once()
        call_kwargs = mock_place.call_args.kwargs
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["quantity"] == 2000


# ============================================================================
# 5. last_run_at 更新
# ============================================================================

class TestLastRunAt:
    """守护 auto_trade_last_run_at 的更新时机。"""

    def test_dry_run_true_does_not_update_last_run_at(self, db_session):
        """dry_run=True → last_run_at 不更新。"""
        p = _make_portfolio(db_session, name="QA-LastRunDry")
        before = p.auto_trade_last_run_at
        assert before is None

        run_auto_trade(db_session, portfolio_id=p.id, dry_run=True)

        db_session.refresh(p)
        assert p.auto_trade_last_run_at is None  # 未更新

    def test_dry_run_false_updates_last_run_at(self, db_session):
        """dry_run=False → last_run_at 更新为当前时间。"""
        p = _make_portfolio(db_session, name="QA-LastRunExec")
        before = p.auto_trade_last_run_at
        assert before is None

        # 至少有一笔操作才会触发更新（即使无信号也会执行到末尾）
        run_auto_trade(db_session, portfolio_id=p.id, dry_run=False)

        db_session.refresh(p)
        assert p.auto_trade_last_run_at is not None
        # 时间戳应在最近 5 秒内
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = now - p.auto_trade_last_run_at
        assert abs(delta.total_seconds()) < 5


# ============================================================================
# 6. API 端点
# ============================================================================

class TestAutoTradeEndpoint:
    """守护 POST /portfolios/{id}/auto-trade/execute 的状态码映射。"""

    def test_200_success(self, db_session):
        """正常 dry_run 调用 → 200。"""
        from app.main import app

        p = _make_portfolio(db_session, name="QA-API-OK", auto_trade_enabled=True)

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        try:
            resp = client.post(
                f"/api/v1/portfolios/{p.id}/auto-trade/execute",
                json={"dry_run": True, "buy_candidate_limit": 10},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["portfolio_id"] == p.id
            assert body["dry_run"] is True
            assert "sells" in body
            assert "buys" in body
            assert "errors" in body
            assert "executed_at" in body
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_404_not_found(self, db_session):
        """组合不存在 → 404。"""
        from app.main import app

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        try:
            resp = client.post(
                "/api/v1/portfolios/99999/auto-trade/execute",
                json={"dry_run": True},
            )
            assert resp.status_code == 404
            assert "not found" in resp.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_400_not_simulated(self, db_session):
        """组合非 simulated → 400。"""
        from app.main import app

        p = _make_portfolio(db_session, name="QA-API-Real", account_type="real")

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        try:
            resp = client.post(
                f"/api/v1/portfolios/{p.id}/auto-trade/execute",
                json={"dry_run": True},
            )
            assert resp.status_code == 400
            assert "not simulated" in resp.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_409_auto_trade_disabled(self, db_session):
        """auto_trade_enabled=0 → 409。"""
        from app.main import app

        p = _make_portfolio(db_session, name="QA-API-Disabled", auto_trade_enabled=False)

        def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        try:
            resp = client.post(
                f"/api/v1/portfolios/{p.id}/auto-trade/execute",
                json={"dry_run": True},
            )
            assert resp.status_code == 409
            assert "auto_trade_enabled" in resp.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 7. create_portfolio_auto_trade_task 去重
# ============================================================================

class TestCreateAutoTradeTask:
    """守护任务去重与启动逻辑。"""

    def test_returns_existing_queued_task(self):
        """已有 queued 任务 → 返回现有任务，不创建新任务。"""
        existing_task = MagicMock()
        existing_task.status = "queued"
        with patch(
            "app.services.auto_trade_task.list_async_tasks",
            return_value=[existing_task],
        ) as mock_list, patch(
            "app.services.auto_trade_task.create_async_task"
        ) as mock_create, patch(
            "app.services.auto_trade_task._start_worker"
        ) as mock_start:
            result = create_portfolio_auto_trade_task(dry_run=False, buy_candidate_limit=10)

        assert result is existing_task
        mock_create.assert_not_called()
        mock_start.assert_not_called()

    def test_returns_existing_running_task(self):
        """已有 running 任务 → 返回现有任务。"""
        existing_task = MagicMock()
        existing_task.status = "running"
        with patch(
            "app.services.auto_trade_task.list_async_tasks",
            return_value=[existing_task],
        ), patch(
            "app.services.auto_trade_task.create_async_task"
        ) as mock_create, patch(
            "app.services.auto_trade_task._start_worker"
        ) as mock_start:
            result = create_portfolio_auto_trade_task(dry_run=False)

        assert result is existing_task
        mock_create.assert_not_called()
        mock_start.assert_not_called()

    def test_creates_new_task_when_none_running(self):
        """无 queued/running 任务 → 创建新任务并启动 worker。"""
        new_task = MagicMock()
        new_task.id = "new-auto-trade-task-id"
        new_task.status = "queued"
        with patch(
            "app.services.auto_trade_task.list_async_tasks",
            return_value=[],
        ), patch(
            "app.services.auto_trade_task.create_async_task",
            return_value=new_task,
        ) as mock_create, patch(
            "app.services.auto_trade_task._start_worker"
        ) as mock_start:
            result = create_portfolio_auto_trade_task(dry_run=False, buy_candidate_limit=15)

        assert result is new_task
        mock_create.assert_called_once_with(
            TASK_TYPE, {"dry_run": False, "buy_candidate_limit": 15}
        )
        mock_start.assert_called_once()


# ============================================================================
# 8. scheduled_tasks 接入
# ============================================================================

class TestScheduledTasksIntegration:
    """守护 portfolio_auto_trade 在调度框架内的注册与分发。"""

    def test_task_definition_registered(self):
        """TASK_DEFINITIONS 应包含 portfolio_auto_trade。"""
        from app.services.scheduled_tasks import TASK_DEFINITIONS

        assert "portfolio_auto_trade" in TASK_DEFINITIONS
        definition = TASK_DEFINITIONS["portfolio_auto_trade"]
        assert "name" in definition
        assert "description" in definition
        assert definition["default_payload"] == {"dry_run": False, "buy_candidate_limit": 10}

    def test_validate_payload_accepts_valid_input(self):
        """合法 payload 通过校验。"""
        from app.services.scheduled_tasks import validate_task_payload

        result = validate_task_payload(
            "portfolio_auto_trade", {"dry_run": True, "buy_candidate_limit": 20}
        )
        assert result == {"dry_run": True, "buy_candidate_limit": 20}

    def test_validate_payload_rejects_invalid_limit(self):
        """buy_candidate_limit 越界 → ValueError。"""
        from app.services.scheduled_tasks import validate_task_payload

        with pytest.raises(ValueError, match="buy_candidate_limit"):
            validate_task_payload(
                "portfolio_auto_trade", {"buy_candidate_limit": 0}
            )
        with pytest.raises(ValueError, match="buy_candidate_limit"):
            validate_task_payload(
                "portfolio_auto_trade", {"buy_candidate_limit": 100}
            )

    def test_dispatch_routes_to_create_auto_trade_task(self):
        """_dispatch_task 应路由到 create_portfolio_auto_trade_task。"""
        from app.services.scheduled_tasks import _dispatch_task
        from app.models.scheduled_task import ScheduledTask

        item = ScheduledTask(
            name="QA-Dispatch",
            task_type="portfolio_auto_trade",
            frequency="daily",
            time_of_day="14:55",
            weekdays_json="[]",
            interval_minutes=None,
            timezone="Asia/Shanghai",
            payload_json=json.dumps({"dry_run": False, "buy_candidate_limit": 10}),
            enabled=1,
        )

        fake_task = SimpleNamespace(id="dispatched-task-id", status="queued", message="queued")
        with patch(
            "app.services.auto_trade_task.create_portfolio_auto_trade_task",
            return_value=fake_task,
        ) as mock_create:
            source, task = _dispatch_task(item)

        assert source == "async"
        assert task is fake_task
        mock_create.assert_called_once_with(dry_run=False, buy_candidate_limit=10)

    def test_default_schedule_seeded_disabled(self, db_session):
        """默认调度应包含 portfolio_auto_trade 且 enabled=False。"""
        from app.services import scheduled_tasks
        from app.models.scheduled_task import ScheduledTask

        scheduled_tasks.seed_default_schedules(db_session)
        db_session.commit()

        item = db_session.query(ScheduledTask).filter_by(
            task_type="portfolio_auto_trade"
        ).one()
        assert item.enabled == 0  # 默认关闭，用户需主动开启
        assert item.next_run_at is None
        assert item.time_of_day == "14:55"
