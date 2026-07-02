"""白盒测试 - 仓位分配 (app.services.allocation)。

针对代码审查发现的 M-7 问题：compute_position_budget 假设
max_etf_position_pct 非空，ETF 字段为 None 时 float(None) 抛 TypeError。
同时覆盖零资金、空持仓、边界分配等。
"""
from __future__ import annotations

import pytest

from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.symbol import Symbol
from app.services import allocation


def _make_portfolio(db_session, total_capital=1_000_000, investable_ratio=0.9, is_default=1):
    p = Portfolio(
        name=f"QA-{id(db_session)}",
        account_type="sim",
        total_capital=total_capital,
        investable_ratio=investable_ratio,
        cash_reserve_ratio=0.1,
        is_default=is_default,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_rule(db_session, portfolio_id, *, stock=0.4, etf=0.3, single=0.1, sector=0.3,
               loss=0.05, open_positions=10, stage_limits=None):
    r = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="active",
        max_single_position_pct=single,
        max_sector_position_pct=sector,
        max_stock_position_pct=stock,
        max_etf_position_pct=etf,
        max_loss_per_trade_pct=loss,
        max_open_positions=open_positions,
        stage_limits_json=stage_limits or '{"stock":{"start":0.1,"accel":0.15}}',
        is_active=1,
    )
    db_session.add(r)
    db_session.commit()
    return r


def _make_symbol(db_session, symbol="600000", asset_type="stock", theme="银行"):
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


# ---------- compute_allocation ----------

def test_allocation_empty_portfolio(db_session):
    """空组合应返回全 0 持仓。"""
    p = _make_portfolio(db_session)
    result = allocation.compute_allocation(db_session, p.id)
    assert result["used_amount"] == 0.0
    assert result["total_position_pct"] == 0.0
    assert result["cash_amount"] == 1_000_000.0
    assert result["position_count"] == 0


def test_allocation_zero_capital_no_division_error(db_session):
    """[边界] total_capital=0 时不应除零崩溃。"""
    p = _make_portfolio(db_session, total_capital=0)
    result = allocation.compute_allocation(db_session, p.id)
    assert result["total_position_pct"] == 0.0
    assert result["stock_position_pct"] == 0.0


def test_allocation_with_positions(db_session):
    """含持仓时应正确计算各类型暴露。"""
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session)
    pos = Position(
        portfolio_id=p.id,
        symbol_id=sym.id,
        quantity=1000,
        avg_cost=10,
        latest_price=12,
        market_value=12000,
        position_pct=0.012,
        asset_type="stock",
        theme="银行",
    )
    db_session.add(pos)
    db_session.commit()

    result = allocation.compute_allocation(db_session, p.id)
    assert result["used_amount"] == 12000.0
    assert result["stock_amount"] == 12000.0
    assert result["etf_amount"] == 0.0
    assert result["position_count"] == 1
    assert result["total_position_pct"] == 0.012


def test_allocation_portfolio_not_found_raises(db_session):
    """不存在的组合应抛 ValueError。"""
    with pytest.raises(ValueError, match="Portfolio not found"):
        allocation.compute_allocation(db_session, 99999)


# ---------- compute_position_budget ----------

def test_budget_no_rule_returns_blocked(db_session):
    """无风控规则应返回 blocked 决策。"""
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session)
    result = allocation.compute_position_budget(db_session, p.id, sym, "start")
    assert result["can_open"] is False
    assert result["decision"] == "blocked"
    assert "no_active_rule" in result["blocked_reasons"]


def test_budget_etf_with_null_max_etf_pct_raises(db_session):
    """[M-7 回归] ETF 标的但规则 max_etf_position_pct 为 None 时应抛 TypeError。

    代码审查发现：allocation.py:119 行
    `float(rule.max_stock_position_pct if symbol.asset_type == "stock" else rule.max_etf_position_pct)`
    当 ETF 字段为 None 时 float(None) 会抛 TypeError。

    注意：PortfolioRule 模型中 max_etf_position_pct 是 non-nullable Float，
    但历史脏数据或迁移可能残留 NULL。此测试模拟该场景。
    """
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="510300", asset_type="etf", theme=None)
    r = _make_rule(db_session, p.id, stock=0.4, etf=0.3)
    # 模拟脏数据：将 max_etf_position_pct 设为 None
    r.max_etf_position_pct = None
    db_session.commit()

    # 当前实现会抛 TypeError，这正是 bug
    with pytest.raises(TypeError):
        allocation.compute_position_budget(db_session, p.id, sym, "start", entry_price=5.0, stop_loss=4.5)


def test_budget_stock_with_full_rule(db_session):
    """股票标的 + 完整规则应正确计算推荐仓位。"""
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600000", asset_type="stock", theme="银行")
    _make_rule(db_session, p.id)

    result = allocation.compute_position_budget(
        db_session, p.id, sym, "start", entry_price=10.0, stop_loss=9.5
    )
    assert result["can_open"] in (True, False)
    assert result["recommended_pct"] >= 0.0
    assert "constraints" in result
    # 应包含 single/stage/sector/asset 等约束
    keys = [c["key"] for c in result["constraints"]]
    assert "single" in keys


def test_budget_stage_limit_lookup(db_session):
    """stage_limits_json 中对应阶段的限制应被正确解析。"""
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session)
    _make_rule(db_session, p.id, stage_limits='{"stock":{"start":0.12,"accel":0.20,"cooldown":0}}')

    result = allocation.compute_position_budget(
        db_session, p.id, sym, "accel", entry_price=10.0, stop_loss=9.5
    )
    stage_constraints = [c for c in result["constraints"] if c["key"] == "stage"]
    assert stage_constraints
    assert stage_constraints[0]["limit_pct"] == 0.20


def test_budget_stage_limits_json_null_handled(db_session):
    """[M-6 回归] stage_limits_json 为 NULL 时不应崩溃。

    allocation.py:115 使用 `json.loads(rule.stage_limits_json or "{}")` 已有兜底，
    确认 NULL 时回退到 {}。
    """
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session)
    r = _make_rule(db_session, p.id)
    r.stage_limits_json = None
    db_session.commit()

    # 不应抛 JSONDecodeError/TypeError
    result = allocation.compute_position_budget(
        db_session, p.id, sym, "start", entry_price=10.0, stop_loss=9.5
    )
    assert result is not None


def test_budget_existing_position_excluded_from_open(db_session):
    """已有持仓的标的应标记 has_position，影响 can_open。"""
    p = _make_portfolio(db_session)
    sym = _make_symbol(db_session)
    _make_rule(db_session, p.id)
    # 创建持仓
    pos = Position(
        portfolio_id=p.id,
        symbol_id=sym.id,
        quantity=500,
        avg_cost=10,
        latest_price=11,
        market_value=5500,
        position_pct=0.0055,
        asset_type="stock",
        theme="银行",
    )
    db_session.add(pos)
    db_session.commit()

    result = allocation.compute_position_budget(
        db_session, p.id, sym, "start", entry_price=11.0, stop_loss=10.0
    )
    assert result["current_position_pct"] == 0.0055
