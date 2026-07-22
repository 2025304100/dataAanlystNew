"""白盒测试 - WP-AI.3 受控上下文包。

覆盖：
1. 基本构建
2. 不含敏感信息
3. 大小限制
4. 不可信内容标记
5. 元数据附加
6. 脱敏 API Key
7. 脱敏 Webhook
8. 去除 None 字段
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.ai.context_pack import (
    ContextPack,
    DEFAULT_MAX_CONTEXT_TOKENS,
    build_context_pack,
    has_sensitive_info,
    sanitize_context,
    to_prompt_dict,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_symbol(db_session, *, symbol="600010", name="测试股票", market="cn", asset_type="stock"):
    """创建一个测试标的。"""
    from app.models.symbol import Symbol
    sym = Symbol(
        symbol=symbol, name=name, asset_type=asset_type,
        market=market, board="main", is_st=0, is_active=1,
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id, *, trade_date=None, close=10.5):
    """创建一条测试行情。"""
    from app.models.daily_bar import DailyBar
    if trade_date is None:
        trade_date = _utcnow_naive().date()
    bar = DailyBar(
        symbol_id=symbol_id, trade_date=trade_date,
        open=10.0, high=11.0, low=9.8, close=close,
        volume=1000000.0, amount=10500000.0, turnover_rate=0.01,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_portfolio(db_session, *, name="测试组合", account_type="simulated", total_capital=100000.0):
    """创建一个测试组合。"""
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=name, account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.8, cash_reserve_ratio=0.2,
        currency="CNY", is_default=0,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_score(db_session, symbol_id, *, trade_date=None):
    """创建一条测试评分。"""
    from app.models.score import Score
    if trade_date is None:
        trade_date = _utcnow_naive().date()
    s = Score(
        symbol_id=symbol_id, trade_date=trade_date,
        quality_score=75.5, quality_grade="B",
        timing_score=68.2, stage="hold", action="watch",
        priority_score=72.0,
        trend_score=70.0, momentum_score=65.0,
        volatility_score=80.0, liquidity_score=85.0,
        breadth_score=60.0, event_score=50.0,
        data_credibility=0.92,
        weight_mode="manual",
        calc_batch_id="test-batch",
    )
    db_session.add(s)
    db_session.commit()
    return s


def _make_news(db_session, symbol_id, *, title="测试新闻", source="test_source"):
    """创建一条测试新闻。"""
    from app.models.news_event import NewsEvent
    n = NewsEvent(
        symbol_id=symbol_id, symbol="600010",
        title=title, source=source, event_type="news",
        sentiment="neutral", strength=1,
        raw_score=0.0, effective_score=0.0, risk_level="low",
        published_at=_utcnow_naive(),
    )
    db_session.add(n)
    db_session.commit()
    return n


# ----------------------------------------------------------------------------
# 1. 基本构建
# ----------------------------------------------------------------------------


def test_build_context_pack_basic(db_session):
    """【WP-AI.3】build_context_pack 返回包含必填字段的 ContextPack。"""
    sym = _make_symbol(db_session)
    _make_daily_bar(db_session, sym.id)
    _make_score(db_session, sym.id)

    pack = build_context_pack(
        db_session,
        user_question="600010 近期走势如何？",
        source_page="research",
        references={"symbol_id": sym.id},
    )

    assert isinstance(pack, ContextPack)
    assert pack.user_question == "600010 近期走势如何？"
    assert pack.source_page == "research"
    assert pack.references == {"symbol_id": sym.id}
    # data_status 必须有结构（即使部分字段为空）
    assert isinstance(pack.data_status, dict)
    assert "data_cutoff" in pack.data_status
    assert "missing_items" in pack.data_status
    # scoring_info 必须有结构
    assert isinstance(pack.scoring_info, dict)
    assert "score_config" in pack.scoring_info
    assert "factor_contribution" in pack.scoring_info
    # capabilities 必须有结构
    assert isinstance(pack.capabilities, dict)
    assert "current_gate" in pack.capabilities
    assert "allowed_next_step" in pack.capabilities
    # market_summary 应该被填充（因为提供了 symbol_id 且有行情）
    assert pack.market_summary is not None
    assert pack.market_summary["symbol_id"] == sym.id
    assert pack.market_summary["close"] == 10.5


# ----------------------------------------------------------------------------
# 2. 不含敏感信息
# ----------------------------------------------------------------------------


def test_context_pack_no_sensitive_info(db_session):
    """【WP-AI.3】构建完成的上下文包不含敏感信息。"""
    sym = _make_symbol(db_session, symbol="600011")
    _make_daily_bar(db_session, sym.id)

    pack = build_context_pack(
        db_session,
        user_question="分析 600011",
        source_page="research",
        references={"symbol_id": sym.id},
    )

    # 自检：has_sensitive_info 应返回 False
    assert has_sensitive_info(pack) is False, "上下文包中仍残留敏感信息"


# ----------------------------------------------------------------------------
# 3. 大小限制
# ----------------------------------------------------------------------------


def test_context_pack_size_limit(db_session):
    """【WP-AI.3】上下文包大小不超过 max_context_tokens。"""
    sym = _make_symbol(db_session, symbol="600012")
    _make_daily_bar(db_session, sym.id)
    _make_score(db_session, sym.id)
    # 添加多条新闻以触发截断
    for i in range(20):
        _make_news(db_session, sym.id, title=f"新闻 {i} " * 50)

    # 用很小的上限强制截断
    pack = build_context_pack(
        db_session,
        user_question="分析 600012",
        source_page="research",
        references={"symbol_id": sym.id},
        max_context_tokens=200,  # 极小上限
    )

    # 估算 token 数应 <= 上限（截断后）
    import json
    from app.services.ai.context_pack import _estimate_tokens
    text = json.dumps(pack.__dict__ if hasattr(pack, '__dict__') else
                      {k: getattr(pack, k) for k in pack.__dataclass_fields__},
                      ensure_ascii=False, default=str)
    # 允许少量超出（截断后附加的元数据）
    assert _estimate_tokens({k: getattr(pack, k) for k in pack.__dataclass_fields__}) <= 200 + 50

    # 应有截断标记
    assert pack.metadata.get("_size_truncated") is True or len(pack.untrusted_content) < 20


# ----------------------------------------------------------------------------
# 4. 不可信内容标记
# ----------------------------------------------------------------------------


def test_untrusted_content_marked(db_session):
    """【WP-AI.3】新闻/第三方文本标记 is_untrusted: true。"""
    sym = _make_symbol(db_session, symbol="600013")
    _make_daily_bar(db_session, sym.id)
    _make_news(db_session, sym.id, title="利好消息")
    _make_news(db_session, sym.id, title="风险提示")

    pack = build_context_pack(
        db_session,
        user_question="600013 有什么新闻？",
        source_page="research",
        references={"symbol_id": sym.id},
    )

    assert len(pack.untrusted_content) >= 2
    for item in pack.untrusted_content:
        assert item.get("is_untrusted") is True, "新闻未被标记为不可信"
        assert item.get("type") == "news"


# ----------------------------------------------------------------------------
# 5. 元数据附加
# ----------------------------------------------------------------------------


def test_context_pack_metadata_attached(db_session):
    """【WP-AI.3】AI 回复必须附 data_as_of/model_version/rule_version/based_on。"""
    sym = _make_symbol(db_session, symbol="600014")
    _make_daily_bar(db_session, sym.id)
    _make_score(db_session, sym.id)

    pack = build_context_pack(
        db_session,
        user_question="分析 600014",
        source_page="research",
        references={"symbol_id": sym.id, "portfolio_id": 99},
    )

    meta = pack.metadata
    assert "data_as_of" in meta
    assert "model_version" in meta
    assert "rule_version" in meta
    assert "based_on" in meta
    assert "built_at" in meta
    # based_on 应包含 references 中的非空 ID
    assert any("symbol_id" in s for s in meta["based_on"])
    assert any("portfolio_id" in s for s in meta["based_on"])
    # data_as_of 应该与 data_cutoff 一致
    assert meta["data_as_of"] == pack.data_status.get("data_cutoff")


# ----------------------------------------------------------------------------
# 6. 脱敏 API Key
# ----------------------------------------------------------------------------


def test_sanitize_removes_api_key():
    """【WP-AI.3】sanitize_context 去除 API Key。"""
    pack = ContextPack(
        user_question="我的 API Key 是 sk-abcdef1234567890xyz",
        source_page="research",
        references={"api_key": "sk-test-secret-12345678"},
        data_status={"source": "api_key=sk-secret1234567890"},
        scoring_info={},
        capabilities={},
        market_summary=None,
        backtest_summary=None,
        performance_summary=None,
        untrusted_content=[],
        metadata={},
    )

    sanitized = sanitize_context(pack)

    # 所有字段中的 API Key 都应被替换
    assert "sk-abcdef1234567890xyz" not in sanitized.user_question
    assert "sk-test-secret-12345678" not in str(sanitized.references)
    assert "sk-secret1234567890" not in str(sanitized.data_status)
    # 替换标记应出现
    assert "[REDACTED_API_KEY]" in sanitized.user_question or "[REDACTED" in sanitized.user_question
    # 自检
    assert has_sensitive_info(sanitized) is False


# ----------------------------------------------------------------------------
# 7. 脱敏 Webhook URL
# ----------------------------------------------------------------------------


def test_sanitize_removes_webhook_url():
    """【WP-AI.3】sanitize_context 去除 Webhook URL。"""
    pack = ContextPack(
        user_question="请访问 https://example.com/webhook/secret123 触发通知",
        source_page="task",
        references={"webhook_url": "https://hooks.example.com/notify/abcdef"},
        data_status={},
        scoring_info={},
        capabilities={},
        market_summary=None,
        backtest_summary=None,
        performance_summary=None,
        untrusted_content=[
            {"title": "外部链接 https://example.com/webhook/test", "is_untrusted": True}
        ],
        metadata={},
    )

    sanitized = sanitize_context(pack)

    # Webhook URL 应被替换
    assert "https://example.com/webhook/secret123" not in sanitized.user_question
    assert "https://hooks.example.com/notify/abcdef" not in str(sanitized.references)
    # 不可信内容中的 webhook 也应被脱敏（但 is_untrusted 标记保留）
    for item in sanitized.untrusted_content:
        assert "https://example.com/webhook/test" not in item.get("title", "")
        assert item.get("is_untrusted") is True  # 标记保留
    # 自检
    assert has_sensitive_info(sanitized) is False


# ----------------------------------------------------------------------------
# 8. 去除 None 字段
# ----------------------------------------------------------------------------


def test_to_prompt_dict_strips_none(db_session):
    """【WP-AI.3】to_prompt_dict 去除 None 字段。"""
    # 不提供 portfolio_id，使 backtest_summary/performance_summary 为 None
    sym = _make_symbol(db_session, symbol="600015")
    _make_daily_bar(db_session, sym.id)

    pack = build_context_pack(
        db_session,
        user_question="分析 600015",
        source_page="research",
        references={"symbol_id": sym.id},  # 不提供 portfolio_id
    )

    prompt = to_prompt_dict(pack)

    # None 字段不应出现
    assert "backtest_summary" not in prompt
    assert "performance_summary" not in prompt
    # market_summary 应该存在（有行情数据）
    assert "market_summary" in prompt
    # 必填字段应该都在
    assert "user_question" in prompt
    assert "source_page" in prompt
    assert "references" in prompt
    assert "data_status" in prompt
    assert "scoring_info" in prompt
    assert "capabilities" in prompt
    assert "metadata" in prompt
    assert "untrusted_content" in prompt


# ----------------------------------------------------------------------------
# 额外：默认上限
# ----------------------------------------------------------------------------


def test_default_max_context_tokens():
    """【WP-AI.3】默认上限为 8192。"""
    assert DEFAULT_MAX_CONTEXT_TOKENS == 8192
