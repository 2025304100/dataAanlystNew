"""白盒测试 - 信号规则 API (P1-3)。

覆盖 app/api/routes/signal_rules.py 路由层逻辑：
1. GET /signal-rules/presets 列表（不依赖 db）
2. GET /portfolios/{id}/signal-rule 获取规则
3. POST /portfolios/{id}/signal-rule 创建/更新规则
4. GET /signal-rules/stats/{symbol_id} 无评分时返回 404
5. POST /portfolios/{id}/signal-rule/preview 4 档预警状态机（idle/warn/ok）
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes.signal_rules import (
    get_signal_rule,
    get_signal_stats,
    list_signal_rule_presets,
    preview_signal_rule,
    save_signal_rule,
)
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.signal_rule import SignalRule
from app.models.symbol import Symbol
from app.schemas.signal_rule import (
    SignalRulePreviewRequest,
    SignalRuleUpsert,
)

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

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
# 1. GET /signal-rules/presets 预设列表
# ----------------------------------------------------------------------------

def test_list_signal_rule_presets_returns_all_presets():
    """【P1-3 API 测试】list_signal_rule_presets 返回内置 4 个预设。"""
    result = list_signal_rule_presets()
    assert len(result) == 4
    modes = {p.mode for p in result}
    assert modes == {"conservative", "balanced", "aggressive", "expert"}
    # 每个预设字段完整
    for preset in result:
        for field in (
            "mode", "rule_name", "description",
            "quality_tolerance", "timing_tolerance",
            "min_sample_count", "max_samples",
            "same_region", "same_asset_type", "same_stage", "same_action",
        ):
            assert hasattr(preset, field)


# ----------------------------------------------------------------------------
# 2. GET /portfolios/{id}/signal-rule 获取规则
# ----------------------------------------------------------------------------

def test_get_signal_rule_no_existing_returns_default_balanced(db_session):
    """【P1-3 API 测试】无规则时 get_signal_rule 返回 balanced 默认规则。"""
    portfolio = _make_portfolio(db_session)
    result = get_signal_rule(portfolio.id, db=db_session)
    assert result.portfolio_id == portfolio.id
    assert result.mode == "balanced"
    assert result.is_active is True
    # balanced preset 的 quality_tolerance=12
    assert result.quality_tolerance == 12
    assert result.timing_tolerance == 12


def test_get_signal_rule_portfolio_not_found_raises_404(db_session):
    """【P1-3 API 测试】portfolio 不存在时 get_signal_rule 抛 404。"""
    with pytest.raises(HTTPException) as exc:
        get_signal_rule(99999, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 3. POST /portfolios/{id}/signal-rule 保存规则
# ----------------------------------------------------------------------------

def test_save_signal_rule_creates_new_rule(db_session):
    """【P1-3 API 测试】save_signal_rule 创建并激活新规则。"""
    portfolio = _make_portfolio(db_session)
    payload = SignalRuleUpsert(
        rule_name="自定义",
        mode="aggressive",
        quality_tolerance=18.0,
        timing_tolerance=18.0,
        min_sample_count=3,
        max_samples=120,
        same_region=True,
        same_asset_type=True,
        same_stage=True,
        same_action=False,
    )

    result = save_signal_rule(portfolio.id, payload, db=db_session)
    assert result.id is not None
    assert result.portfolio_id == portfolio.id
    assert result.rule_name == "自定义"
    assert result.mode == "aggressive"
    assert result.quality_tolerance == 18.0
    assert result.timing_tolerance == 18.0
    assert result.same_action is False
    assert result.is_active is True

    # 验证 DB 中确实创建了一条 SignalRule
    rules = db_session.query(SignalRule).filter(SignalRule.portfolio_id == portfolio.id).all()
    assert len(rules) == 1
    assert rules[0].is_active == 1
    assert rules[0].same_action == 0  # bool → int 存储


def test_save_signal_rule_overwrites_existing_active(db_session):
    """【P1-3 API 测试】save_signal_rule 二次保存覆盖现有 active 规则。"""
    portfolio = _make_portfolio(db_session)
    payload1 = SignalRuleUpsert(rule_name="v1", mode="balanced")
    save_signal_rule(portfolio.id, payload1, db=db_session)

    payload2 = SignalRuleUpsert(rule_name="v2", mode="aggressive", quality_tolerance=20.0)
    result = save_signal_rule(portfolio.id, payload2, db=db_session)

    assert result.rule_name == "v2"
    assert result.mode == "aggressive"
    assert result.quality_tolerance == 20.0


def test_save_signal_rule_portfolio_not_found_raises_404(db_session):
    """【P1-3 API 测试】portfolio 不存在时 save_signal_rule 抛 404。"""
    payload = SignalRuleUpsert(rule_name="x", mode="balanced")
    with pytest.raises(HTTPException) as exc:
        save_signal_rule(99999, payload, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 4. GET /signal-rules/stats/{symbol_id}
# ----------------------------------------------------------------------------

def test_get_signal_stats_no_score_returns_404(db_session):
    """【P1-3 API 测试】无评分时 get_signal_stats 抛 404。

    防止前端在标的还未评分时调用统计接口崩溃。
    """
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600100")
    with pytest.raises(HTTPException) as exc:
        get_signal_stats(sym.id, portfolio_id=portfolio.id, db=db_session)
    assert exc.value.status_code == 404
    assert "评分" in exc.value.detail or "score" in exc.value.detail.lower()


def test_get_signal_stats_symbol_not_found_raises_404(db_session):
    """【P1-3 API 测试】symbol_id 不存在时 get_signal_stats 抛 404。"""
    portfolio = _make_portfolio(db_session)
    with pytest.raises(HTTPException) as exc:
        get_signal_stats(99999, portfolio_id=portfolio.id, db=db_session)
    assert exc.value.status_code == 404
    assert "symbol" in exc.value.detail.lower()


# ----------------------------------------------------------------------------
# 5. POST /portfolios/{id}/signal-rule/preview 预览状态机
# ----------------------------------------------------------------------------

def test_preview_signal_rule_no_symbol_returns_idle(db_session):
    """【P1-3 API 测试】payload.symbol_id=None 时返回 status=idle。"""
    portfolio = _make_portfolio(db_session)
    payload = SignalRulePreviewRequest(
        rule_name="x", mode="balanced", symbol_id=None,
    )
    result = preview_signal_rule(portfolio.id, payload, db=db_session)
    assert result.status == "idle"
    assert "选择" in result.message or "标的" in result.message


def test_preview_signal_rule_no_score_returns_warn(db_session):
    """【P1-3 API 测试】symbol 存在但无评分时返回 status=warn。"""
    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600200")
    payload = SignalRulePreviewRequest(
        rule_name="x", mode="balanced", symbol_id=sym.id,
    )
    result = preview_signal_rule(portfolio.id, payload, db=db_session)
    assert result.status == "warn"
    assert result.symbol_id == sym.id
    assert result.stats is None


def test_preview_signal_rule_portfolio_not_found_raises_404(db_session):
    """【P1-3 API 测试】portfolio 不存在时 preview 抛 404。"""
    payload = SignalRulePreviewRequest(
        rule_name="x", mode="balanced", symbol_id=None,
    )
    with pytest.raises(HTTPException) as exc:
        preview_signal_rule(99999, payload, db=db_session)
    assert exc.value.status_code == 404


def test_preview_signal_rule_symbol_not_found_raises_404(db_session):
    """【P1-3 API 测试】symbol_id 不存在时 preview 抛 404。"""
    portfolio = _make_portfolio(db_session)
    payload = SignalRulePreviewRequest(
        rule_name="x", mode="balanced", symbol_id=99999,
    )
    with pytest.raises(HTTPException) as exc:
        preview_signal_rule(portfolio.id, payload, db=db_session)
    assert exc.value.status_code == 404
