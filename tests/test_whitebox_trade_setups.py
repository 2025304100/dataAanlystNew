"""白盒测试 - 交易计划 API (P1-3)。

覆盖 app/api/routes/trade_setups.py 路由层逻辑：
1. POST /trade-setups/generate 生成计划
2. GET /trade-setups/latest/{symbol_id} 获取最新计划
3. PATCH /trade-setups/{setup_id}/tranches 更新分批计划
4. _safe_datetime / _ensure_datetime_attrs 兼容 MySQL datetime 字符串
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.routes.trade_setups import (
    _safe_datetime,
    generate_trade_setup,
    get_latest_trade_setup,
    update_trade_setup_tranches,
)
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, PortfolioRule
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.schemas.trade_setup import (
    TradeSetupGenerateRequest,
    TradeSetupTranche,
    TradeSetupTrancheUpdateRequest,
)

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_portfolio(db_session, name="QA-Portfolio") -> Portfolio:
    p = Portfolio(
        name=name,
        account_type="real",
        total_capital=1_000_000,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        is_default=0,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_rule(db_session, portfolio_id: int) -> PortfolioRule:
    """创建激活的 PortfolioRule，允许 start 阶段 0.1 仓位。"""
    r = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="active",
        max_single_position_pct=0.1,
        max_sector_position_pct=0.3,
        max_stock_position_pct=0.4,
        max_etf_position_pct=0.3,
        max_loss_per_trade_pct=0.05,
        max_open_positions=10,
        stage_limits_json='{"stock":{"start":0.1,"accel":0.15}}',
        is_active=1,
    )
    db_session.add(r)
    db_session.commit()
    return r


def _make_symbol(db_session, symbol="600000", market="cn") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market=market,
        theme="银行",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_bars(db_session, symbol_id: int, base_price=10.0, days=30):
    """插入 days 根日K线，每天使用不同 trade_date 避免唯一约束冲突。"""
    today = date.today()
    from datetime import timedelta
    for i in range(days):
        # 从今天往前推 days 天，保证 trade_date 唯一
        trade_date = today - timedelta(days=days - i - 1)
        db_session.add(DailyBar(
            symbol_id=symbol_id,
            trade_date=trade_date,
            open=base_price,
            high=base_price * 1.02,
            low=base_price * 0.98,
            close=base_price,
            volume=1_000_000.0,
        ))
        base_price *= 1.001
    db_session.commit()


def _make_score(db_session, symbol_id: int, stage="start", action="open") -> Score:
    s = Score(
        symbol_id=symbol_id,
        trade_date=date.today(),
        quality_score=70.0,
        quality_grade="B",
        timing_score=65.0,
        stage=stage,
        action=action,
        priority_score=70.0,
        calc_batch_id="qa",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


# ----------------------------------------------------------------------------
# 1. _safe_datetime 单元测试
# ----------------------------------------------------------------------------

def test_safe_datetime_handles_none():
    """【P1-3 API 测试】_safe_datetime(None) 返回 None。"""
    assert _safe_datetime(None) is None


def test_safe_datetime_handles_string_formats():
    """【P1-3 API 测试】_safe_datetime 兼容多种 MySQL datetime 字符串格式。"""
    # 标准 datetime 字符串
    assert _safe_datetime("2026-01-15 10:30:00") == datetime(2026, 1, 15, 10, 30, 0)
    # ISO 格式
    assert _safe_datetime("2026-01-15T10:30:00") == datetime(2026, 1, 15, 10, 30, 0)
    # 带微秒
    assert _safe_datetime("2026-01-15 10:30:00.123456") == datetime(2026, 1, 15, 10, 30, 0, 123456)
    # 仅日期
    assert _safe_datetime("2026-01-15") == datetime(2026, 1, 15, 0, 0, 0)
    # 非法字符串返回 None
    assert _safe_datetime("not-a-date") is None


def test_safe_datetime_passes_through_datetime_object():
    """【P1-3 API 测试】_safe_datetime 对 datetime 对象直接返回。"""
    dt = datetime(2026, 1, 1, 12, 0, 0)
    assert _safe_datetime(dt) is dt


# ----------------------------------------------------------------------------
# 2. POST /trade-setups/generate
# ----------------------------------------------------------------------------

def test_generate_trade_setup_no_score_returns_404(db_session):
    """【P1-3 API 测试】无评分时 generate_trade_setup 抛 404。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600100")
    payload = TradeSetupGenerateRequest(
        portfolio_id=portfolio.id, symbol_id=sym.id,
    )
    with pytest.raises(HTTPException) as exc:
        generate_trade_setup(payload, db=db_session)
    assert exc.value.status_code == 404
    assert "score" in exc.value.detail.lower() or "portfolio" in exc.value.detail.lower()


def test_generate_trade_setup_portfolio_not_found_returns_404(db_session):
    """【P1-3 API 测试】portfolio 不存在时 generate_trade_setup 抛 404。"""
    sym = _make_symbol(db_session, symbol="600101")
    _make_score(db_session, sym.id)
    payload = TradeSetupGenerateRequest(
        portfolio_id=99999, symbol_id=sym.id,
    )
    with pytest.raises(HTTPException) as exc:
        generate_trade_setup(payload, db=db_session)
    assert exc.value.status_code == 404


def test_generate_trade_setup_symbol_not_found_returns_404(db_session):
    """【P1-3 API 测试】symbol 不存在时 generate_trade_setup 抛 404。"""
    portfolio = _make_portfolio(db_session)
    payload = TradeSetupGenerateRequest(
        portfolio_id=portfolio.id, symbol_id=99999,
    )
    with pytest.raises(HTTPException) as exc:
        generate_trade_setup(payload, db=db_session)
    assert exc.value.status_code == 404


