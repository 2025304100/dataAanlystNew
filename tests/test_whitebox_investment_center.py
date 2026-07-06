"""白盒测试 - 投资中心 API (P1-3)。

覆盖：
1. app/api/routes/portfolios.py 中的组合与持仓端点
   - list_portfolios / create_portfolio
   - list_positions / upsert_position / delete_position
2. app/api/routes/sim_accounts.py 中的模拟账户端点
   - get_sim_account_snapshot / create_sim_order

特别覆盖：
- 胜率/仓位计算的除零保护（total_capital=0 时 position_pct=0）
- upsert_position 在 Symbol 不存在时返回 404
- create_sim_order 在 Symbol 不存在时返回 404
- create_portfolio 自动为 simulated 账户注入初始资金流水
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes.portfolios import (
    create_portfolio,
    delete_position,
    list_portfolios,
    list_positions,
    upsert_position,
)
from app.api.routes.sim_accounts import (
    create_sim_order,
    get_sim_account_snapshot,
)
from app.models.portfolio import Portfolio
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.schemas.portfolio import PortfolioCreate, PositionUpsert
from app.schemas.sim_account import SimOrderCreate

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _make_portfolio(db_session, name="QA组合", account_type="simulated",
                    total_capital=1_000_000, is_default=False) -> Portfolio:
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        is_default=int(is_default),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_symbol(db_session, symbol="600000", asset_type="stock", theme="银行") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type=asset_type,
        market="cn",
        theme=theme,
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


# ----------------------------------------------------------------------------
# 1. GET /portfolios 列表
# ----------------------------------------------------------------------------

def test_list_portfolios_empty_returns_empty_list(db_session):
    """【P1-3 API 测试】无组合时 list_portfolios 返回空列表。"""
    result = list_portfolios(db=db_session)
    assert result == []


def test_list_portfolios_returns_created_portfolio(db_session):
    """【P1-3 API 测试】list_portfolios 返回已创建的组合列表。"""
    _make_portfolio(db_session, name="QA-A")
    _make_portfolio(db_session, name="QA-B")

    result = list_portfolios(db=db_session)
    assert len(result) == 2
    names = {p.name for p in result}
    assert names == {"QA-A", "QA-B"}


# ----------------------------------------------------------------------------
# 2. POST /portfolios 创建组合
# ----------------------------------------------------------------------------

def test_create_portfolio_simulated_seeds_cash_ledger(db_session):
    """【P1-3 API 测试】create_portfolio 对 simulated 账户自动注入初始资金流水。"""
    payload = PortfolioCreate(
        name="QA-Sim",
        account_type="simulated",
        total_capital=500_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )

    result = create_portfolio(payload, db=db_session)
    assert result.id is not None
    assert result.name == "QA-Sim"
    assert result.account_type == "simulated"
    assert result.total_capital == 500_000
    assert result.is_default == 0  # bool → int

    # 验证 CashLedger 已被 ensure_sim_account_seed 注入
    ledgers = db_session.query(CashLedger).filter(CashLedger.portfolio_id == result.id).all()
    assert len(ledgers) == 1
    assert ledgers[0].entry_type == "deposit"
    assert ledgers[0].amount == 500_000


def test_create_portfolio_real_account_no_cash_ledger(db_session):
    """【P1-3 API 测试】create_portfolio 对 real 账户不注入资金流水。"""
    payload = PortfolioCreate(
        name="QA-Real",
        account_type="real",
        total_capital=200_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )

    result = create_portfolio(payload, db=db_session)
    assert result.account_type == "real"

    ledgers = db_session.query(CashLedger).filter(CashLedger.portfolio_id == result.id).all()
    assert len(ledgers) == 0


# ----------------------------------------------------------------------------
# 3. GET /portfolios/{id}/positions 持仓列表
# ----------------------------------------------------------------------------

def test_list_positions_returns_position_list(db_session):
    """【P1-3 API 测试】list_positions 返回持仓列表。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600001")
    # 直接通过 upsert_position 创建持仓
    upsert_position(
        portfolio.id,
        PositionUpsert(symbol_id=sym.id, quantity=100, avg_cost=10.0, latest_price=12.0),
        db=db_session,
    )

    result = list_positions(portfolio.id, db=db_session)
    assert len(result) == 1
    pos = result[0]
    assert pos.portfolio_id == portfolio.id
    assert pos.symbol_id == sym.id
    assert pos.quantity == 100
    assert pos.avg_cost == 10.0
    assert pos.latest_price == 12.0
    assert pos.market_value == 1200.0
    assert pos.position_pct == round(1200.0 / 1_000_000, 4)


