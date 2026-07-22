"""白盒测试 - 组合绩效计算服务（P1-1）。

守护 compute_portfolio_performance 的关键行为，防止重构/优化时退化：
1. 无 snapshot 时所有指标为 0，equity_curve 为空
2. 有 snapshot 但无卖出 trade：win_rate/profit_factor/trade_count 为 0，但 total_return/max_drawdown/sharpe 正常
3. 完整场景：snapshot + 卖出 trade，所有指标正确计算
4. start_date/end_date 过滤同时作用于 snapshot 和 trade
5. 非模拟组合抛 ValueError（由调用方转 400）
6. 不存在的组合抛 ValueError（由调用方转 404）
7. equity_curve 格式正确（date 字符串 + equity float）
8. hold_days 从同 symbol 买入 trade 推算
9. snapshot_count 与实际 snapshot 数量一致
10. initial_capital 来自 portfolio.total_capital
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from app.models.portfolio import Portfolio
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.portfolio_equity_snapshot import upsert_snapshot
from app.services.portfolio_performance import compute_portfolio_performance
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================

def _make_portfolio(db_session, name="QA-Perf", account_type="simulated", total_capital=100000.0) -> Portfolio:
    """造一个组合并初始化 CashLedger。"""
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    if account_type == "simulated":
        ensure_sim_account_seed(db_session, p)
        db_session.commit()
    return p


def _make_symbol(db_session, symbol="600010", name="测试标的") -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type="stock", market="sh")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _seed_snapshots(db_session, portfolio_id, start_equity=100000.0, days=5, start_date=date(2026, 7, 15)):
    """造 N 天快照，total_equity 线性增长（每日 +100）。"""
    for i in range(days):
        snap_date = start_date + timedelta(days=i)
        equity = start_equity + i * 100
        upsert_snapshot(
            db_session,
            portfolio_id=portfolio_id,
            snapshot_date=snap_date,
            cash_balance=equity - i * 50,
            market_value=float(i * 50),
            total_equity=equity,
            realized_pnl=float(i * 10),
            unrealized_pnl=float(i * 40),
            position_count=1 if i > 0 else 0,
        )
    db_session.commit()


def _make_sell_trade(db_session, portfolio_id, symbol_id, realized_pnl, created_at, buy_created_at=None):
    """造一笔卖出 trade（带 realized_pnl）+ 可选的买入 trade（用于 hold_days 推算）。"""
    if buy_created_at is not None:
        buy_order = SimOrder(
            portfolio_id=portfolio_id, symbol_id=symbol_id, side="buy", order_type="market",
            quantity=100, submitted_price=10.0, status="filled",
            filled_quantity=100, filled_price=10.0, filled_amount=1000.0, fee=0.0,
            filled_at=buy_created_at,
        )
        db_session.add(buy_order)
        db_session.flush()
        buy_trade = SimTrade(
            portfolio_id=portfolio_id, symbol_id=symbol_id, order_id=buy_order.id,
            side="buy", quantity=100, price=10.0, amount=1000.0, fee=0.0,
            realized_pnl=None, created_at=buy_created_at,
        )
        db_session.add(buy_trade)

    sell_order = SimOrder(
        portfolio_id=portfolio_id, symbol_id=symbol_id, side="sell", order_type="market",
        quantity=100, submitted_price=11.0, status="filled",
        filled_quantity=100, filled_price=11.0, filled_amount=1100.0, fee=0.0,
        filled_at=created_at,
    )
    db_session.add(sell_order)
    db_session.flush()
    sell_trade = SimTrade(
        portfolio_id=portfolio_id, symbol_id=symbol_id, order_id=sell_order.id,
        side="sell", quantity=100, price=11.0, amount=1100.0, fee=0.0,
        realized_pnl=realized_pnl, created_at=created_at,
    )
    db_session.add(sell_trade)
    db_session.commit()


# ============================================================================
# 1. 无 snapshot 边界
# ============================================================================

class TestNoSnapshot:
    """守护无 snapshot 时的边界处理。"""

    def test_empty_portfolio_returns_zero_stats(self, db_session):
        """【P1-1 测试】无 snapshot 时所有指标为 0，equity_curve 为空。"""
        p = _make_portfolio(db_session, name="QA-Empty")
        result = compute_portfolio_performance(db_session, p.id)

        assert result["snapshot_count"] == 0
        assert result["equity_curve"] == []
        assert result["date_range"] == {"start": None, "end": None}
        stats = result["stats"]
        assert stats["total_return"] == 0.0
        assert stats["max_drawdown"] == 0.0
        assert stats["sharpe_ratio"] == 0.0
        assert stats["win_rate"] == 0.0
        assert stats["trade_count"] == 0

    def test_initial_capital_from_portfolio(self, db_session):
        """【P1-1 测试】initial_capital 来自 portfolio.total_capital。"""
        p = _make_portfolio(db_session, name="QA-Cap", total_capital=250000.0)
        result = compute_portfolio_performance(db_session, p.id)
        assert result["initial_capital"] == 250000.0


# ============================================================================
# 2. 有 snapshot 无 trade
# ============================================================================

class TestSnapshotWithoutTrades:
    """守护有 snapshot 但无卖出 trade 的场景。"""

    def test_equity_metrics_computed_without_trade_stats(self, db_session):
        """【P1-1 测试】无卖出 trade 时 total_return/max_drawdown/sharpe 正常，win_rate/trade_count 为 0。"""
        p = _make_portfolio(db_session, name="QA-NoTrades", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=5)

        result = compute_portfolio_performance(db_session, p.id)

        assert result["snapshot_count"] == 5
        assert len(result["equity_curve"]) == 5
        stats = result["stats"]
        # equity 从 100000 涨到 100400，total_return = 400
        assert stats["total_return"] == 400.0
        assert stats["total_return_pct"] == pytest.approx(0.004, abs=1e-4)
        # 无卖出 trade
        assert stats["trade_count"] == 0
        assert stats["win_rate"] == 0.0
        assert stats["profit_factor"] == 0.0
        # sharpe 应该是有限数（5 个点有 4 个收益率）
        assert math.isfinite(stats["sharpe_ratio"])


# ============================================================================
# 3. 完整场景：snapshot + trade
# ============================================================================

class TestFullScenario:
    """守护 snapshot + 卖出 trade 的完整绩效计算。"""

    def test_full_performance_with_profitable_trade(self, db_session):
        """【P1-1 测试】盈利卖出 trade：win_rate=1.0, profit_factor=999.0。"""
        p = _make_portfolio(db_session, name="QA-Full", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600020")
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        # 买入 7-15，卖出 7-17，盈利 500
        _make_sell_trade(
            db_session, p.id, sym.id, realized_pnl=500.0,
            created_at=datetime(2026, 7, 17, 10, 0),
            buy_created_at=datetime(2026, 7, 15, 10, 0),
        )

        result = compute_portfolio_performance(db_session, p.id)
        stats = result["stats"]

        assert stats["trade_count"] == 1
        assert stats["win_rate"] == 1.0  # 1 笔全胜
        assert stats["profit_factor"] == 999.0  # 无亏损 → 999.0
        # hold_days = (7-17) - (7-15) = 2 天
        assert stats["avg_holding_days"] == 2.0

    def test_full_performance_with_loss_trade(self, db_session):
        """【P1-1 测试】亏损卖出 trade：win_rate=0.0, profit_factor=0.0。"""
        p = _make_portfolio(db_session, name="QA-Loss", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600030")
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        _make_sell_trade(
            db_session, p.id, sym.id, realized_pnl=-300.0,
            created_at=datetime(2026, 7, 17, 10, 0),
            buy_created_at=datetime(2026, 7, 16, 10, 0),
        )

        result = compute_portfolio_performance(db_session, p.id)
        stats = result["stats"]

        assert stats["trade_count"] == 1
        assert stats["win_rate"] == 0.0
        assert stats["profit_factor"] == 0.0  # 无盈利 → 0
        assert stats["avg_holding_days"] == 1.0


# ============================================================================
# 4. 日期过滤
# ============================================================================

class TestDateFiltering:
    """守护 start_date/end_date 同时作用于 snapshot 和 trade。"""

    def test_start_date_filters_both_snapshot_and_trade(self, db_session):
        """【P1-1 测试】start_date 过滤掉早期 snapshot 和 trade。"""
        p = _make_portfolio(db_session, name="QA-Filter", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600040")
        # 7-15 ~ 7-19 共 5 天 snapshot
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=5)
        # 7-16 的卖出 trade
        _make_sell_trade(
            db_session, p.id, sym.id, realized_pnl=200.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            buy_created_at=datetime(2026, 7, 15, 10, 0),
        )

        # 只看 7-17 之后
        result = compute_portfolio_performance(
            db_session, p.id, start_date=date(2026, 7, 17)
        )

        # snapshot 只剩 7-17, 7-18, 7-19
        assert result["snapshot_count"] == 3
        assert result["date_range"]["start"] == "2026-07-17"
        # trade 被过滤（7-16 < 7-17）
        assert result["stats"]["trade_count"] == 0

    def test_end_date_filters_both_snapshot_and_trade(self, db_session):
        """【P1-1 测试】end_date 过滤掉晚期 snapshot 和 trade。"""
        p = _make_portfolio(db_session, name="QA-FilterEnd", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600050")
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=5)
        _make_sell_trade(
            db_session, p.id, sym.id, realized_pnl=200.0,
            created_at=datetime(2026, 7, 18, 10, 0),
            buy_created_at=datetime(2026, 7, 15, 10, 0),
        )

        # 只看 7-17 之前
        result = compute_portfolio_performance(
            db_session, p.id, end_date=date(2026, 7, 17)
        )

        # snapshot 只剩 7-15, 7-16, 7-17
        assert result["snapshot_count"] == 3
        assert result["date_range"]["end"] == "2026-07-17"
        # trade 被过滤（7-18 > 7-17）
        assert result["stats"]["trade_count"] == 0


# ============================================================================
# 5-6. 错误场景
# ============================================================================

class TestErrorScenarios:
    """守护错误场景抛 ValueError（由 API 层转 HTTP 错误码）。"""

    def test_portfolio_not_found_raises_value_error(self, db_session):
        """【P1-1 测试】不存在的组合抛 ValueError。"""
        with pytest.raises(ValueError, match="Portfolio not found"):
            compute_portfolio_performance(db_session, 99999)

    def test_real_portfolio_does_not_raise_in_service(self, db_session):
        """【P1-1 测试】服务层不校验 account_type（由 API 层转 400），但能正常计算。

        注意：服务层故意不抛 account_type 错误，避免与 API 层重复校验。
        API 层 (get_portfolio_performance_route) 负责返回 400。
        """
        p = _make_portfolio(db_session, name="QA-Real", account_type="real")
        # 服务层不应抛异常，但会返回空结果（无 snapshot）
        result = compute_portfolio_performance(db_session, p.id)
        assert result["snapshot_count"] == 0


# ============================================================================
# 7-8. 数据格式与 hold_days
# ============================================================================

class TestDataFormat:
    """守护 equity_curve 格式与 hold_days 推算。"""

    def test_equity_curve_format(self, db_session):
        """【P1-1 测试】equity_curve 每项有 date 字符串和 equity float。"""
        p = _make_portfolio(db_session, name="QA-Format", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        result = compute_portfolio_performance(db_session, p.id)

        assert len(result["equity_curve"]) == 3
        for point in result["equity_curve"]:
            assert "date" in point
            assert "equity" in point
            assert isinstance(point["date"], str)
            assert isinstance(point["equity"], float)
            # 日期格式 YYYY-MM-DD
            assert len(point["date"]) == 10

    def test_hold_days_without_buy_trade_is_zero(self, db_session):
        """【P1-1 测试】卖出 trade 无对应买入 trade 时 hold_days=0（不抛异常）。"""
        p = _make_portfolio(db_session, name="QA-NoBuy", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600060")
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=2)

        # 只造卖出 trade，无买入
        _make_sell_trade(
            db_session, p.id, sym.id, realized_pnl=100.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            buy_created_at=None,  # 无买入
        )

        result = compute_portfolio_performance(db_session, p.id)
        stats = result["stats"]

        assert stats["trade_count"] == 1
        assert stats["avg_holding_days"] == 0.0  # 无买入 → 0


# ============================================================================
# 9-10. snapshot_count 与 initial_capital
# ============================================================================

class TestCountersAndCapital:
    """守护 snapshot_count 与 initial_capital 的正确性。"""

    def test_snapshot_count_matches_actual(self, db_session):
        """【P1-1 测试】snapshot_count 等于实际 snapshot 数量。"""
        p = _make_portfolio(db_session, name="QA-Count", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=10)

        result = compute_portfolio_performance(db_session, p.id)

        assert result["snapshot_count"] == 10
        assert len(result["equity_curve"]) == 10

    def test_snapshot_limit_truncates(self, db_session):
        """【P1-1 测试】snapshot_limit 截断 snapshot 数量（防止爆内存）。"""
        p = _make_portfolio(db_session, name="QA-Limit", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=20)

        result = compute_portfolio_performance(db_session, p.id, snapshot_limit=5)

        # 只取前 5 条（按日期升序）
        assert result["snapshot_count"] == 5
        assert result["date_range"]["start"] == "2026-07-15"
        assert result["date_range"]["end"] == "2026-07-19"


# ============================================================================
# Benchmark 对比曲线（P3+）
# ============================================================================

class TestBenchmarkCurve:
    """【P3+ 测试】benchmark 对比曲线构造逻辑。"""

    def test_default_benchmark_returns_name_with_empty_curve_when_no_data(self, db_session):
        """无 IndexPrice 数据时，benchmark_curve 为空但 benchmark_name 仍返回（前端降级）。"""
        p = _make_portfolio(db_session, name="QA-Bench-Empty", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        result = compute_portfolio_performance(db_session, p.id)

        assert result["benchmark_name"] == "沪深300"
        assert result["benchmark_curve"] == []

    def test_benchmark_none_disables_curve(self, db_session):
        """benchmark=None 时完全不返回 benchmark 数据。"""
        p = _make_portfolio(db_session, name="QA-Bench-None", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        result = compute_portfolio_performance(db_session, p.id, benchmark=None)

        assert result["benchmark_name"] is None
        assert result["benchmark_curve"] == []

    def test_benchmark_empty_string_disables_curve(self, db_session):
        """benchmark='' 时完全不返回 benchmark 数据。"""
        p = _make_portfolio(db_session, name="QA-Bench-EmptyStr", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        result = compute_portfolio_performance(db_session, p.id, benchmark="")

        assert result["benchmark_name"] is None
        assert result["benchmark_curve"] == []

    def test_benchmark_curve_normalized_to_initial_capital(self, db_session):
        """有 IndexPrice 数据时，benchmark_curve 归一化到 initial_capital 起点。"""
        from app.models.index_price import IndexPrice

        p = _make_portfolio(db_session, name="QA-Bench-Norm", total_capital=100000.0)
        # equity_curve: 2026-07-15 ~ 2026-07-17, equity = 100000/100100/100200
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        # seed IndexPrice for "000300"：close 从 4000 涨到 4080（+2%）
        db_session.add_all([
            IndexPrice(symbol="000300", trade_date=date(2026, 7, 15), open=4000.0, high=4010.0, low=3990.0, close=4000.0, volume=1e6, amount=4e9),
            IndexPrice(symbol="000300", trade_date=date(2026, 7, 16), open=4000.0, high=4050.0, low=3995.0, close=4040.0, volume=1.1e6, amount=4.4e9),
            IndexPrice(symbol="000300", trade_date=date(2026, 7, 17), open=4040.0, high=4090.0, low=4030.0, close=4080.0, volume=9.5e5, amount=3.9e9),
        ])
        db_session.commit()

        result = compute_portfolio_performance(db_session, p.id)

        assert result["benchmark_name"] == "沪深300"
        bench = result["benchmark_curve"]
        assert len(bench) == 3
        # 首点归一化到 initial_capital=100000
        assert bench[0]["equity"] == pytest.approx(100000.0, rel=1e-4)
        # 4040/4000 = 1.01 → 101000
        assert bench[1]["equity"] == pytest.approx(101000.0, rel=1e-4)
        # 4080/4000 = 1.02 → 102000
        assert bench[2]["equity"] == pytest.approx(102000.0, rel=1e-4)
        # 日期与 equity_curve 对齐
        assert bench[0]["date"] == "2026-07-15"
        assert bench[1]["date"] == "2026-07-16"
        assert bench[2]["date"] == "2026-07-17"

    def test_benchmark_curve_forward_fills_non_trading_day(self, db_session):
        """equity_curve 某天无对应 benchmark 数据时，前向填充（取最近一日 close）。"""
        from app.models.index_price import IndexPrice

        p = _make_portfolio(db_session, name="QA-Bench-FFill", total_capital=100000.0)
        # equity_curve: 2026-07-15 ~ 2026-07-17
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        # 只 seed 7-15 和 7-17，缺 7-16
        db_session.add_all([
            IndexPrice(symbol="000300", trade_date=date(2026, 7, 15), open=4000.0, high=4010.0, low=3990.0, close=4000.0, volume=1e6, amount=4e9),
            IndexPrice(symbol="000300", trade_date=date(2026, 7, 17), open=4040.0, high=4090.0, low=4030.0, close=4080.0, volume=9.5e5, amount=3.9e9),
        ])
        db_session.commit()

        result = compute_portfolio_performance(db_session, p.id)

        bench = result["benchmark_curve"]
        assert len(bench) == 3
        # 7-16 前向填充 7-15 的 close=4000 → 归一化后 100000
        assert bench[1]["date"] == "2026-07-16"
        assert bench[1]["equity"] == pytest.approx(100000.0, rel=1e-4)
        # 7-17 用 4080 → 102000
        assert bench[2]["equity"] == pytest.approx(102000.0, rel=1e-4)

    def test_benchmark_unknown_symbol_returns_symbol_as_name(self, db_session):
        """未知 symbol 的 benchmark_name 回退为 symbol 本身。"""
        p = _make_portfolio(db_session, name="QA-Bench-Unknown", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        result = compute_portfolio_performance(db_session, p.id, benchmark="999999")

        assert result["benchmark_name"] == "999999"
        assert result["benchmark_curve"] == []

    def test_benchmark_no_snapshot_returns_empty_curve(self, db_session):
        """无 snapshot 时 benchmark_curve 也为空（即使有 IndexPrice 数据）。"""
        from app.models.index_price import IndexPrice

        p = _make_portfolio(db_session, name="QA-Bench-NoSnap", total_capital=100000.0)
        # 不 seed snapshot
        db_session.add(IndexPrice(symbol="000300", trade_date=date(2026, 7, 15), open=4000.0, high=4010.0, low=3990.0, close=4000.0, volume=1e6, amount=4e9))
        db_session.commit()

        result = compute_portfolio_performance(db_session, p.id)

        assert result["benchmark_curve"] == []
        # benchmark_name 仍返回（即使 curve 为空）
        assert result["benchmark_name"] == "沪深300"

    def test_benchmark_custom_symbol_uses_correct_name(self, db_session):
        """自定义 benchmark symbol 时使用对应中文名。"""
        from app.models.index_price import IndexPrice

        p = _make_portfolio(db_session, name="QA-Bench-SH", total_capital=100000.0)
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=2)

        db_session.add(IndexPrice(symbol="000001", trade_date=date(2026, 7, 15), open=3000.0, high=3010.0, low=2990.0, close=3000.0, volume=1e6, amount=3e9))
        db_session.add(IndexPrice(symbol="000001", trade_date=date(2026, 7, 16), open=3000.0, high=3060.0, low=2995.0, close=3060.0, volume=1.1e6, amount=3.3e9))
        db_session.commit()

        result = compute_portfolio_performance(db_session, p.id, benchmark="000001")

        assert result["benchmark_name"] == "上证指数"
        bench = result["benchmark_curve"]
        assert len(bench) == 2
        # 3060/3000 = 1.02 → 102000
        assert bench[1]["equity"] == pytest.approx(102000.0, rel=1e-4)


# ============================================================================
# WP8 绩效归因测试
# ============================================================================
# 守护 6 类归因维度 + 统一入口 + 样本提示 + API 端点
# 1. attribute_by_member 基本成员归因
# 2. attribute_by_execution_mode 执行模式归因
# 3. attribute_by_source 来源归因
# 4. attribute_by_rule_signal 规则/信号归因
# 5. compute_backtest_vs_sim_diff 回测与模拟偏差
# 6. attribute_cost_impact 成本影响
# 7. get_attribution_report 完整报告
# 8. 样本不足提示
# 9. API 端点
# ============================================================================

from app.models.backtest import BacktestRun
from app.models.portfolio_member import PortfolioMember
from app.services.attribution import (
    attribute_by_member,
    attribute_by_execution_mode,
    attribute_by_source,
    attribute_by_rule_signal,
    compute_backtest_vs_sim_diff,
    attribute_cost_impact,
    get_attribution_report,
    MIN_TRADE_SAMPLE,
)


def _make_member(
    db_session, portfolio_id, symbol_id, execution_mode="manual", source_type="manual"
) -> PortfolioMember:
    """造一个组合成员。"""
    m = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status="active",
        execution_mode=execution_mode,
        source_type=source_type,
    )
    db_session.add(m)
    db_session.commit()
    db_session.refresh(m)
    return m


def _make_attributed_sell_trade(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    realized_pnl: float,
    created_at: datetime,
    member_id: int | None = None,
    execution_mode: str | None = None,
    source_type: str | None = None,
    signal_id: int | None = None,
    rule_version_id: int | None = None,
    note: str | None = None,
    fee: float = 0.0,
    submitted_price: float = 11.0,
    filled_price: float = 11.0,
    status: str = "filled",
    rejection_code: str | None = None,
):
    """造一笔带归因字段的卖出 trade（SimOrder + SimTrade）。

    SimOrder 携带 WP6 归因字段（member_id/execution_mode/source_type 等）。
    """
    sell_order = SimOrder(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        side="sell",
        order_type="market",
        quantity=100,
        submitted_price=submitted_price,
        status=status,
        filled_quantity=100,
        filled_price=filled_price,
        filled_amount=filled_price * 100,
        fee=fee,
        filled_at=created_at,
        member_id=member_id,
        execution_mode=execution_mode,
        source_type=source_type,
        signal_id=signal_id,
        rule_version_id=rule_version_id,
        note=note,
        rejection_code=rejection_code,
        created_at=created_at,
    )
    db_session.add(sell_order)
    db_session.flush()
    sell_trade = SimTrade(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        order_id=sell_order.id,
        side="sell",
        quantity=100,
        price=filled_price,
        amount=filled_price * 100,
        fee=fee,
        realized_pnl=realized_pnl,
        created_at=created_at,
    )
    db_session.add(sell_trade)
    db_session.commit()
    return sell_order, sell_trade


class TestAttributeByMember:
    """【WP8 测试】按成员贡献归因。"""

    def test_attribute_by_member_basic(self, db_session):
        """【WP8 测试】基本成员归因：两个成员各自贡献正确计算。"""
        p = _make_portfolio(db_session, name="QA-Attr-Member", total_capital=100000.0)
        sym1 = _make_symbol(db_session, symbol="700001")
        sym2 = _make_symbol(db_session, symbol="700002")
        m1 = _make_member(db_session, p.id, sym1.id, execution_mode="manual")
        m2 = _make_member(db_session, p.id, sym2.id, execution_mode="auto")

        # m1 盈利 500，m2 亏损 200
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym1.id, realized_pnl=500.0,
            created_at=datetime(2026, 7, 16, 10, 0), member_id=m1.id,
        )
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym2.id, realized_pnl=-200.0,
            created_at=datetime(2026, 7, 17, 10, 0), member_id=m2.id,
        )

        result = attribute_by_member(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )

        assert result["total_pnl"] == 300.0  # 500 - 200
        assert result["total_trades"] == 2
        assert result["member_count"] == 2
        items = result["items"]
        # 按 pnl 降序：m1(500) 在前，m2(-200) 在后
        assert items[0]["member_id"] == m1.id
        assert items[0]["pnl"] == 500.0
        assert items[0]["contribution_pct"] == pytest.approx(166.67, abs=0.1)
        assert items[1]["member_id"] == m2.id
        assert items[1]["pnl"] == -200.0
        assert items[1]["contribution_pct"] == pytest.approx(-66.67, abs=0.1)


class TestAttributeByExecutionMode:
    """【WP8 测试】按执行模式归因。"""

    def test_attribute_by_execution_mode(self, db_session):
        """【WP8 测试】manual/auto/confirm 三种模式各自贡献。"""
        p = _make_portfolio(db_session, name="QA-Attr-Mode", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700010")

        # manual 盈利 300，auto 盈利 100，confirm 亏损 50
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=300.0,
            created_at=datetime(2026, 7, 16, 10, 0), execution_mode="manual",
        )
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=100.0,
            created_at=datetime(2026, 7, 17, 10, 0), execution_mode="auto",
        )
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=-50.0,
            created_at=datetime(2026, 7, 18, 10, 0), execution_mode="confirm",
        )

        result = attribute_by_execution_mode(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )

        assert result["total_pnl"] == 350.0
        assert result["total_trades"] == 3
        items = result["items"]
        modes = {it["execution_mode"]: it for it in items}
        assert modes["manual"]["pnl"] == 300.0
        assert modes["auto"]["pnl"] == 100.0
        assert modes["confirm"]["pnl"] == -50.0
        # 标签正确
        assert modes["manual"]["execution_label"] == "手动"
        assert modes["auto"]["execution_label"] == "自动"
        assert modes["confirm"]["execution_label"] == "确认"


class TestAttributeBySource:
    """【WP8 测试】按来源归因。"""

    def test_attribute_by_source(self, db_session):
        """【WP8 测试】member/scan/manual 来源各自贡献。"""
        p = _make_portfolio(db_session, name="QA-Attr-Source", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700020")

        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=400.0,
            created_at=datetime(2026, 7, 16, 10, 0), source_type="member",
        )
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=-100.0,
            created_at=datetime(2026, 7, 17, 10, 0), source_type="scan",
        )
        # 无 source_type 的历史订单
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=50.0,
            created_at=datetime(2026, 7, 18, 10, 0), source_type=None,
        )

        result = attribute_by_source(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )

        assert result["total_pnl"] == 350.0
        items = result["items"]
        srcs = {it["source_type"]: it for it in items}
        assert srcs["member"]["pnl"] == 400.0
        assert srcs["member"]["source_label"] == "组合成员"
        assert srcs["scan"]["pnl"] == -100.0
        assert srcs["scan"]["source_label"] == "扫描结果"
        # None → unknown
        assert srcs["unknown"]["pnl"] == 50.0
        assert srcs["unknown"]["source_label"] == "未知"


class TestAttributeByRuleSignal:
    """【WP8 测试】按规则版本/信号/退出原因归因。"""

    def test_attribute_by_rule_signal(self, db_session):
        """【WP8 测试】不同 rule_version_id + signal_id + exit_reason 分组。"""
        p = _make_portfolio(db_session, name="QA-Attr-Rule", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700030")

        # rule_v1 + signal_1 + exit:stop_loss
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=-200.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            rule_version_id=1, signal_id=101, note="exit:stop_loss",
        )
        # rule_v1 + signal_1 + exit:take_profit
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=500.0,
            created_at=datetime(2026, 7, 17, 10, 0),
            rule_version_id=1, signal_id=101, note="exit:take_profit",
        )
        # rule_v2 + signal_2 + 无 note
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=150.0,
            created_at=datetime(2026, 7, 18, 10, 0),
            rule_version_id=2, signal_id=102, note=None,
        )

        result = attribute_by_rule_signal(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )

        assert result["total_pnl"] == 450.0
        assert result["total_trades"] == 3
        items = result["items"]
        # 应有 3 个分组：(1, 101, stop_loss), (1, 101, take_profit), (2, 102, unspecified)
        assert len(items) == 3
        groups = {(it["rule_version_id"], it["signal_id"], it["exit_reason"]): it for it in items}
        assert (1, 101, "stop_loss") in groups
        assert (1, 101, "take_profit") in groups
        assert (2, 102, "unspecified") in groups
        assert groups[(1, 101, "take_profit")]["pnl"] == 500.0
        assert groups[(2, 102, "unspecified")]["pnl"] == 150.0


class TestComputeBacktestVsSimDiff:
    """【WP8 测试】回测与模拟账户同期偏差。"""

    def test_compute_backtest_vs_sim_diff(self, db_session):
        """【WP8 测试】回测指标 vs 模拟指标偏差计算正确。"""
        p = _make_portfolio(db_session, name="QA-Attr-BtVsSim", total_capital=100000.0)
        # 造 5 天 snapshot（总收益 +400 = 0.4%）
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=5)

        # 造回测记录：total_return_pct=2%, max_drawdown_pct=5%, sharpe=1.5, win_rate=0.6
        bt_run = BacktestRun(
            portfolio_id=p.id,
            run_name="QA-BtRun",
            symbols_json="[]",
            rule_config_json="{}",
            start_date=date(2026, 7, 15),
            end_date=date(2026, 7, 19),
            initial_capital=100000.0,
            total_return=2000.0,
            total_return_pct=0.02,
            max_drawdown=5000.0,
            max_drawdown_pct=0.05,
            sharpe_ratio=1.5,
            win_rate=0.6,
            profit_factor=2.0,
            trade_count=10,
            avg_holding_days=3.0,
            status="completed",
        )
        db_session.add(bt_run)
        db_session.commit()
        db_session.refresh(bt_run)

        result = compute_backtest_vs_sim_diff(
            db_session, p.id, bt_run.id,
            start_date=date(2026, 7, 15), end_date=date(2026, 7, 19),
        )

        # 回测指标
        assert result["backtest_metrics"]["total_return_pct"] == 0.02
        assert result["backtest_metrics"]["sharpe_ratio"] == 1.5
        # 模拟指标（snapshot 总收益 0.4%）
        assert result["sim_metrics"]["total_return_pct"] == pytest.approx(0.004, abs=1e-4)
        # 偏差 = sim - backtest = 0.004 - 0.02 = -0.016
        assert result["diff"]["return_diff_pct"] == pytest.approx(-0.016, abs=1e-4)
        assert result["diff"]["sharpe_diff"] != 0  # 有差异
        # 解释文本非空
        assert result["explanation"]
        # 样本不足（sim 无 trade）
        assert result["sample_warning"] is not None

    def test_compute_backtest_vs_sim_diff_not_found(self, db_session):
        """【WP8 测试】不存在的 backtest_run_id 抛 ValueError。"""
        p = _make_portfolio(db_session, name="QA-Attr-BtNotFound")
        with pytest.raises(ValueError, match="BacktestRun not found"):
            compute_backtest_vs_sim_diff(
                db_session, p.id, 99999,
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            )


class TestAttributeCostImpact:
    """【WP8 测试】成本/滑点/未成交/风控阻断影响。"""

    def test_attribute_cost_impact(self, db_session):
        """【WP8 测试】手续费 + 滑点 + 拒绝/风控阻断计数正确。"""
        p = _make_portfolio(db_session, name="QA-Attr-Cost", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700040")

        # 订单1：已成交，fee=5，滑点=|11-10|*100=100
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=500.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            fee=5.0, submitted_price=10.0, filled_price=11.0,
        )
        # 订单2：已成交，fee=3，无滑点
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=200.0,
            created_at=datetime(2026, 7, 17, 10, 0),
            fee=3.0, submitted_price=11.0, filled_price=11.0,
        )
        # 订单3：被风控拒绝（status=rejected, rejection_code=risk_limit_exceed）
        rejected_order = SimOrder(
            portfolio_id=p.id, symbol_id=sym.id, side="buy", order_type="market",
            quantity=100, submitted_price=10.0, status="rejected",
            filled_quantity=0, filled_price=0, filled_amount=0, fee=0,
            filled_at=datetime(2026, 7, 18, 10, 0),
            rejection_code="risk_limit_exceed",
            created_at=datetime(2026, 7, 18, 10, 0),
        )
        db_session.add(rejected_order)
        db_session.commit()

        result = attribute_cost_impact(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )

        # 手续费 = 5 + 3 = 8
        assert result["fee_cost"] == 8.0
        # 滑点 = |11-10|*100 = 100（订单1）+ 0（订单2）= 100
        assert result["slippage_cost"] == 100.0
        # 总成本 = 8 + 100 = 108
        assert result["total_cost"] == 108.0
        # 拒绝订单数 = 1（rejected_order）
        assert result["rejected_count"] == 1
        # 风控阻断数 = 1（rejection_code 含 "risk" 和 "limit"）
        assert result["risk_blocked_count"] == 1
        # 影响百分比 = 108 / (11*100 + 11*100) * 100 = 108/2200*100 ≈ 4.9091
        assert result["impact_pct"] == pytest.approx(4.9091, abs=0.01)


class TestGetAttributionReport:
    """【WP8 测试】统一归因入口。"""

    def test_get_attribution_report_full(self, db_session):
        """【WP8 测试】完整报告包含全部维度 + summary。"""
        p = _make_portfolio(db_session, name="QA-Attr-Report", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700050")
        m = _make_member(db_session, p.id, sym.id, execution_mode="auto", source_type="candidate")
        _seed_snapshots(db_session, p.id, start_equity=100000.0, days=3)

        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=500.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            member_id=m.id, execution_mode="auto", source_type="candidate",
            rule_version_id=1, signal_id=201, note="exit:take_profit",
        )

        report = get_attribution_report(
            db_session, p.id,
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        )

        assert report["portfolio_id"] == p.id
        assert report["start_date"] == "2026-07-01"
        assert report["end_date"] == "2026-07-31"
        # 全部维度都计算了
        assert report["by_member"] is not None
        assert report["by_execution_mode"] is not None
        assert report["by_source"] is not None
        assert report["by_rule_signal"] is not None
        # backtest_vs_sim 未传 backtest_run_id → None
        assert report["backtest_vs_sim"] is None
        assert report["cost_impact"] is not None
        # summary 非空
        assert report["summary"]
        # 验证成员归因数据正确
        assert report["by_member"]["items"][0]["member_id"] == m.id
        assert report["by_member"]["items"][0]["pnl"] == 500.0

    def test_get_attribution_report_partial_dimensions(self, db_session):
        """【WP8 测试】只请求部分维度时其他维度为 None。"""
        p = _make_portfolio(db_session, name="QA-Attr-Partial", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700060")
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=100.0,
            created_at=datetime(2026, 7, 16, 10, 0),
        )

        report = get_attribution_report(
            db_session, p.id,
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            dimensions=["by_member", "cost_impact"],
        )

        assert report["by_member"] is not None
        assert report["cost_impact"] is not None
        # 未请求的维度为 None
        assert report["by_execution_mode"] is None
        assert report["by_source"] is None
        assert report["by_rule_signal"] is None
        assert report["backtest_vs_sim"] is None

    def test_get_attribution_report_not_found(self, db_session):
        """【WP8 测试】不存在的组合抛 ValueError。"""
        with pytest.raises(ValueError, match="Portfolio not found"):
            get_attribution_report(
                db_session, 99999,
                start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
            )


class TestAttributionSampleSizeWarning:
    """【WP8 测试】样本不足提示。"""

    def test_attribution_sample_size_warning(self, db_session):
        """【WP8 测试】交易笔数 < MIN_TRADE_SAMPLE 时返回 sample_warning。"""
        p = _make_portfolio(db_session, name="QA-Attr-Sample", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700070")

        # 只造 1 笔交易（< MIN_TRADE_SAMPLE=5）
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=100.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            execution_mode="manual", source_type="manual",
        )

        result_member = attribute_by_member(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )
        result_mode = attribute_by_execution_mode(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )
        result_source = attribute_by_source(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )

        # 交易笔数 1 < 5 → 样本不足
        assert result_member["total_trades"] == 1
        assert result_member["sample_warning"] == "样本不足，结论仅供参考"
        assert result_mode["sample_warning"] == "样本不足，结论仅供参考"
        assert result_source["sample_warning"] == "样本不足，结论仅供参考"

    def test_attribution_member_count_warning(self, db_session):
        """【WP8 测试】成员数 < MIN_MEMBER_SAMPLE 时 member 归因返回 sample_warning。"""
        p = _make_portfolio(db_session, name="QA-Attr-MemberCount", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700071")
        m = _make_member(db_session, p.id, sym.id)

        # 造 6 笔交易（>= MIN_TRADE_SAMPLE=5），但只有 1 个成员（< MIN_MEMBER_SAMPLE=3）
        for i in range(6):
            _make_attributed_sell_trade(
                db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=50.0,
                created_at=datetime(2026, 7, 10 + i, 10, 0),
                member_id=m.id,
            )

        result = attribute_by_member(
            db_session, p.id, date(2026, 7, 1), date(2026, 7, 31)
        )
        # 交易笔数足够（6 >= 5），但成员数不足（1 < 3）
        assert result["total_trades"] == 6
        assert result["member_count"] == 1
        assert result["sample_warning"] == "样本不足，结论仅供参考"


class TestAttributionAPIEndpoint:
    """【WP8 测试】归因 API 端点。"""

    @pytest.fixture()
    def client(self, db_session):
        """构造 TestClient，依赖覆盖让 get_db 返回测试 session。"""
        from fastapi.testclient import TestClient

        from app.db.session import get_db
        from app.main import app

        def _override_get_db():
            try:
                yield db_session
            finally:
                pass

        app.dependency_overrides[get_db] = _override_get_db
        yield TestClient(app)
        app.dependency_overrides.pop(get_db, None)

    def test_attribution_api_endpoint(self, client, db_session):
        """【WP8 测试】GET /portfolios/{id}/attribution 返回归因报告。"""
        p = _make_portfolio(db_session, name="QA-Attr-API", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700080")
        m = _make_member(db_session, p.id, sym.id, execution_mode="auto")

        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=300.0,
            created_at=datetime(2026, 7, 16, 10, 0),
            member_id=m.id, execution_mode="auto", source_type="member",
            rule_version_id=1, signal_id=301,
        )

        resp = client.get(
            f"/api/v1/portfolios/{p.id}/attribution",
            params={
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["portfolio_id"] == p.id
        assert data["start_date"] == "2026-07-01"
        assert data["end_date"] == "2026-07-31"
        # 全部维度都返回
        assert data["by_member"] is not None
        assert data["by_execution_mode"] is not None
        assert data["by_source"] is not None
        assert data["by_rule_signal"] is not None
        assert data["cost_impact"] is not None
        # backtest_vs_sim 未传 backtest_run_id → None
        assert data["backtest_vs_sim"] is None
        # summary 非空
        assert data["summary"]
        # 成员归因数据正确
        assert data["by_member"]["items"][0]["member_id"] == m.id
        assert data["by_member"]["items"][0]["pnl"] == 300.0

    def test_attribution_api_endpoint_partial_dimensions(self, client, db_session):
        """【WP8 测试】GET ?dimensions=by_member,cost_impact 只返回指定维度。"""
        p = _make_portfolio(db_session, name="QA-Attr-API-Partial", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700081")
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=200.0,
            created_at=datetime(2026, 7, 16, 10, 0),
        )

        resp = client.get(
            f"/api/v1/portfolios/{p.id}/attribution",
            params={
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
                "dimensions": "by_member,cost_impact",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["by_member"] is not None
        assert data["cost_impact"] is not None
        assert data["by_execution_mode"] is None
        assert data["by_source"] is None

    def test_attribution_api_endpoint_not_found(self, client, db_session):
        """【WP8 测试】GET 不存在的组合返回 404。"""
        resp = client.get("/api/v1/portfolios/99999/attribution")
        assert resp.status_code == 404

    def test_create_review_api_endpoint(self, client, db_session):
        """【WP8 测试】POST /portfolios/{id}/reviews 创建复盘记录。"""
        p = _make_portfolio(db_session, name="QA-Attr-Review", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="700090")
        _make_attributed_sell_trade(
            db_session, portfolio_id=p.id, symbol_id=sym.id, realized_pnl=100.0,
            created_at=datetime(2026, 7, 16, 10, 0),
        )

        resp = client.post(
            f"/api/v1/portfolios/{p.id}/reviews",
            json={
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
                "note": "本月归因复盘",
                "title": "7月复盘",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] is not None
        assert data["portfolio_id"] == p.id
        assert data["start_date"] == "2026-07-01"
        assert data["end_date"] == "2026-07-31"
        assert data["note"] == "本月归因复盘"
        assert data["title"] == "7月复盘"
        # report_snapshot_json 非空（自动计算了归因报告）
        assert data["report_snapshot_json"] is not None

    def test_list_reviews_api_endpoint(self, client, db_session):
        """【WP8 测试】GET /portfolios/{id}/reviews 列出复盘记录。"""
        p = _make_portfolio(db_session, name="QA-Attr-ReviewList", total_capital=100000.0)

        # 创建两条复盘记录
        for i in range(2):
            client.post(
                f"/api/v1/portfolios/{p.id}/reviews",
                json={
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-31",
                    "note": f"复盘 {i}",
                    "title": f"7月复盘-{i}",
                },
            )

        resp = client.get(f"/api/v1/portfolios/{p.id}/reviews")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        # 按创建时间降序
        assert data[0]["title"] == "7月复盘-1"

