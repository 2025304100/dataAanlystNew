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

