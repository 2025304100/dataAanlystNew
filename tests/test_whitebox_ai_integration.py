"""白盒测试 - WP-AI.8 集成测试。

跨 WP-AI.1 ~ WP-AI.6 多模块的端到端集成测试：
1. test_ai_response_end_to_end - 从上下文包构建到响应生成端到端
2. test_failover_during_request - 请求过程中主备切换
3. test_cache_used_on_second_request - 相同请求第二次走缓存
4. test_audit_record_created_for_draft - 草稿创建审计记录
5. test_audit_no_sensitive_data - 审计记录无敏感数据
6. test_session_cleanup_removes_expired - 过期会话清理
7. test_ai_does_not_block_business - AI 失败不阻塞业务

设计要点：
- 集成多个 WP-AI 模块（context_pack / tools / drafts / audit / failover / cache / session_retention）
- 验证跨模块协作的正确性
- 验证关键约束（不阻塞业务 / 不泄漏敏感数据 / 缓存命中 / 主备切换）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.ai_profile import (
    HEALTH_DEGRADED,
    HEALTH_DOWN,
    HEALTH_HEALTHY,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI_COMPATIBLE,
    PURPOSE_ALL,
)
from app.models.ai_session import (
    AIActionAudit,
    AIMessage,
    AISession,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
)
from app.services import ai_profile_service
from app.services.ai import audit as ai_audit
from app.services.ai import response as ai_response
from app.services.ai import session_retention
from app.services.ai_cache import AICache, reset_ai_cache
from app.services.ai.context_pack import build_context_pack, has_sensitive_info
from app.services.ai.drafts.indicator import draft_indicator
from app.services.ai_failover import (
    AIFailoverManager,
    get_failover_manager,
    reset_failover_manager,
)
from app.services.ai.tools import call_tool
from app.services.ai_session_service import (
    add_action_audit,
    add_message,
    confirm_action,
    create_session,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_symbol(db_session, **kwargs):
    from app.models.symbol import Symbol

    defaults = {
        "symbol": "600010",
        "name": "测试股票",
        "asset_type": "stock",
        "market": "cn",
        "board": "main",
        "is_st": 0,
        "is_active": 1,
    }
    defaults.update(kwargs)
    sym = Symbol(**defaults)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id, *, trade_date=None, close=10.5):
    from app.models.daily_bar import DailyBar

    if trade_date is None:
        trade_date = _utcnow_naive().date()
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=10.0,
        high=11.0,
        low=9.8,
        close=close,
        volume=1000000.0,
        amount=10500000.0,
        turnover_rate=0.01,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_score(db_session, symbol_id, *, trade_date=None):
    from app.models.score import Score

    if trade_date is None:
        trade_date = _utcnow_naive().date()
    s = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=75.5,
        quality_grade="B",
        timing_score=68.2,
        stage="hold",
        action="watch",
        priority_score=72.0,
        data_credibility=0.92,
        weight_mode="manual",
        calc_batch_id="integ-batch",
    )
    db_session.add(s)
    db_session.commit()
    return s


def _make_profile(
    db_session,
    *,
    name="primary",
    provider=PROVIDER_OPENAI_COMPATIBLE,
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    priority=0,
    is_enabled=True,
    is_fallback=False,
    purpose=PURPOSE_ALL,
    daily_request_limit=100,
    health_status=HEALTH_HEALTHY,
):
    profile = ai_profile_service.create_profile(
        db_session,
        name=name,
        provider=provider,
        model=model,
        base_url=base_url,
        auth_type="none",
        priority=priority,
        is_enabled=is_enabled,
        is_fallback=is_fallback,
        purpose=purpose,
        daily_request_limit=daily_request_limit,
    )
    # create_profile 默认设置 health_status=HEALTH_UNKNOWN，需手动覆盖
    if health_status != HEALTH_HEALTHY:
        profile.health_status = health_status
        db_session.commit()
    return profile


@pytest.fixture(autouse=True)
def _reset_ai_singletons():
    """每个测试前后重置 AI 单例，避免相互污染。"""
    reset_ai_cache()
    reset_failover_manager()
    yield
    reset_ai_cache()
    reset_failover_manager()


# ----------------------------------------------------------------------------
# 1. test_ai_response_end_to_end - 端到端：上下文包构建 → 工具调用 → 响应生成
# ----------------------------------------------------------------------------


def test_ai_response_end_to_end(db_session):
    """【WP-AI.8】从上下文包构建到 AI 响应生成的端到端流程。

    流程：
    1. 准备业务数据（Symbol / DailyBar / Score）
    2. build_context_pack 构造受控上下文包
    3. call_tool("get_symbol_research") 调用只读工具
    4. 工具返回数据 → build_response 构造统一响应
    5. 响应包含 answer/evidence/warnings/suggested_actions/metadata
    6. metadata 含 data_as_of / model_version
    """
    # ── 1. 准备数据 ──
    sym = _make_symbol(db_session, symbol="600100", name="端到端测试股")
    _make_daily_bar(db_session, sym.id, close=15.6)
    _make_score(db_session, sym.id)

    # ── 2. 构造上下文包 ──
    pack = build_context_pack(
        db_session,
        user_question="600100 最近走势如何？",
        source_page="research",
        references={"symbol_id": sym.id},
    )
    assert pack.user_question == "600100 最近走势如何？"
    assert pack.source_page == "research"
    # 上下文包不含敏感信息
    assert has_sensitive_info(pack) is False
    # 元数据含 data_as_of / model_version
    assert "data_as_of" in pack.metadata
    assert "model_version" in pack.metadata

    # ── 3. 调用只读工具 ──
    tool_result = call_tool("get_symbol_research", db_session, pack)
    assert tool_result["status"] == "ok"
    data = tool_result["data"]
    assert data["symbol"]["symbol"] == "600100"
    assert data["latest_bar"]["close"] == 15.6
    assert data["latest_score"]["quality_grade"] == "B"

    # ── 4. 构造 AI 统一响应 ──
    resp = ai_response.build_response(
        answer=f"600100 当前质量评分 B，收盘价 15.6 元，量价偏强。",
        evidence=[
            {
                "type": "kline",
                "source": "daily_bar",
                "content": f"close={data['latest_bar']['close']}",
                "confidence": 0.9,
            },
            {
                "type": "score",
                "source": "score",
                "content": f"quality_grade={data['latest_score']['quality_grade']}",
                "confidence": 0.85,
            },
        ],
        warnings=["量能略低"],
        suggested_actions=[
            {"action_type": "draft_indicator", "description": "建议添加 RSI 指标"}
        ],
        metadata={
            "data_as_of": pack.metadata["data_as_of"],
            "model_version": pack.metadata["model_version"],
            "rule_version": pack.metadata.get("rule_version"),
            "provider_used": "openai",
            "latency_ms": 350,
        },
    )

    # ── 5. 响应结构完整 ──
    d = resp.to_dict()
    assert set(d.keys()) == {
        "answer", "evidence", "warnings", "suggested_actions", "draft", "metadata"
    }
    assert "600100" in d["answer"]
    assert len(d["evidence"]) == 2
    assert d["evidence"][0]["confidence"] == 0.9
    assert d["warnings"] == ["量能略低"]
    assert d["suggested_actions"][0]["action_type"] == "draft_indicator"
    # metadata 含数据截止与模型版本
    assert d["metadata"]["data_as_of"] == pack.metadata["data_as_of"]
    assert d["metadata"]["model_version"] == pack.metadata["model_version"]
    assert d["metadata"]["provider_used"] == "openai"


# ----------------------------------------------------------------------------
# 2. test_failover_during_request - 请求过程中主备切换
# ----------------------------------------------------------------------------


def test_failover_during_request(db_session):
    """【WP-AI.8】请求过程中主 Profile 失败，自动切换到备用 Profile。

    场景：
    1. 配置主 Profile（priority=0）+ 备用 Profile（priority=1, is_fallback=True）
    2. 主 Profile 被选中
    3. 模拟主 Profile 请求超时（record_failure）
    4. failover 切换到备用 Profile
    5. 切换后的 profile_id != 原 profile_id
    6. 响应中应记录 provider_used（业务侧使用 fallback profile）
    """
    # ── 1. 配置主备 Profile ──
    primary = _make_profile(
        db_session,
        name="integ-primary",
        priority=0,
        is_fallback=False,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model="gpt-4o-mini",
    )
    fallback = _make_profile(
        db_session,
        name="integ-fallback",
        priority=1,
        is_fallback=True,
        provider=PROVIDER_OPENAI_COMPATIBLE,
        model="gpt-4o",
    )
    db_session.commit()

    # ── 2. 主 Profile 被选中 ──
    mgr = get_failover_manager()
    selected = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
    assert selected is not None
    assert selected.id == primary.id
    primary_id = primary.id

    # ── 3. 模拟主 Profile 请求超时 ──
    mgr.record_failure(
        db_session,
        profile_id=primary.id,
        error_type="timeout",
        error_msg="upstream timeout after 30s",
    )
    db_session.commit()
    db_session.refresh(primary)
    # 超时后健康状态变为 degraded
    assert primary.health_status == HEALTH_DEGRADED

    # ── 4. failover 切换到备用 Profile ──
    switched = mgr.failover(db_session, primary.id, purpose=PURPOSE_ALL)
    assert switched is not None
    assert switched.id != primary_id, "切换后的 profile_id 应不同于原 profile_id"
    assert switched.id == fallback.id
    assert switched.is_fallback is True

    # ── 5. 业务侧使用 fallback profile 构造响应 ──
    resp = ai_response.build_response(
        answer="因主模型超时，已切换到备用模型响应。",
        evidence=[{"type": "system", "source": "failover", "content": "timeout", "confidence": 1.0}],
        warnings=["主模型超时，已切换备用模型"],
        suggested_actions=[],
        metadata={
            "provider_used": switched.provider,
            "model_used": switched.model,
            "failover_from": primary.name,
            "failover_to": switched.name,
            "latency_ms": 1200,
        },
    )
    d = resp.to_dict()
    assert d["metadata"]["provider_used"] == switched.provider
    assert d["metadata"]["failover_from"] == "integ-primary"
    assert d["metadata"]["failover_to"] == "integ-fallback"
    # 切换信息在响应中可见
    assert "切换到备用模型" in d["answer"]


# ----------------------------------------------------------------------------
# 3. test_cache_used_on_second_request - 相同请求第二次走缓存
# ----------------------------------------------------------------------------


def test_cache_used_on_second_request(db_session):
    """【WP-AI.8】相同解释请求第二次直接走缓存，不重新调用 AI。

    场景：
    1. 第一次请求：构造响应并写入 AICache
    2. 第二次请求：相同 prompt + 相同 context_version → 命中缓存
    3. 第三次请求：相同 prompt + 不同 context_version → 未命中
    4. 缓存命中时返回值与首次一致
    """
    # ── 1. 第一次请求：写入缓存 ──
    cache = AICache(ttl=300)
    prompt = "解释 600010 的近期走势"
    context_version = "v1-2024-01-15"
    first_response = {
        "answer": "600010 近期走势偏强，量价齐升。",
        "evidence": [{"type": "kline", "source": "bar", "confidence": 0.85}],
        "warnings": ["量能略低"],
        "metadata": {"provider_used": "openai", "model_version": "v1"},
    }
    cache.set(prompt, context_version, first_response)
    assert cache.size() == 1

    # ── 2. 第二次请求：相同 prompt + 相同 context_version → 命中 ──
    cached = cache.get(prompt, context_version)
    assert cached is not None, "相同请求第二次应命中缓存"
    assert cached == first_response
    assert cached["answer"] == first_response["answer"]
    assert cached["evidence"] == first_response["evidence"]

    # ── 3. 第三次请求：相同 prompt + 不同 context_version → 未命中 ──
    new_version = "v2-2024-01-16"  # 数据更新后版本变化
    missed = cache.get(prompt, new_version)
    assert missed is None, "数据版本变化后应未命中缓存"

    # ── 4. 验证缓存命中时业务侧直接返回缓存响应 ──
    def _ai_or_cache(prompt_text: str, version: str):
        """模拟业务侧调用：先查缓存，未命中再"调用 AI"。"""
        cached_resp = cache.get(prompt_text, version)
        if cached_resp is not None:
            cached_resp["_from_cache"] = True
            return cached_resp
        # 模拟调用 AI
        return {"answer": "新的 AI 回答", "_from_cache": False}

    # 第二次（命中缓存）
    result2 = _ai_or_cache(prompt, context_version)
    assert result2["_from_cache"] is True
    assert result2["answer"] == first_response["answer"]

    # 数据版本变化后（未命中，重新调用 AI）
    result3 = _ai_or_cache(prompt, new_version)
    assert result3["_from_cache"] is False
    assert result3["answer"] == "新的 AI 回答"


# ----------------------------------------------------------------------------
# 4. test_audit_record_created_for_draft - 草稿创建审计记录
# ----------------------------------------------------------------------------


def test_audit_record_created_for_draft(db_session):
    """【WP-AI.8】AI 草稿流程会产生审计记录。

    场景：
    1. 创建 AI 会话
    2. 用户提问 → AI 回复（add_message）
    3. AI 调用 draft_indicator 草稿工具
    4. create_audit_record 写入审计记录（含上下文摘要）
    5. 审计记录关联到正确的 message_id
    6. 审计记录的 user_confirmed 默认为 False
    7. 确认后 user_confirmed 变为 True
    """
    # ── 1. 创建会话 ──
    session = create_session(
        db_session,
        title="草稿审计集成测试",
        source_page="research",
        provider="openai",
        model="gpt-4o-mini",
    )

    # ── 2. 用户提问 + AI 回复 ──
    user_msg = add_message(
        db_session,
        session_id=session.id,
        role="user",
        content="建议添加一个 RSI 指标",
    )
    ai_msg = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content="我建议添加 RSI(14) 指标，请确认。",
    )

    # ── 3. 构造上下文包并调用 draft_indicator ──
    pack = build_context_pack(
        db_session,
        user_question="建议添加一个 RSI 指标",
        source_page="research",
        references={},
    )
    suggestion = {
        "name": "集成测试_RSI",
        "key": "integ_rsi",
        "formula": "rsi(close, 14)",
        "value_type": "number",
    }
    draft_result = draft_indicator(db_session, pack, suggestion)
    assert draft_result["draft_type"] == "draft_indicator"
    assert draft_result["requires_confirmation"] is True
    assert draft_result["validation_status"] == "valid"

    # ── 4. 写入审计记录 ──
    audit = ai_audit.create_audit_record(
        db_session,
        session_id=session.id,
        message_id=ai_msg.id,
        context_pack={
            "source_page": "research",
            "user_question": "建议添加一个 RSI 指标",
            "metadata": {"data_as_of": "2024-01-15", "model_version": "v1"},
        },
        action_type="draft_indicator",
        suggested_payload=suggestion,
    )
    assert audit is not None
    assert audit.id is not None
    assert audit.message_id == ai_msg.id
    # 默认未确认
    assert audit.user_confirmed is False
    assert audit.confirmed_at is None

    # ── 5. 审计历史查询应能查到这条记录 ──
    history = ai_audit.get_audit_history(db_session, session.id)
    assert len(history) == 1
    assert history[0].id == audit.id
    assert history[0].action_type == "draft_indicator"

    # ── 6. 用户确认后状态变化 ──
    confirmed = confirm_action(
        db_session,
        audit.id,
        final_result={"status": "confirmed_by_user"},
    )
    assert confirmed.user_confirmed is True
    assert confirmed.confirmed_at is not None

    # ── 7. 验证 AIActionAudit 表中确实有这条记录 ──
    from sqlalchemy import select as sa_select

    audit_in_db = db_session.execute(
        sa_select(AIActionAudit).where(AIActionAudit.id == audit.id)
    ).scalars().first()
    assert audit_in_db is not None
    assert audit_in_db.user_confirmed is True
    # suggested_payload 中保留业务字段
    payload = json.loads(audit_in_db.suggested_payload)
    assert payload["name"] == "集成测试_RSI"
    assert payload["key"] == "integ_rsi"


# ----------------------------------------------------------------------------
# 5. test_audit_no_sensitive_data - 审计记录无敏感数据
# ----------------------------------------------------------------------------


def test_audit_no_sensitive_data(db_session):
    """【WP-AI.8】审计记录与导出结果均不含敏感数据。

    场景：
    1. 构造含敏感信息的上下文包（api_key/webhook_url/password）
    2. 构造含敏感字段的 suggested_payload
    3. create_audit_record 写入审计记录
    4. 审计记录中所有敏感字段被替换为 [REDACTED]
    5. export_audit_records 导出结果同样不含敏感数据
    6. 完整 K 线/大字段被替换为 [STRIPPED]
    """
    # ── 1. 创建会话与消息 ──
    session = create_session(
        db_session,
        title="审计脱敏集成测试",
        source_page="research",
    )
    msg = add_message(
        db_session,
        session_id=session.id,
        role="assistant",
        content="建议下单",
    )

    # ── 2. 含敏感信息的上下文与 payload ──
    sensitive_context = {
        "source_page": "research",
        "references": ["600010"],
        "metadata": {"data_as_of": "2024-01-15", "model_version": "v1"},
        # 敏感字段
        "api_key": "sk-INTEG-DO-NOT-LEAK-12345",
        "webhook_url": "https://hooks.example.com/notify/secret-token-abc",
        "password": "super-secret-password-99999",
        "smtp_password": "smtp-mail-password-88888",
        # 大字段（完整 K 线）
        "bars": [{"date": f"2024-01-{i:02d}", "close": 10 + i} for i in range(1, 32)],
        "kline": {"full": "complete-kline-data"},
    }
    sensitive_payload = {
        "side": "buy",
        "quantity": 100,
        "price": 10.5,
        "api_key": "sk-PAYLOAD-INTEG-DO-NOT-LEAK",
        "webhook_url": "https://hooks.example.com/payload/xyz",
        "password": "payload-password-77777",
    }

    # ── 3. 写入审计记录 ──
    audit = ai_audit.create_audit_record(
        db_session,
        session_id=session.id,
        message_id=msg.id,
        context_pack=sensitive_context,
        action_type="draft_order",
        suggested_payload=sensitive_payload,
    )
    assert audit is not None

    # ── 4. 审计记录中所有敏感值被替换 ──
    audit_text = json.dumps(
        {
            "suggested_payload": audit.suggested_payload,
            "preview_result": audit.preview_result,
        },
        ensure_ascii=False,
    )
    # 所有敏感值不应出现
    assert "sk-INTEG-DO-NOT-LEAK-12345" not in audit_text
    assert "secret-token-abc" not in audit_text
    assert "super-secret-password-99999" not in audit_text
    assert "smtp-mail-password-88888" not in audit_text
    assert "sk-PAYLOAD-INTEG-DO-NOT-LEAK" not in audit_text
    assert "payload-password-77777" not in audit_text

    # 敏感字段被替换为 [REDACTED]
    payload = json.loads(audit.suggested_payload)
    assert payload["api_key"] == "[REDACTED]"
    assert payload["password"] == "[REDACTED]"
    assert payload["side"] == "buy"  # 非敏感字段保留
    assert payload["quantity"] == 100

    # ── 5. 导出结果同样不含敏感数据 ──
    exported = ai_audit.export_audit_records(db_session, session.id)
    assert len(exported) == 1
    export_text = json.dumps(exported, ensure_ascii=False)
    assert "sk-INTEG-DO-NOT-LEAK-12345" not in export_text
    assert "sk-PAYLOAD-INTEG-DO-NOT-LEAK" not in export_text
    assert "super-secret-password-99999" not in export_text
    assert "smtp-mail-password-88888" not in export_text
    assert "payload-password-77777" not in export_text

    # ── 6. 完整 K 线被替换为 [STRIPPED] ──
    summary = json.loads(audit.preview_result)
    summary_text = json.dumps(summary, ensure_ascii=False)
    assert "[STRIPPED]" in summary_text or "bars" not in summary_text
    assert "[STRIPPED]" in summary_text or "kline" not in summary_text
    # 完整 K 线数据不应出现在审计摘要中
    assert "complete-kline-data" not in summary_text


# ----------------------------------------------------------------------------
# 6. test_session_cleanup_removes_expired - 过期会话清理
# ----------------------------------------------------------------------------


def test_session_cleanup_removes_expired(db_session):
    """【WP-AI.8】过期会话被自动归档，活跃会话保留。

    场景：
    1. 创建 3 个会话：1 个过期（100 天前）+ 1 个临界（90 天前）+ 1 个活跃（今天）
    2. 调用 cleanup_expired_sessions
    3. 过期会话被归档（status=archived）
    4. 活跃会话保持 active
    5. API 端点 /api/v1/ai/sessions/cleanup 可触发清理
    """
    # ── 1. 创建 3 个会话 ──
    expired_session = create_session(
        db_session,
        title="过期会话（100天前）",
        source_page="research",
    )
    # 手动设置 created_at 为 100 天前
    expired_session.created_at = _utcnow_naive() - timedelta(days=100)
    db_session.commit()

    boundary_session = create_session(
        db_session,
        title="临界会话（91天前）",
        source_page="research",
    )
    boundary_session.created_at = _utcnow_naive() - timedelta(days=91)
    db_session.commit()

    fresh_session = create_session(
        db_session,
        title="活跃会话（今天）",
        source_page="research",
    )
    # 不修改 created_at，默认为今天

    # ── 2. 调用 cleanup_expired_sessions ──
    cleaned = session_retention.cleanup_expired_sessions(db_session)
    # 默认保留期 90 天，所以 100 天前和 91 天前的都应被归档
    assert cleaned == 2

    # ── 3. 过期会话被归档 ──
    db_session.refresh(expired_session)
    db_session.refresh(boundary_session)
    db_session.refresh(fresh_session)
    assert expired_session.status == SESSION_STATUS_ARCHIVED
    assert boundary_session.status == SESSION_STATUS_ARCHIVED

    # ── 4. 活跃会话保持 active ──
    assert fresh_session.status == SESSION_STATUS_ACTIVE

    # ── 5. 再次清理：已归档的会话不再被处理 ──
    cleaned_again = session_retention.cleanup_expired_sessions(db_session)
    assert cleaned_again == 0

    # ── 6. API 端点也能触发清理 ──
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.router import api_router
    from app.db.session import get_db

    # 创建另一个过期会话验证 API 清理
    another_expired = create_session(
        db_session,
        title="API 清理测试会话",
        source_page="research",
    )
    another_expired.created_at = _utcnow_naive() - timedelta(days=120)
    db_session.commit()

    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)
    resp = client.post("/api/v1/ai/sessions/cleanup")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["cleaned"] == 1
    assert "retention_days" in data

    # 验证 API 触发的清理生效
    db_session.refresh(another_expired)
    assert another_expired.status == SESSION_STATUS_ARCHIVED


# ----------------------------------------------------------------------------
# 7. test_ai_does_not_block_business - AI 失败不阻塞业务
# ----------------------------------------------------------------------------


def test_ai_does_not_block_business(db_session):
    """【WP-AI.8】AI 失败时不阻塞业务流程（扫描/回测/告警/交易）。

    场景：
    1. 配置一个不可用的 AI Profile（health_status=down）
    2. select_profile 返回 None（无可用 Profile）
    3. 业务侧 try/except 包裹 AI 调用，使用默认消息
    4. 业务功能（写入 symbol/daily_bar/score）正常执行
    5. 用户看到可理解的错误消息
    """
    # ── 1. 配置不可用 Profile ──
    profile = _make_profile(
        db_session,
        name="integ-down-profile",
        priority=0,
        health_status=HEALTH_DOWN,
    )
    db_session.commit()

    # ── 2. select_profile 返回 None ──
    mgr = get_failover_manager()
    selected = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
    assert selected is None, "无可用 Profile 时应返回 None"

    # ── 3. 业务侧调用 AI 的包装函数（best-effort） ──
    def _safe_ai_explain(question: str) -> dict:
        """业务侧调用 AI：失败时返回可理解错误，不抛异常。"""
        try:
            prof = mgr.select_profile(db_session, purpose=PURPOSE_ALL)
            if prof is None:
                # 关键：不抛异常，返回可理解错误
                return ai_response.build_response(
                    answer="AI 助手暂不可用，请稍后再试。",
                    warnings=["AI 服务未配置或不可用"],
                    suggested_actions=[
                        {"action_type": "configure_ai", "description": "前往设置配置 AI 助手"}
                    ],
                    metadata={"ai_available": False},
                ).to_dict()
            # 模拟成功响应
            return ai_response.build_response(
                answer=f"AI 回答：{question}",
                metadata={"provider_used": prof.provider, "ai_available": True},
            ).to_dict()
        except Exception as exc:
            # 任何异常都不阻塞业务
            return ai_response.build_response(
                answer=f"AI 调用失败：{exc}",
                warnings=["AI 服务异常"],
                metadata={"ai_available": False},
            ).to_dict()

    # ── 4. 调用 AI：返回可理解错误，不抛异常 ──
    result = _safe_ai_explain("解释 600010 的走势")
    assert "answer" in result
    assert "AI 助手暂不可用" in result["answer"] or "AI 调用失败" in result["answer"]
    assert result["metadata"]["ai_available"] is False

    # ── 5. 业务功能正常执行（不受 AI 失败影响） ──
    # 写入 Symbol
    sym = _make_symbol(db_session, symbol="600200", name="业务正常执行测试")
    assert sym.id is not None

    # 写入 DailyBar
    bar = _make_daily_bar(db_session, sym.id, close=20.5)
    from sqlalchemy import select as sa_select
    from app.models.daily_bar import DailyBar
    bars_in_db = db_session.execute(
        sa_select(DailyBar).where(DailyBar.symbol_id == sym.id)
    ).scalars().all()
    assert len(bars_in_db) == 1
    assert bars_in_db[0].close == 20.5

    # 写入 Score
    score = _make_score(db_session, sym.id)
    from app.models.score import Score
    scores_in_db = db_session.execute(
        sa_select(Score).where(Score.symbol_id == sym.id)
    ).scalars().all()
    assert len(scores_in_db) == 1
    assert scores_in_db[0].quality_grade == "B"

    # ── 6. AI 配置变为可用后，业务侧再次调用应成功 ──
    profile.health_status = HEALTH_HEALTHY
    db_session.commit()

    result2 = _safe_ai_explain("解释 600200 的走势")
    assert "answer" in result2
    assert result2["metadata"]["ai_available"] is True
    assert "AI 回答" in result2["answer"]


# ----------------------------------------------------------------------------
# 8. test_full_session_lifecycle_with_draft_and_audit - 完整生命周期
# ----------------------------------------------------------------------------


def test_full_session_lifecycle_with_draft_and_audit(db_session):
    """【WP-AI.8】完整会话生命周期：创建 → 提问 → 草稿 → 审计 → 确认 → 删除。

    场景：
    1. 创建 AI 会话
    2. 添加用户提问与 AI 回复
    3. AI 调用 draft_alert 生成告警草稿
    4. 写入审计记录
    5. 用户确认
    6. 永久删除会话（含级联删除）
    """
    from app.services.ai.drafts.alert import draft_alert

    # ── 1. 创建会话 ──
    session = create_session(
        db_session,
        title="完整生命周期测试",
        source_page="research",
        provider="openai",
        model="gpt-4o-mini",
    )
    assert session.status == SESSION_STATUS_ACTIVE

    # ── 2. 添加用户提问与 AI 回复 ──
    user_msg = add_message(
        db_session, session_id=session.id, role="user",
        content="600010 突破前高，建议设置价格告警",
    )
    ai_msg = add_message(
        db_session, session_id=session.id, role="assistant",
        content="建议创建价格告警，阈值 12.0 元，请确认。",
    )
    assert session.total_tokens == 0  # 未指定 token

    # ── 3. AI 调用 draft_alert ──
    pack = build_context_pack(
        db_session,
        user_question="设置价格告警",
        source_page="research",
        references={},
    )
    suggestion = {
        "name": "生命周期_价格告警",
        "alert_type": "price_alert",
        "severity": "warn",
        "config_json": '{"threshold": 12.0}',
        "cooldown_minutes": 30,
    }
    draft_result = draft_alert(db_session, pack, suggestion)
    assert draft_result["draft_type"] == "draft_alert"
    assert draft_result["requires_confirmation"] is True
    assert draft_result["validation_status"] == "valid"

    # ── 4. 写入审计记录 ──
    audit = ai_audit.create_audit_record(
        db_session,
        session_id=session.id,
        message_id=ai_msg.id,
        context_pack={
            "source_page": "research",
            "metadata": {"data_as_of": "2024-01-15", "model_version": "v1"},
        },
        action_type="draft_alert",
        suggested_payload=suggestion,
    )
    assert audit is not None
    assert audit.user_confirmed is False

    # ── 5. 用户确认 ──
    confirmed = confirm_action(
        db_session, audit.id,
        final_result={"confirmed": True, "audit_id": audit.id},
    )
    assert confirmed.user_confirmed is True

    # ── 6. 验证会话、消息、审计记录都存在 ──
    session_id = session.id
    msg_id = ai_msg.id
    audit_id = audit.id

    from sqlalchemy import select as sa_select
    assert db_session.execute(
        sa_select(AISession).where(AISession.id == session_id)
    ).scalars().first() is not None
    assert db_session.execute(
        sa_select(AIMessage).where(AIMessage.id == msg_id)
    ).scalars().first() is not None
    assert db_session.execute(
        sa_select(AIActionAudit).where(AIActionAudit.id == audit_id)
    ).scalars().first() is not None

    # ── 7. 永久删除会话（级联删除消息与审计） ──
    ok = session_retention.delete_session_permanently(db_session, session_id)
    assert ok is True

    # 验证级联删除
    assert db_session.execute(
        sa_select(AISession).where(AISession.id == session_id)
    ).scalars().first() is None
    assert db_session.execute(
        sa_select(AIMessage).where(AIMessage.id == msg_id)
    ).scalars().first() is None
    assert db_session.execute(
        sa_select(AIActionAudit).where(AIActionAudit.id == audit_id)
    ).scalars().first() is None
