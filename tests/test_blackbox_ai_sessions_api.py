"""黑盒测试 - AI 创建会话 HTTP API（UAT-P0.2）。

通过 FastAPI TestClient + get_db 依赖覆盖，端到端验证 POST /api/v1/ai/sessions：
1. 创建会话（无 first_message）→ 返回 session_id/title/created_at，messages 为空
2. 创建会话 + 首条消息 → mock LLM 成功，返回 user + assistant 两条消息
3. 未配置 Profile → 400 + 中文 "AI 助手未配置，请前往设置"
4. 模型超时/限流降级 → 503 + 降级响应（answer 含 "AI 服务暂时不可用"）
5. 主备切换标注 → warnings 包含主备切换说明
6. 三步确认（副作用草稿）→ 审计记录写入（user_confirmed=False），不执行业务写
7. 凭据脱敏 → 响应中无 API Key/Webhook/邮箱密码等敏感字段

project_memory 硬约束：
- 永不返回明文 Secret
- AI 失败不阻塞业务（503 + 降级响应）
- 三步确认：副作用只生成草稿，不执行写操作
- 错误使用统一错误协议，中文文案
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.ai_profile import (
    HEALTH_HEALTHY,
    PROVIDER_OPENAI_COMPATIBLE,
    PURPOSE_ALL,
)
from app.services import ai_profile_service
from app.services.ai.llm_client import LLMCallResult
from app.services.ai_cache import reset_ai_cache
from app.services.ai_failover import reset_failover_manager
from app.services.secret_store import reset_secret_store


pytestmark = pytest.mark.blackbox


# ----------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """每个测试前后重置 AI 单例，避免相互污染。"""
    reset_secret_store()
    reset_ai_cache()
    reset_failover_manager()
    yield
    reset_secret_store()
    reset_ai_cache()
    reset_failover_manager()


@pytest.fixture()
def client(db_session):
    """构造 TestClient，依赖覆盖让 get_db 返回测试 session。"""
    from app.main import app

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_profile(
    db_session,
    *,
    name="qa-primary",
    provider=PROVIDER_OPENAI_COMPATIBLE,
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    priority=0,
    is_enabled=True,
    is_fallback=False,
    purpose=PURPOSE_ALL,
    secret_value=None,
    daily_request_limit=100,
    health_status=HEALTH_HEALTHY,
):
    """创建测试用 AI Profile（参考 test_whitebox_ai_integration._make_profile）。"""
    profile = ai_profile_service.create_profile(
        db_session,
        name=name,
        provider=provider,
        model=model,
        base_url=base_url,
        auth_type="bearer" if secret_value else "none",
        secret_value=secret_value,
        priority=priority,
        is_enabled=is_enabled,
        is_fallback=is_fallback,
        purpose=purpose,
        daily_request_limit=daily_request_limit,
    )
    # create_profile 默认设置 health_status=HEALTH_UNKNOWN，需手动覆盖为 healthy
    if health_status != HEALTH_HEALTHY:
        profile.health_status = health_status
        db_session.commit()
    else:
        profile.health_status = HEALTH_HEALTHY
        db_session.commit()
    return profile


def _make_llm_success(
    profile,
    *,
    raw_response=None,
    failover_happened=False,
    failover_from=None,
    failover_to=None,
):
    """构造成功的 LLMCallResult。"""
    if raw_response is None:
        raw_response = json.dumps(
            {
                "answer": "你好！我是 A 股量化投资助手，有什么可以帮您？",
                "evidence": [],
                "warnings": [],
                "suggested_actions": [],
                "draft": None,
            },
            ensure_ascii=False,
        )
    return LLMCallResult(
        success=True,
        raw_response=raw_response,
        profile_used=profile,
        failover_happened=failover_happened,
        failover_from=failover_from,
        failover_to=failover_to,
        latency_ms=200,
        prompt_tokens=50,
        completion_tokens=100,
        total_tokens=150,
    )


def _make_llm_failure(
    *,
    failover_happened=False,
    failover_from=None,
    failover_to=None,
    error_type="timeout",
    error_message="请求超时",
):
    """构造失败的 LLMCallResult（主备均不可用）。"""
    return LLMCallResult(
        success=False,
        error_type=error_type,
        error_message=error_message,
        profile_used=None,
        failover_from=failover_from,
        failover_to=failover_to,
        failover_happened=failover_happened,
        latency_ms=5000,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
    )


def _collect_keys(obj) -> set[str]:
    """递归收集 JSON 对象的所有键名（小写）。"""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k).lower())
            keys.update(_collect_keys(v))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(_collect_keys(item))
    return keys


# ----------------------------------------------------------------------------
# 1. 创建会话（无 first_message）
# ----------------------------------------------------------------------------


def test_create_session_without_first_message(client, db_session):
    """场景1：POST /api/v1/ai/sessions 无首条消息，返回空会话。

    断言：返回 session_id、title、created_at，messages 为空数组。
    """
    profile = _make_profile(db_session, name="qa-s1-profile")
    db_session.commit()

    resp = client.post(
        "/api/v1/ai/sessions",
        json={"profile_id": profile.id},
    )
    assert resp.status_code == 200, f"创建会话失败：{resp.text}"
    data = resp.json()

    # 必填字段完整（前端依赖）
    assert "session_id" in data, "响应缺少 session_id"
    assert "title" in data, "响应缺少 title"
    assert "created_at" in data, "响应缺少 created_at"
    assert "messages" in data, "响应缺少 messages"

    # messages 为空数组
    assert data["messages"] == [], "无首条消息时 messages 应为空数组"
    # 无首条消息时 response 为 None
    assert data["response"] is None, "无首条消息时 response 应为 None"

    # session_id 为正整数
    assert isinstance(data["session_id"], int) and data["session_id"] > 0, (
        "session_id 应为正整数"
    )
    # title 默认为"新 AI 会话"
    assert data["title"] == "新 AI 会话", f"默认标题应为'新 AI 会话'，实际：{data['title']}"
    # created_at 为 ISO 字符串
    assert isinstance(data["created_at"], str) and len(data["created_at"]) > 0, (
        "created_at 应为非空 ISO 字符串"
    )


# ----------------------------------------------------------------------------
# 2. 创建会话 + 首条消息
# ----------------------------------------------------------------------------


def test_create_session_with_first_message(client, db_session, monkeypatch):
    """场景2：POST /api/v1/ai/sessions 含首条消息，返回 user + assistant 两条消息。

    mock call_llm_with_failover 返回成功，断言 messages 包含 user + assistant 两条。
    """
    profile = _make_profile(db_session, name="qa-s2-profile")
    db_session.commit()

    # Mock LLM 调用返回成功
    def _mock_call_llm(*args, **kwargs):
        return _make_llm_success(profile)

    monkeypatch.setattr(
        "app.api.routes.ai_sessions.call_llm_with_failover", _mock_call_llm
    )

    resp = client.post(
        "/api/v1/ai/sessions",
        json={"profile_id": profile.id, "first_message": "你好"},
    )
    assert resp.status_code == 200, f"创建会话失败：{resp.text}"
    data = resp.json()

    # messages 包含 user + assistant 两条
    assert len(data["messages"]) == 2, (
        f"应返回 user + assistant 两条消息，实际 {len(data['messages'])} 条"
    )
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant"], f"消息角色顺序错误：{roles}"

    # user 消息内容为首条消息
    assert data["messages"][0]["content"] == "你好", "user 消息内容应为首条消息"

    # assistant 消息内容为 LLM 返回的 answer（JSON 序列化后）
    assistant_content = data["messages"][1]["content"]
    assert "你好" in assistant_content or "量化" in assistant_content, (
        "assistant 消息应包含 LLM 回答内容"
    )

    # response 字段非空，包含 answer
    assert data["response"] is not None, "有首条消息时 response 不应为 None"
    assert "answer" in data["response"], "response 应包含 answer 字段"


# ----------------------------------------------------------------------------
# 3. 未配置 Profile
# ----------------------------------------------------------------------------


def test_create_session_no_profile_configured(client, db_session):
    """场景3：无 profile_id 且无默认 Profile → 400 + 中文提示。

    断言：返回 400 + "AI 助手未配置，请前往设置"。
    """
    # db_session 为空（fresh SQLite），无任何 Profile
    resp = client.post(
        "/api/v1/ai/sessions",
        json={},
    )
    assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}：{resp.text}"
    body = resp.json()

    # 统一错误协议必填字段
    for key in ("error_code", "user_message", "impact", "retryable", "correlation_id"):
        assert key in body, f"统一错误响应缺少字段 {key}：{body}"

    # 中文提示
    assert body["user_message"] == "AI 助手未配置，请前往设置", (
        f"user_message 应为'AI 助手未配置，请前往设置'，实际：{body['user_message']}"
    )
    assert body["error_code"] == "AI_CONFIG_MISSING"


# ----------------------------------------------------------------------------
# 4. 模型超时/限流降级
# ----------------------------------------------------------------------------


def test_create_session_llm_failure_degraded(client, db_session, monkeypatch):
    """场景4：LLM 全失败 → 503 + 降级响应（answer 含"AI 服务暂时不可用"）。

    mock call_llm_with_failover 返回 success=False，断言响应 503 + 降级响应。
    """
    profile = _make_profile(db_session, name="qa-s4-profile")
    db_session.commit()

    def _mock_call_llm(*args, **kwargs):
        return _make_llm_failure(
            failover_happened=True,
            failover_from="qa-s4-profile",
            failover_to="qa-s4-fallback",
            error_type="timeout",
            error_message="主备模型均不可用",
        )

    monkeypatch.setattr(
        "app.api.routes.ai_sessions.call_llm_with_failover", _mock_call_llm
    )

    resp = client.post(
        "/api/v1/ai/sessions",
        json={"profile_id": profile.id, "first_message": "分析一下大盘"},
    )
    assert resp.status_code == 503, f"期望 503，实际 {resp.status_code}：{resp.text}"
    data = resp.json()

    # 降级响应仍保留会话与消息（不阻塞业务）
    assert "session_id" in data, "降级响应应包含 session_id"
    assert len(data["messages"]) == 2, "降级响应仍应包含 user + assistant 两条消息"

    # 降级 answer 包含"AI 服务暂时不可用"
    response_dict = data["response"]
    assert "AI 服务暂时不可用" in response_dict["answer"], (
        f"降级响应 answer 应包含'AI 服务暂时不可用'，实际：{response_dict['answer']}"
    )
    # warnings 包含降级提示
    warnings_text = json.dumps(response_dict["warnings"], ensure_ascii=False)
    assert "降级" in warnings_text, (
        f"warnings 应包含降级提示，实际：{response_dict['warnings']}"
    )


# ----------------------------------------------------------------------------
# 5. 主备切换标注
# ----------------------------------------------------------------------------


def test_create_session_failover_warning(client, db_session, monkeypatch):
    """场景5：主备切换 → warnings 包含主备切换说明。

    mock call_llm_with_failover 返回 success=True + failover_happened=True，
    断言 warnings 包含主备切换说明。
    """
    profile = _make_profile(db_session, name="qa-s5-primary")
    db_session.commit()

    def _mock_call_llm(*args, **kwargs):
        return _make_llm_success(
            profile,
            failover_happened=True,
            failover_from="qa-s5-primary",
            failover_to="qa-s5-fallback",
        )

    monkeypatch.setattr(
        "app.api.routes.ai_sessions.call_llm_with_failover", _mock_call_llm
    )

    resp = client.post(
        "/api/v1/ai/sessions",
        json={"profile_id": profile.id, "first_message": "分析 600010"},
    )
    assert resp.status_code == 200, f"创建会话失败：{resp.text}"
    data = resp.json()
    response_dict = data["response"]

    # warnings 包含主备切换说明
    warnings_text = json.dumps(response_dict["warnings"], ensure_ascii=False)
    assert "切换到备用模型" in warnings_text, (
        f"warnings 应包含主备切换说明，实际：{response_dict['warnings']}"
    )
    assert "qa-s5-primary" in warnings_text, "warnings 应包含主模型名称"
    assert "qa-s5-fallback" in warnings_text, "warnings 应包含备用模型名称"

    # metadata 标注 failover
    assert response_dict["metadata"].get("failover_happened") is True, (
        "metadata 应标注 failover_happened=True"
    )
    assert response_dict["metadata"].get("failover_from") == "qa-s5-primary"
    assert response_dict["metadata"].get("failover_to") == "qa-s5-fallback"


# ----------------------------------------------------------------------------
# 6. 三步确认（副作用草稿）
# ----------------------------------------------------------------------------


def test_create_session_draft_audit(client, db_session, monkeypatch):
    """场景6：LLM 返回 draft → 审计记录写入（user_confirmed=False），不执行业务写。

    mock LLM 返回含 draft 字段，断言审计记录被写入（user_confirmed=False），
    不执行任何业务写操作（final_result 为 None）。
    """
    profile = _make_profile(db_session, name="qa-s6-profile")
    db_session.commit()

    draft_payload = {
        "draft_type": "draft_indicator",
        "name": "QA_RSI",
        "key": "qa_rsi_14",
        "formula": "rsi(close, 14)",
        "value_type": "number",
    }
    raw_response = json.dumps(
        {
            "answer": "建议添加 RSI(14) 指标，请确认。",
            "evidence": [],
            "warnings": [],
            "suggested_actions": [
                {"action_type": "draft_indicator", "description": "添加 RSI 指标"}
            ],
            "draft": draft_payload,
        },
        ensure_ascii=False,
    )

    def _mock_call_llm(*args, **kwargs):
        return _make_llm_success(profile, raw_response=raw_response)

    monkeypatch.setattr(
        "app.api.routes.ai_sessions.call_llm_with_failover", _mock_call_llm
    )

    resp = client.post(
        "/api/v1/ai/sessions",
        json={"profile_id": profile.id, "first_message": "帮我加一个 RSI 指标"},
    )
    assert resp.status_code == 200, f"创建会话失败：{resp.text}"
    data = resp.json()
    session_id = data["session_id"]

    # 通过 API 查询审计记录
    audit_resp = client.get(f"/api/v1/ai/sessions/{session_id}/audit")
    assert audit_resp.status_code == 200, f"查询审计记录失败：{audit_resp.text}"
    audit_data = audit_resp.json()
    assert audit_data["count"] >= 1, "应写入至少一条审计记录"

    audit_record = audit_data["items"][0]
    # 三步确认第一步：user_confirmed 为 False（待用户确认）
    assert audit_record["user_confirmed"] is False, (
        "审计记录 user_confirmed 应为 False（待用户确认）"
    )
    # action_type 为 draft_indicator
    assert audit_record["action_type"] == "draft_indicator", (
        f"action_type 应为 draft_indicator，实际：{audit_record['action_type']}"
    )
    # suggested_payload 保留业务字段
    payload = audit_record["suggested_payload"]
    assert payload["name"] == "QA_RSI", "suggested_payload 应保留 draft 的 name 字段"
    assert payload["key"] == "qa_rsi_14", "suggested_payload 应保留 draft 的 key 字段"

    # 不执行任何业务写操作：final_result 为 None（未执行）
    assert audit_record["final_result"] is None, (
        "final_result 应为 None（未执行业务写操作）"
    )
    assert audit_record["confirmed_at"] is None, (
        "confirmed_at 应为 None（用户未确认）"
    )

    # 响应中的 draft 字段也包含草稿内容
    assert data["response"]["draft"] is not None, "response 应包含 draft 字段"


# ----------------------------------------------------------------------------
# 7. 凭据脱敏
# ----------------------------------------------------------------------------


def test_create_session_no_sensitive_data(client, db_session, monkeypatch):
    """场景7：响应中无 API Key/Webhook/邮箱密码等敏感字段（只保留上下文摘要）。

    创建含 secret_value 的 Profile，mock LLM 成功，
    断言响应中无敏感字段名、无明文 secret_value。
    """
    secret_value = "sk-qa-s7-secret-DO-NOT-LEAK-1234567890abcdef"
    profile = _make_profile(
        db_session,
        name="qa-s7-profile",
        secret_value=secret_value,
    )
    db_session.commit()

    def _mock_call_llm(*args, **kwargs):
        return _make_llm_success(profile)

    monkeypatch.setattr(
        "app.api.routes.ai_sessions.call_llm_with_failover", _mock_call_llm
    )

    resp = client.post(
        "/api/v1/ai/sessions",
        json={"profile_id": profile.id, "first_message": "分析 600010"},
    )
    assert resp.status_code == 200, f"创建会话失败：{resp.text}"
    data = resp.json()
    resp_text = json.dumps(data, ensure_ascii=False)

    # 1. Profile 的 secret_value 不应出现在响应中
    assert secret_value not in resp_text, "响应中泄露了 Profile 的 secret_value"

    # 2. 响应 JSON 结构中不应包含敏感字段名（作为 key）
    all_keys = _collect_keys(data)
    sensitive_keys = {
        "secret_key_ref",
        "secret_value",
        "api_key",
        "apikey",
        "password",
        "passwd",
        "webhook_url",
        "webhook_secret",
        "smtp_password",
        "app_token",
        "authorization",
        "auth_header",
        "private_key",
    }
    leaked_keys = all_keys & sensitive_keys
    assert not leaked_keys, f"响应中包含敏感字段名：{leaked_keys}"

    # 3. 消息的 context_summary 只保留上下文摘要（已脱敏）
    for msg in data["messages"]:
        if msg.get("context_summary"):
            ctx = msg["context_summary"]
            # context_summary 中不应包含明文 secret
            assert secret_value not in ctx, "context_summary 中泄露了 secret_value"

    # 4. 审计记录同样不应包含 secret_value
    session_id = data["session_id"]
    audit_resp = client.get(f"/api/v1/ai/sessions/{session_id}/audit")
    assert audit_resp.status_code == 200, f"查询审计记录失败：{audit_resp.text}"
    audit_text = json.dumps(audit_resp.json(), ensure_ascii=False)
    assert secret_value not in audit_text, "审计记录中泄露了 secret_value"


def test_stream_session_pushes_deltas_and_persists_response(client, db_session, monkeypatch):
    profile = _make_profile(db_session)

    def fake_stream(*_args, **_kwargs):
        yield {"type": "delta", "content": '{"answer":"流'}
        yield {"type": "delta", "content": '式回答","evidence":[],"warnings":[],"suggested_actions":[],"draft":null}'}
        yield {"type": "result", "result": LLMCallResult(
            success=True,
            raw_response='{"answer":"流式回答","evidence":[],"warnings":[],"suggested_actions":[],"draft":null}',
            profile_used=profile,
            latency_ms=12,
        )}

    monkeypatch.setattr("app.api.routes.ai_sessions.stream_llm_completion", fake_stream)
    with client.stream("POST", "/api/v1/ai/sessions/stream", json={
        "profile_id": profile.id, "message": "请流式回答", "source_page": "settings",
    }) as resp:
        body = resp.read().decode("utf-8")
    assert resp.status_code == 200
    assert "event: session" in body
    assert body.count("event: delta") == 2
    assert "event: done" in body
    sessions = client.get("/api/v1/ai/sessions").json()["items"]
    messages = client.get(f"/api/v1/ai/sessions/{sessions[0]['id']}/messages").json()["items"]
    assert json.loads(messages[-1]["content"])["answer"] == "流式回答"


def test_evidence_is_normalized_for_display():
    from app.services.ai.response import parse_llm_response

    response = parse_llm_response(json.dumps({
        "answer": "ok",
        "evidence": ["字符串证据", {"kind": "market", "origin": "daily", "value": "收盘价上涨", "score": 85}],
    }, ensure_ascii=False), {})
    assert response.evidence[0]["content"] == "字符串证据"
    assert response.evidence[1] == {
        "type": "market", "source": "daily", "content": "收盘价上涨", "confidence": 0.85,
    }
