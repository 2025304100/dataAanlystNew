"""黑盒自动模式专项测试（Final.2）。

验证自动交易模式下各种异常的合理处理：
重复调度、任务取消、数据过期、规则失效、涨跌停、T+1、现金不足、部分标的失败

使用 SQLite 内存库（conftest.db_session fixture），AAA 模式，测试独立可运行。
不修改被测代码，仅通过服务层接口验证异常处理机制。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    STATUS_ACTIVE,
    PortfolioMember,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import execute_member_source
from app.services.auto_trade_safety import (
    KLINE_FRESHNESS_HOURS,
    RULE_FRESHNESS_HOURS,
    SCORE_FRESHNESS_HOURS,
    cancel_task,
    check_data_health,
    check_task_cancelled,
    reset_task_cancel,
)
from app.services.auto_trade_task import create_portfolio_auto_trade_task
from app.services.market_rules import check_price_limit, check_t_plus_1
from app.services.sim_accounts import (
    cash_balance,
    ensure_sim_account_seed,
    place_sim_order,
)

pytestmark = pytest.mark.blackbox


# ============================================================================
# 测试辅助
# ============================================================================


def _make_portfolio(
    db_session,
    name="QA-Blackbox-Auto",
    total_capital=100000.0,
    auto_trade_enabled=True,
) -> Portfolio:
    """造一个组合并初始化 CashLedger。"""
    p = Portfolio(
        name=name,
        account_type="simulated",
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
    ensure_sim_account_seed(db_session, p)
    db_session.commit()
    return p


def _make_symbol(
    db_session,
    symbol="800001",
    market="sh",
    asset_type="stock",
    board=None,
    is_st=0,
) -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type=asset_type,
        market=market,
        board=board,
        is_st=is_st,
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id, trade_date, close=10.0):
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
    symbol_id,
    *,
    action="open",
    stage="start",
    trade_date=date(2026, 7, 17),
):
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


def _make_member(
    db_session,
    *,
    portfolio_id,
    symbol_id,
    execution_mode=EXECUTION_AUTO,
):
    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=STATUS_ACTIVE,
        execution_mode=execution_mode,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================================
# 自动模式专项测试
# ============================================================================


class TestAutoModeEdgeCases:
    """Final.2：验证自动模式各种异常的合理处理。"""

    def test_duplicate_schedule_returns_existing_task(self):
        """异常 1：重复调度 → 返回已有任务，不创建新任务。

        验证：已有 queued/running 任务时，create_portfolio_auto_trade_task
        返回现有任务而非创建新任务（幂等防重）。
        """
        # Arrange: 模拟已有 queued 任务
        existing_task = MagicMock()
        existing_task.status = "queued"
        existing_task.id = 42

        # Act
        with patch(
            "app.services.auto_trade_task.list_async_tasks",
            return_value=[existing_task],
        ) as mock_list, patch(
            "app.services.auto_trade_task.create_async_task"
        ) as mock_create, patch(
            "app.services.auto_trade_task._start_worker"
        ) as mock_start:
            result = create_portfolio_auto_trade_task(
                dry_run=False, buy_candidate_limit=10
            )

        # Assert: 返回已有任务，不创建新任务
        assert result is existing_task
        mock_create.assert_not_called()
        mock_start.assert_not_called()

    def test_task_cancel_propagation(self, db_session):
        """异常 2：任务取消 → check_task_cancelled 返回 True，重置后恢复。

        验证：cancel_task 设置取消标志后，check_task_cancelled 能检测到；
        reset_task_cancel 后恢复正常。任务取消可传播到后续组合和订单。
        """
        # Arrange: 确保取消状态干净
        reset_task_cancel()
        assert check_task_cancelled() is False

        # Act 1: 取消任务
        cancel_task("QA test cancel")

        # Assert 1: 取消状态已传播
        assert check_task_cancelled() is True

        # Act 2: 重置
        reset_task_cancel()

        # Assert 2: 恢复正常
        assert check_task_cancelled() is False

    def test_data_expired_blocks_buy(self, db_session):
        """异常 3：数据过期 → check_data_health 返回 healthy=False（fail-closed）。

        验证：K 线数据超过新鲜度阈值时，fail-closed 禁止买入。
        """
        # Arrange
        sym = _make_symbol(db_session, symbol="800010")
        expired_kline = _now_naive() - timedelta(
            hours=KLINE_FRESHNESS_HOURS + 1
        )

        # Act
        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=expired_kline,
        ):
            result = check_data_health(db_session, symbol_id=sym.id)

        # Assert: fail-closed，禁止买入
        assert result.healthy is False
        assert "K线数据过期" in result.reason
        assert result.kline_latest_at == expired_kline

    def test_rule_expired_blocks_buy(self, db_session):
        """异常 4：规则失效 → check_data_health 返回 healthy=False（fail-closed）。

        验证：规则版本超过 7 天新鲜度阈值时，fail-closed 禁止买入。
        """
        # Arrange
        sym = _make_symbol(db_session, symbol="800020")
        expired_rule = _now_naive() - timedelta(
            hours=RULE_FRESHNESS_HOURS + 1
        )

        # Act
        with patch(
            "app.services.auto_trade_safety._get_latest_kline_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_latest_score_time",
            return_value=_now_naive(),
        ), patch(
            "app.services.auto_trade_safety._get_rule_version_time",
            return_value=expired_rule,
        ):
            result = check_data_health(
                db_session, symbol_id=sym.id, rule_version_id=2001
            )

        # Assert: fail-closed
        assert result.healthy is False
        assert "规则版本" in result.reason
        assert "过期" in result.reason
        assert result.rule_version_id == 2001

    def test_price_limit_up_blocks_buy(self, db_session):
        """异常 5：涨停封板 → check_price_limit 禁止买入。

        验证：买入价 >= 涨停价时，无法成交（涨停封板）。
        主板 10% 涨停限制。
        """
        # Arrange: 主板股票，前收盘 10.0，涨停价 11.0
        sym = _make_symbol(db_session, symbol="800030", market="sh", is_st=0)
        prev_close = 10.0
        limit_up_price = 11.0  # 10.0 * 1.10 = 11.0

        # Act
        can_fill, reason = check_price_limit(
            fill_price=limit_up_price,
            prev_close=prev_close,
            symbol=sym,
            side="buy",
        )

        # Assert: 涨停无法买入
        assert can_fill is False
        assert reason is not None
        assert "涨停" in reason

    def test_price_limit_down_blocks_sell(self, db_session):
        """异常 6：跌停封板 → check_price_limit 禁止卖出。

        验证：卖出价 <= 跌停价时，无法成交（跌停封板）。
        主板 10% 跌停限制。
        """
        # Arrange: 主板股票，前收盘 10.0，跌停价 9.0
        sym = _make_symbol(db_session, symbol="800040", market="sh", is_st=0)
        prev_close = 10.0
        limit_down_price = 9.0  # 10.0 * 0.90 = 9.0

        # Act
        can_fill, reason = check_price_limit(
            fill_price=limit_down_price,
            prev_close=prev_close,
            symbol=sym,
            side="sell",
        )

        # Assert: 跌停无法卖出
        assert can_fill is False
        assert reason is not None
        assert "跌停" in reason

    def test_t_plus_1_blocks_same_day_sell(self, db_session):
        """异常 7：T+1 规则 → 当日买入当日不可卖。

        验证：当日买入的股票，T+1 日才能卖出。
        check_t_plus_1 查到今日买入量后，可卖余额不足时返回 (False, reason)。
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-T1-Port")
        sym = _make_symbol(db_session, symbol="800050")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 22), close=10.0)

        # 当日买入 1000 股（enforce_rules=False 跳过下单时的 T+1 检查）
        place_sim_order(
            db_session,
            portfolio=p,
            symbol=sym,
            side="buy",
            quantity=1000,
            price=10.0,
            order_type="limit",
            enforce_rules=False,
            apply_fees=False,
            note="T+1 test buy",
        )
        db_session.commit()

        # Act: 尝试当日卖出 100 股
        can_sell, reason = check_t_plus_1(
            db_session,
            portfolio_id=p.id,
            symbol_id=sym.id,
            sell_quantity=100,
        )

        # Assert: T+1 禁止当日卖出（今日买入 1000 股不可卖，可卖 0 股）
        assert can_sell is False
        assert reason is not None
        assert "T+1" in reason

    def test_insufficient_cash_rejected(self, db_session):
        """异常 8：现金不足 → place_sim_order 拒绝下单。

        验证：买入金额超过现金余额时，place_sim_order 抛出 HTTPException 400。
        """
        # Arrange: 小资金组合（1000 元）
        p = _make_portfolio(
            db_session, name="QA-NoCash-Port", total_capital=1000.0
        )
        sym = _make_symbol(db_session, symbol="800060")
        _make_daily_bar(db_session, sym.id, date(2026, 7, 22), close=50.0)
        # 买 100 股 @ 50.0 = 5000 > 1000 现金

        # Act & Assert: 现金不足被拒绝
        with pytest.raises(HTTPException) as exc_info:
            place_sim_order(
                db_session,
                portfolio=p,
                symbol=sym,
                side="buy",
                quantity=100,
                price=50.0,
                order_type="limit",
                enforce_rules=False,
                apply_fees=False,
                note="Insufficient cash test",
            )

        assert exc_info.value.status_code == 400
        assert "cash" in exc_info.value.detail.lower()

    def test_partial_symbol_failure_isolated(self, db_session):
        """异常 9：部分标的失败 → 其他标的不受影响。

        验证：两个 auto 成员，一个数据健康（有 DailyBar + Score），
        一个数据缺失（有 Score 但无 DailyBar）。
        execute_member_source 应隔离失败，健康标的仍生成有效决策。
        """
        # Arrange: 确保取消状态干净
        reset_task_cancel()
        p = _make_portfolio(db_session, name="QA-Partial-Port")

        # 标的 A：数据健康（有 DailyBar + Score）
        sym_a = _make_symbol(db_session, symbol="800070")
        _make_daily_bar(db_session, sym_a.id, date(2026, 7, 22), close=10.0)
        _make_score(
            db_session,
            sym_a.id,
            action="open",
            stage="start",
            trade_date=date(2026, 7, 22),
        )
        _make_member(db_session, portfolio_id=p.id, symbol_id=sym_a.id)

        # 标的 B：数据缺失（有 Score 但无 DailyBar → check_data_health fail-closed）
        sym_b = _make_symbol(db_session, symbol="800080")
        _make_score(
            db_session,
            sym_b.id,
            action="open",
            stage="start",
            trade_date=date(2026, 7, 22),
        )
        _make_member(db_session, portfolio_id=p.id, symbol_id=sym_b.id)

        # Act
        result = execute_member_source(
            db_session, portfolio_id=p.id, dry_run=True
        )

        # Assert: 任务未被取消，无异常崩溃
        assert result["skipped_due_to_cancel"] is False
        assert len(result["errors"]) == 0

        # 所有决策（含被拒绝的）都应该出现
        all_decisions = (
            result["buy_decisions"] + result["rejected_decisions"]
        )
        assert len(all_decisions) >= 2

        # 标的 B（数据缺失）被拒绝
        rejected = result["rejected_decisions"]
        assert len(rejected) >= 1
        rejected_symbols = {d["symbol_id"] for d in rejected}
        assert sym_b.id in rejected_symbols
        # 拒绝码为 BLOCKED
        for d in rejected:
            if d["symbol_id"] == sym_b.id:
                assert d["rejection_code"] == "BLOCKED"

        # 标的 A（数据健康）未被拒绝，进入 buy_decisions
        healthy = result["buy_decisions"]
        assert len(healthy) >= 1
        healthy_symbols = {d["symbol_id"] for d in healthy}
        assert sym_a.id in healthy_symbols
        # 无拒绝码
        for d in healthy:
            if d["symbol_id"] == sym_a.id:
                assert d["rejection_code"] is None
