"""白盒测试 - 回测结果应用到模拟组合（P2-1）。

守护 apply_backtest_run_to_portfolio 的关键行为，防止重构/优化时退化：
1. 回测不存在/未成功/组合不存在/非模拟 → ValueError
2. 目标组合已有 sim orders 且 clear_existing=False → ValueError
3. 完整 trade（买入+卖出）→ applied=2, open_positions=0
4. 未平仓 trade（只有买入）→ applied=1, open_positions=1
5. 多笔 trade 按时间顺序处理
6. clear_existing=True 清空已有数据
7. 空 trades → applied=0, final_cash=initial_capital
8. 单笔失败隔离（symbol 不存在）
9. 时间戳使用 entry_date/exit_date（不污染最近交易统计）
10. filled_price/fee 使用回测值，不重新计算滑点
11. realized_pnl 来自 _upsert_position 卖出计算
12. API 端点：200 成功 / 404 不存在 / 409 已有数据
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.backtest_apply import apply_backtest_run_to_portfolio
from app.services.sim_accounts import cash_balance, place_sim_order

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================

def _make_portfolio(db_session, name="QA-Apply", account_type="simulated", total_capital=100000.0) -> Portfolio:
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
    return p


def _make_symbol(db_session, symbol="600010", name="测试标的", market="sh") -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type="stock", market=market)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_backtest_run(
    db_session,
    portfolio_id: int,
    *,
    initial_capital: float = 100000.0,
    status: str = "success",
    start_date: date = date(2026, 1, 1),
    end_date: date = date(2026, 3, 31),
) -> BacktestRun:
    run = BacktestRun(
        portfolio_id=portfolio_id,
        run_name=f"QA-Run-{datetime.now().strftime('%H%M%S%f')}",
        symbols_json="[]",
        rule_config_json="{}",
        cost_config_json="{}",
        score_weight_mode="manual",
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        status=status,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _add_trade(
    db_session,
    run_id: int,
    symbol_id: int,
    *,
    entry_date: date,
    entry_price: float,
    quantity: float,
    entry_cost: float = 5.0,
    exit_date: date | None = None,
    exit_price: float | None = None,
    exit_cost: float | None = None,
    exit_reason: str | None = None,
    pnl: float | None = None,
) -> BacktestTrade:
    trade = BacktestTrade(
        run_id=run_id,
        symbol_id=symbol_id,
        entry_date=entry_date,
        entry_price=entry_price,
        quantity=quantity,
        entry_cost=entry_cost,
        exit_date=exit_date,
        exit_price=exit_price,
        exit_cost=exit_cost,
        exit_reason=exit_reason,
        pnl=pnl,
    )
    db_session.add(trade)
    db_session.commit()
    db_session.refresh(trade)
    return trade


# ============================================================================
# 1. 错误场景
# ============================================================================

class TestApplyErrors:
    """守护错误场景的边界处理。"""

    def test_run_not_found_raises(self, db_session):
        """回测不存在 → ValueError('not found')。"""
        p = _make_portfolio(db_session, name="QA-Err-1")
        with pytest.raises(ValueError, match="not found"):
            apply_backtest_run_to_portfolio(db_session, run_id=99999, portfolio_id=p.id)

    def test_run_not_success_raises(self, db_session):
        """回测 status != success → ValueError('status is')。"""
        p = _make_portfolio(db_session, name="QA-Err-2")
        run = _make_backtest_run(db_session, p.id, status="running")
        with pytest.raises(ValueError, match="status is"):
            apply_backtest_run_to_portfolio(db_session, run_id=run.id, portfolio_id=p.id)

    def test_portfolio_not_found_raises(self, db_session):
        """组合不存在 → ValueError('not found')。"""
        run = _make_backtest_run(db_session, portfolio_id=1)
        with pytest.raises(ValueError, match="not found"):
            apply_backtest_run_to_portfolio(db_session, run_id=run.id, portfolio_id=99999)

    def test_portfolio_not_simulated_raises(self, db_session):
        """组合不是 simulated → ValueError('not simulated')。"""
        p = _make_portfolio(db_session, name="QA-Real", account_type="real")
        run = _make_backtest_run(db_session, portfolio_id=p.id)
        with pytest.raises(ValueError, match="not simulated"):
            apply_backtest_run_to_portfolio(db_session, run_id=run.id, portfolio_id=p.id)

    def test_existing_orders_without_clear_raises(self, db_session):
        """目标组合已有 sim orders 且 clear_existing=False → ValueError('already has sim orders')。"""
        p = _make_portfolio(db_session, name="QA-HasOrders")
        sym = _make_symbol(db_session)
        # 手动下一笔模拟单
        place_sim_order(db_session, p, sym, "buy", quantity=100, price=10.0,
                        enforce_rules=False, apply_fees=False)
        run = _make_backtest_run(db_session, portfolio_id=p.id)
        with pytest.raises(ValueError, match="already has sim orders"):
            apply_backtest_run_to_portfolio(db_session, run_id=run.id, portfolio_id=p.id)


# ============================================================================
# 2. 完整场景：买入+卖出
# ============================================================================

class TestFullRoundTrip:
    """守护完整交易循环的正确性。"""

    def test_completed_trade_applies_buy_and_sell(self, db_session):
        """完整 trade（买入+卖出）→ applied=2, skipped=0, open_positions=0。"""
        p = _make_portfolio(db_session, name="QA-Full", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600010")
        run = _make_backtest_run(db_session, portfolio_id=p.id, initial_capital=100000.0)

        _add_trade(
            db_session, run.id, sym.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
            exit_date=date(2026, 2, 10),
            exit_price=11.0,
            exit_cost=11.0,  # 卖出成本
            exit_reason="take_profit",
            pnl=95.0,
        )

        result = apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        assert result["applied_trades"] == 2  # 买入 + 卖出
        assert result["skipped_trades"] == 0
        assert result["open_positions"] == 0  # 已平仓
        assert result["initial_capital"] == 100000.0
        # 现金 = 100000 - 买入(10*100 + 5) + 卖出(11*100 - 11) = 100000 - 1005 + 1089 = 100084
        assert result["final_cash"] == pytest.approx(100084.0, abs=0.01)
        assert result["errors"] == []

    def test_open_trade_keeps_position(self, db_session):
        """未平仓 trade（只有买入）→ applied=1, open_positions=1。"""
        p = _make_portfolio(db_session, name="QA-Open", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600011")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        _add_trade(
            db_session, run.id, sym.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
            # 无 exit_date
        )

        result = apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        assert result["applied_trades"] == 1
        assert result["open_positions"] == 1
        # 现金 = 100000 - 1005 = 98995
        assert result["final_cash"] == pytest.approx(98995.0, abs=0.01)

        # 验证持仓存在
        positions = db_session.query(Position).filter_by(portfolio_id=p.id).all()
        assert len(positions) == 1
        assert positions[0].symbol_id == sym.id
        assert positions[0].quantity == 100
        assert positions[0].avg_cost == 10.0

    def test_multiple_trades_sorted_by_date(self, db_session):
        """多笔 trade 按时间顺序处理（含多个 symbol）。"""
        p = _make_portfolio(db_session, name="QA-Multi", total_capital=100000.0)
        sym_a = _make_symbol(db_session, symbol="600012", name="A")
        sym_b = _make_symbol(db_session, symbol="600013", name="B")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        # 故意乱序添加，验证按日期重放
        _add_trade(
            db_session, run.id, sym_b.id,
            entry_date=date(2026, 2, 1),
            entry_price=20.0,
            quantity=200,
            entry_cost=6.0,
            exit_date=date(2026, 2, 15),
            exit_price=22.0,
            exit_cost=22.0,
            exit_reason="take_profit",
        )
        _add_trade(
            db_session, run.id, sym_a.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
            exit_date=date(2026, 1, 20),
            exit_price=11.0,
            exit_cost=11.0,
            exit_reason="take_profit",
        )

        result = apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        assert result["applied_trades"] == 4  # 2 买入 + 2 卖出
        assert result["open_positions"] == 0
        assert result["skipped_trades"] == 0

        # 验证 sim_trades 按时间升序
        trades = db_session.query(SimTrade).filter_by(portfolio_id=p.id).order_by(SimTrade.id).all()
        assert len(trades) == 4
        dates = [t.created_at.date() for t in trades]
        assert dates == sorted(dates)


# ============================================================================
# 3. 清空与重置
# ============================================================================

class TestClearExisting:
    """守护 clear_existing=True 的清空逻辑。"""

    def test_clear_existing_removes_old_data(self, db_session):
        """clear_existing=True 删除旧的 sim_orders/positions/cash_ledger。"""
        p = _make_portfolio(db_session, name="QA-Clear", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600014")
        # 先手动下一笔单
        place_sim_order(db_session, p, sym, "buy", quantity=100, price=10.0,
                        enforce_rules=False, apply_fees=False)
        assert db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1
        assert db_session.query(Position).filter_by(portfolio_id=p.id).count() == 1

        run = _make_backtest_run(db_session, portfolio_id=p.id)
        _add_trade(
            db_session, run.id, sym.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
        )

        result = apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        # 旧的 sim_order 被删除，新的被写入
        assert result["applied_trades"] == 1
        # 应该只有 1 个 sim_order（清空后新建的）
        assert db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1
        # CashLedger：清空旧的 + 新写入 deposit + buy = 2
        assert db_session.query(CashLedger).filter_by(portfolio_id=p.id).count() == 2

    def test_no_trades_still_resets_capital(self, db_session):
        """空 trades → applied=0, final_cash=initial_capital。"""
        p = _make_portfolio(db_session, name="QA-Empty", total_capital=50000.0)
        run = _make_backtest_run(db_session, portfolio_id=p.id, initial_capital=50000.0)

        result = apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        assert result["applied_trades"] == 0
        assert result["open_positions"] == 0
        assert result["final_cash"] == 50000.0
        # CashLedger 应该有一笔 deposit
        assert db_session.query(CashLedger).filter_by(portfolio_id=p.id).count() == 1


# ============================================================================
# 4. 失败隔离
# ============================================================================

class TestFailureIsolation:
    """守护单笔失败隔离。"""

    def test_missing_symbol_skipped(self, db_session):
        """symbol 不存在的 trade 被跳过，其他 trade 正常应用。"""
        p = _make_portfolio(db_session, name="QA-Isolate", total_capital=100000.0)
        sym_ok = _make_symbol(db_session, symbol="600015", name="OK")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        # 一笔正常的 trade
        _add_trade(
            db_session, run.id, sym_ok.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
        )
        # 一笔 symbol 不存在的 trade（手动改 symbol_id）
        _add_trade(
            db_session, run.id, 99999,  # 不存在
            entry_date=date(2026, 1, 10),
            entry_price=20.0,
            quantity=100,
            entry_cost=5.0,
        )

        result = apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        assert result["applied_trades"] == 1
        assert result["skipped_trades"] == 1
        assert len(result["errors"]) == 1
        assert "99999" in result["errors"][0]
        # 正常的 trade 仍被应用
        assert db_session.query(SimOrder).filter_by(portfolio_id=p.id).count() == 1


# ============================================================================
# 5. 时间戳与字段保真
# ============================================================================

class TestTimestampFidelity:
    """守护时间戳和字段值的正确性。"""

    def test_timestamps_use_backtest_dates(self, db_session):
        """SimOrder/SimTrade 的 created_at 使用 entry_date/exit_date，而非当前时间。"""
        p = _make_portfolio(db_session, name="QA-Time", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600016")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        entry_dt = date(2026, 1, 5)
        exit_dt = date(2026, 2, 10)
        _add_trade(
            db_session, run.id, sym.id,
            entry_date=entry_dt,
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
            exit_date=exit_dt,
            exit_price=11.0,
            exit_cost=11.0,
            exit_reason="take_profit",
        )

        apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        trades = db_session.query(SimTrade).filter_by(portfolio_id=p.id).order_by(SimTrade.id).all()
        assert len(trades) == 2

        buy_trade = trades[0]
        sell_trade = trades[1]

        # 时间戳应为 entry_date + 15:00
        assert buy_trade.created_at.date() == entry_dt
        assert buy_trade.created_at.time() == time(15, 0, 0)
        assert sell_trade.created_at.date() == exit_dt
        assert sell_trade.created_at.time() == time(15, 0, 0)

        # 不应污染最近交易统计（最近 7 日）
        recent_threshold = datetime.now() - timedelta(days=7)
        assert buy_trade.created_at < recent_threshold
        assert sell_trade.created_at < recent_threshold

    def test_fill_price_uses_backtest_value(self, db_session):
        """fill_price 使用回测的 entry_price/exit_price，不重新计算滑点。"""
        p = _make_portfolio(db_session, name="QA-Price", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600017")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        _add_trade(
            db_session, run.id, sym.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
            exit_date=date(2026, 2, 10),
            exit_price=11.5,
            exit_cost=11.5,
            exit_reason="take_profit",
        )

        apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        orders = db_session.query(SimOrder).filter_by(portfolio_id=p.id).order_by(SimOrder.id).all()
        assert len(orders) == 2

        buy_order = orders[0]
        sell_order = orders[1]

        # fill_price 应严格等于回测值，不应用滑点
        assert buy_order.filled_price == 10.0
        assert sell_order.filled_price == 11.5

        # fee 应使用回测的 entry_cost/exit_cost
        assert buy_order.fee == 5.0
        assert sell_order.fee == 11.5

    def test_realized_pnl_from_upsert_position(self, db_session):
        """卖出 realized_pnl 由 _upsert_position 计算 = (sell_price - avg_cost) * quantity。"""
        p = _make_portfolio(db_session, name="QA-Pnl", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600018")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        _add_trade(
            db_session, run.id, sym.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
            exit_date=date(2026, 2, 10),
            exit_price=12.0,
            exit_cost=12.0,
            exit_reason="take_profit",
        )

        apply_backtest_run_to_portfolio(
            db_session, run_id=run.id, portfolio_id=p.id, clear_existing=True
        )

        sell_trade = (
            db_session.query(SimTrade)
            .filter_by(portfolio_id=p.id, side="sell")
            .one()
        )
        # avg_cost = 10.0, sell_price = 12.0, quantity = 100
        # realized_pnl = (12.0 - 10.0) * 100 = 200.0
        assert sell_trade.realized_pnl == pytest.approx(200.0, abs=0.01)


# ============================================================================
# 6. API 端点
# ============================================================================

class TestApplyEndpoint:
    """守护 POST /backtest/runs/{run_id}/apply-to-portfolio 端点。"""

    def _client_with_db(self, db_session):
        """构造 TestClient，依赖覆盖让 get_db 返回测试 session。"""
        from app.main import app

        def _override_get_db():
            try:
                yield db_session
            finally:
                pass

        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)

    def test_api_success(self, db_session):
        """API 正常调用返回 200。"""
        p = _make_portfolio(db_session, name="QA-API-OK", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600019")
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        _add_trade(
            db_session, run.id, sym.id,
            entry_date=date(2026, 1, 5),
            entry_price=10.0,
            quantity=100,
            entry_cost=5.0,
        )

        client = self._client_with_db(db_session)
        resp = client.post(
            f"/api/v1/backtest/runs/{run.id}/apply-to-portfolio",
            json={"portfolio_id": p.id, "clear_existing": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == run.id
        assert data["portfolio_id"] == p.id
        assert data["applied_trades"] == 1

    def test_api_run_not_found_returns_404(self, db_session):
        """回测不存在 → 404。"""
        p = _make_portfolio(db_session, name="QA-API-404")
        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/runs/99999/apply-to-portfolio",
            json={"portfolio_id": p.id, "clear_existing": True},
        )
        assert resp.status_code == 404

    def test_api_existing_orders_returns_409(self, db_session):
        """目标组合已有 sim orders 且 clear_existing=False → 409。"""
        p = _make_portfolio(db_session, name="QA-API-409")
        sym = _make_symbol(db_session, symbol="600020")
        place_sim_order(db_session, p, sym, "buy", quantity=100, price=10.0,
                        enforce_rules=False, apply_fees=False)
        run = _make_backtest_run(db_session, portfolio_id=p.id)

        client = self._client_with_db(db_session)
        resp = client.post(
            f"/api/v1/backtest/runs/{run.id}/apply-to-portfolio",
            json={"portfolio_id": p.id, "clear_existing": False},
        )
        assert resp.status_code == 409

    def test_api_run_not_success_returns_409(self, db_session):
        """回测未成功 → 409。"""
        p = _make_portfolio(db_session, name="QA-API-RunFail")
        run = _make_backtest_run(db_session, portfolio_id=p.id, status="failed")

        client = self._client_with_db(db_session)
        resp = client.post(
            f"/api/v1/backtest/runs/{run.id}/apply-to-portfolio",
            json={"portfolio_id": p.id, "clear_existing": True},
        )
        assert resp.status_code == 409