def test_list_positions_portfolio_not_found_raises_404(db_session):
    """【P1-3 API 测试】list_positions 不存在的 portfolio_id 抛 404。"""
    with pytest.raises(HTTPException) as exc:
        list_positions(99999, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 4. POST /portfolios/{id}/positions upsert 持仓
# ----------------------------------------------------------------------------

def test_upsert_position_creates_new_position(db_session):
    """【P1-3 API 测试】upsert_position 创建新持仓。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600002")
    payload = PositionUpsert(symbol_id=sym.id, quantity=200, avg_cost=15.0, latest_price=18.0)

    result = upsert_position(portfolio.id, payload, db=db_session)
    assert result.id is not None
    assert result.portfolio_id == portfolio.id
    assert result.symbol_id == sym.id
    assert result.quantity == 200
    assert result.avg_cost == 15.0
    assert result.latest_price == 18.0
    assert result.market_value == 200 * 18.0
    assert result.position_pct == round(200 * 18.0 / 1_000_000, 4)
    assert result.asset_type == "stock"
    assert result.theme == "银行"


def test_upsert_position_updates_existing_position(db_session):
    """【P1-3 API 测试】upsert_position 更新已存在的持仓。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600003")
    # 第一次创建
    upsert_position(
        portfolio.id,
        PositionUpsert(symbol_id=sym.id, quantity=100, avg_cost=10.0, latest_price=10.0),
        db=db_session,
    )
    # 第二次更新
    result = upsert_position(
        portfolio.id,
        PositionUpsert(symbol_id=sym.id, quantity=300, avg_cost=12.0, latest_price=14.0),
        db=db_session,
    )
    assert result.quantity == 300
    assert result.avg_cost == 12.0
    assert result.latest_price == 14.0
    assert result.market_value == 300 * 14.0


def test_upsert_position_zero_capital_no_division_error(db_session):
    """【P1-3 API 测试】total_capital=0 时 position_pct 应为 0，不抛除零异常。"""
    portfolio = _make_portfolio(db_session, name="QA-Zero", total_capital=0)
    sym = _make_symbol(db_session, symbol="600004")
    payload = PositionUpsert(symbol_id=sym.id, quantity=100, avg_cost=10.0, latest_price=12.0)

    result = upsert_position(portfolio.id, payload, db=db_session)
    assert result.position_pct == 0
    assert result.market_value == 1200.0


def test_upsert_position_symbol_not_found_raises_404(db_session):
    """【P1-3 API 测试】symbol_id 不存在时 upsert_position 抛 404。"""
    portfolio = _make_portfolio(db_session)
    payload = PositionUpsert(symbol_id=99999, quantity=100, avg_cost=10.0)
    with pytest.raises(HTTPException) as exc:
        upsert_position(portfolio.id, payload, db=db_session)
    assert exc.value.status_code == 404


def test_upsert_position_falls_back_to_avg_cost_when_no_price(db_session):
    """【P1-3 API 测试】latest_price=None 且无 DailyBar 时回退到 avg_cost。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600005")
    payload = PositionUpsert(symbol_id=sym.id, quantity=100, avg_cost=10.0, latest_price=None)

    result = upsert_position(portfolio.id, payload, db=db_session)
    assert result.latest_price == 10.0  # 回退到 avg_cost
    assert result.market_value == 100 * 10.0


# ----------------------------------------------------------------------------
# 5. DELETE /portfolios/{id}/positions/{symbol_id}
# ----------------------------------------------------------------------------

def test_delete_position_success(db_session):
    """【P1-3 API 测试】delete_position 删除成功返回 {"deleted": True}。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600006")
    upsert_position(
        portfolio.id,
        PositionUpsert(symbol_id=sym.id, quantity=100, avg_cost=10.0, latest_price=10.0),
        db=db_session,
    )

    result = delete_position(portfolio.id, sym.id, db=db_session)
    assert result == {"deleted": True}
    # 验证已删除
    positions = list_positions(portfolio.id, db=db_session)
    assert len(positions) == 0


def test_delete_position_not_found_raises_404(db_session):
    """【P1-3 API 测试】删除不存在的持仓抛 404。"""
    portfolio = _make_portfolio(db_session)
    with pytest.raises(HTTPException) as exc:
        delete_position(portfolio.id, 99999, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 6. GET /portfolios/{id}/sim-account 模拟账户快照
# ----------------------------------------------------------------------------

def test_get_sim_account_snapshot_returns_summary_and_ledger(db_session):
    """【P1-3 API 测试】get_sim_account_snapshot 返回 summary + recent_trades + recent_ledger。

    通过 create_portfolio 路由创建 simulated 组合以触发 ensure_sim_account_seed，
    注入初始 deposit 流水。
    """
    portfolio = create_portfolio(
        PortfolioCreate(
            name="QA-SimSnap",
            account_type="simulated",
            total_capital=1_000_000,
            investable_ratio=0.9,
            cash_reserve_ratio=0.1,
        ),
        db=db_session,
    )

    result = get_sim_account_snapshot(
        portfolio.id, trade_limit=8, ledger_limit=8, db=db_session,
    )
    assert hasattr(result, "summary")
    assert hasattr(result, "recent_trades")
    assert hasattr(result, "recent_ledger")
    # create_portfolio 已注入初始资金
    assert result.summary.cash_balance == 1_000_000
    assert result.summary.available_cash == 1_000_000
    assert result.summary.market_value == 0.0
    assert result.summary.position_count == 0
    assert len(result.recent_ledger) == 1
    assert result.recent_ledger[0].entry_type == "deposit"


def test_get_sim_account_snapshot_portfolio_not_found_raises_404(db_session):
    """【P1-3 API 测试】get_sim_account_snapshot 不存在的 portfolio_id 抛 404。"""
    with pytest.raises(HTTPException) as exc:
        get_sim_account_snapshot(99999, trade_limit=8, ledger_limit=8, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 7. POST /portfolios/{id}/sim-orders 模拟下单
# ----------------------------------------------------------------------------

def test_create_sim_order_buy_success(db_session):
    """【P1-3 API 测试】create_sim_order 买入成功，生成 order + trade + ledger。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600010")
    payload = SimOrderCreate(symbol_id=sym.id, side="buy", quantity=100, price=10.0)

    result = create_sim_order(portfolio.id, payload, db=db_session)
    assert result.order.side == "buy"
    assert result.order.filled_quantity == 100  # cn 1 手 = 100 股
    assert result.order.filled_price == 10.0
    assert result.order.filled_amount == 1000.0
    assert result.trade.symbol_id == sym.id
    assert result.trade.realized_pnl is None  # 买入无 realized_pnl
    # 验证 DB 中确实创建了 order/trade
    orders = db_session.query(SimOrder).filter(SimOrder.portfolio_id == portfolio.id).all()
    assert len(orders) == 1
    trades = db_session.query(SimTrade).filter(SimTrade.portfolio_id == portfolio.id).all()
    assert len(trades) == 1
    # 验证扣减现金
    assert result.summary.cash_balance == 1_000_000 - 1000.0


def test_create_sim_order_symbol_not_found_raises_404(db_session):
    """【P1-3 API 测试】create_sim_order symbol 不存在抛 404。"""
    portfolio = _make_portfolio(db_session)
    payload = SimOrderCreate(symbol_id=99999, side="buy", quantity=100, price=10.0)
    with pytest.raises(HTTPException) as exc:
        create_sim_order(portfolio.id, payload, db=db_session)
    assert exc.value.status_code == 404


def test_create_sim_order_portfolio_not_found_raises_404(db_session):
    """【P1-3 API 测试】create_sim_order portfolio 不存在抛 404。"""
    sym = _make_symbol(db_session, symbol="600011")
    payload = SimOrderCreate(symbol_id=sym.id, side="buy", quantity=100, price=10.0)
    with pytest.raises(HTTPException) as exc:
        create_sim_order(99999, payload, db=db_session)
    assert exc.value.status_code == 404