def test_generate_trade_setup_success(db_session):
    """【P1-3 API 测试】generate_trade_setup 在数据齐全时生成 TradeSetup。"""
    portfolio = _make_portfolio(db_session)
    _make_rule(db_session, portfolio.id)
    sym = _make_symbol(db_session, symbol="600200")
    _make_bars(db_session, sym.id, base_price=10.0, days=30)
    score = _make_score(db_session, sym.id, stage="start", action="open")

    payload = TradeSetupGenerateRequest(
        portfolio_id=portfolio.id, symbol_id=sym.id,
    )
    result = generate_trade_setup(payload, db=db_session)
    assert result.id is not None
    assert result.portfolio_id == portfolio.id
    assert result.symbol_id == sym.id
    assert result.score_id == score.id
    assert result.stage == "start"
    assert result.action == "open"
    assert result.recommended_position_pct is not None
    assert result.recommended_position_amount is not None
    # 验证 DB 中确实有这条 setup
    setups = db_session.query(TradeSetup).filter(TradeSetup.portfolio_id == portfolio.id).all()
    assert len(setups) == 1


# ----------------------------------------------------------------------------
# 3. GET /trade-setups/latest/{symbol_id}
# ----------------------------------------------------------------------------

def test_get_latest_trade_setup_not_found_raises_404(db_session):
    """【P1-3 API 测试】get_latest_trade_setup 不存在时抛 404。"""
    portfolio = _make_portfolio(db_session)
    with pytest.raises(HTTPException) as exc:
        get_latest_trade_setup(99999, portfolio_id=portfolio.id, db=db_session)
    assert exc.value.status_code == 404


def test_get_latest_trade_setup_returns_latest(db_session):
    """【P1-3 API 测试】get_latest_trade_setup 返回最新的 TradeSetup。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600300")
    score = _make_score(db_session, sym.id)
    # 直接创建一条 TradeSetup
    setup = TradeSetup(
        portfolio_id=portfolio.id,
        symbol_id=sym.id,
        score_id=score.id,
        stage="start",
        action="open",
        entry_min=9.5,
        entry_max=10.0,
        stop_loss=9.0,
        target_price=11.0,
        recommended_position_pct=0.1,
        recommended_position_amount=100_000.0,
        risk_reward_ratio=2.0,
        allow_add_position=1,
        is_sector_overweight=0,
        is_asset_overweight=0,
        setup_reason="qa test",
    )
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)

    result = get_latest_trade_setup(sym.id, portfolio_id=portfolio.id, db=db_session)
    assert result.id == setup.id
    assert result.entry_min == 9.5
    assert result.entry_max == 10.0


# ----------------------------------------------------------------------------
# 4. PATCH /trade-setups/{setup_id}/tranches
# ----------------------------------------------------------------------------

def test_update_trade_setup_tranches_over_limit_returns_400(db_session):
    """【P1-3 API 测试】tranche 总仓位超过 recommended_position_pct 时抛 400。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600400")
    score = _make_score(db_session, sym.id)
    setup = TradeSetup(
        portfolio_id=portfolio.id,
        symbol_id=sym.id,
        score_id=score.id,
        stage="start",
        action="open",
        recommended_position_pct=0.1,  # 推荐仓位 10%
        recommended_position_amount=100_000.0,
    )
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)

    payload = TradeSetupTrancheUpdateRequest(tranche_plan=[
        TradeSetupTranche(label="A", position_pct=0.08, amount=80_000.0),
        TradeSetupTranche(label="B", position_pct=0.05, amount=50_000.0),  # 总 0.13 > 0.1
    ])
    with pytest.raises(HTTPException) as exc:
        update_trade_setup_tranches(setup.id, payload, db=db_session)
    assert exc.value.status_code == 400
    assert "exceed" in exc.value.detail.lower() or "tranche" in exc.value.detail.lower()


def test_update_trade_setup_tranches_success(db_session):
    """【P1-3 API 测试】update_trade_setup_tranches 总仓位未超限时成功保存。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600500")
    score = _make_score(db_session, sym.id)
    setup = TradeSetup(
        portfolio_id=portfolio.id,
        symbol_id=sym.id,
        score_id=score.id,
        stage="start",
        action="open",
        recommended_position_pct=0.2,
        recommended_position_amount=200_000.0,
    )
    db_session.add(setup)
    db_session.commit()
    db_session.refresh(setup)

    payload = TradeSetupTrancheUpdateRequest(tranche_plan=[
        TradeSetupTranche(label="首仓", position_pct=0.1, amount=100_000.0, trigger="突破 10 元"),
        TradeSetupTranche(label="加仓", position_pct=0.05, amount=50_000.0, trigger="回踩 9.5 元"),
    ])
    result = update_trade_setup_tranches(setup.id, payload, db=db_session)
    assert result.id == setup.id
    assert result.manual_tranche_plan_json is not None
    # manual_tranche_plan property 应解析回 list
    plan = result.manual_tranche_plan
    assert plan is not None
    assert len(plan) == 2
    assert plan[0]["label"] == "首仓"
    assert plan[1]["label"] == "加仓"


def test_update_trade_setup_tranches_setup_not_found_raises_404(db_session):
    """【P1-3 API 测试】setup_id 不存在时 update_trade_setup_tranches 抛 404。"""
    payload = TradeSetupTrancheUpdateRequest(tranche_plan=[])
    with pytest.raises(HTTPException) as exc:
        update_trade_setup_tranches(99999, payload, db=db_session)
    assert exc.value.status_code == 404
