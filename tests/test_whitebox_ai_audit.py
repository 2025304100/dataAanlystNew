"""白盒测试 - WP-AI.6 结构化输出与审计。

覆盖：
1. AIResponse 统一响应结构（字段/默认值/序列化）
2. build_response 最小/完整响应
3. parse_llm_response JSON/纯文本/缺数据场景
4. 审计记录创建（不含敏感数据/不含完整 K 线）
5. summarize_context_for_audit 摘要去除完整数据
6. 会话保留期管理（配置/过期清理/永久删除）
7. 审计导出脱敏（不含 Secret）
8. 缺数据响应说"不知道"
9. API 端点（列表/详情/删除/审计/cleanup）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.core.config import settings
from app.db.session import get_db
from app.models.ai_session import (
    AIActionAudit,
    AIMessage,
    AISession,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_ARCHIVED,
)
from app.services import ai_session_service
from app.services.ai import audit as ai_audit
from app.services.ai import response as ai_response
from app.services.ai import session_retention


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_session(db_session, **kwargs) -> AISession:
    """创建一个最小可用的 AISession。"""
    defaults = {
        "title": "QA-Audit-Session",
        "source_page": "discovery",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    defaults.update(kwargs)
    return ai_session_service.create_session(db_session, **defaults)


def _make_message(db_session, session_id, role="assistant", content="AI 回答") -> AIMessage:
    return ai_session_service.add_message(
        db_session,
        session_id=session_id,
        role=role,
        content=content,
    )


def _make_test_client(db_session) -> TestClient:
    """构建覆盖了 get_db 依赖的 TestClient。"""
    app = FastAPI()
    app.include_router(api_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


# ----------------------------------------------------------------------------
# 1. AIResponse 统一响应结构
# ----------------------------------------------------------------------------


def test_ai_response_structure():
    """【WP-AI.6】AIResponse 包含全部必要字段。"""
    resp = ai_response.AIResponse(
        answer="600010 近期走势偏强",
        evidence=[{"type": "kline", "source": "daily_bar", "content": "连涨3日", "confidence": 0.85}],
        warnings=["量能略低"],
        suggested_actions=[{"action_type": "draft_indicator", "description": "添加 RSI 指标"}],
        draft={"indicator": "RSI", "params": {"period": 14}},
        metadata={"data_as_of": "2024-01-01", "model_version": "v1", "latency_ms": 500},
    )
    assert resp.answer == "600010 近期走势偏强"
    assert len(resp.evidence) == 1
    assert resp.evidence[0]["confidence"] == 0.85
    assert resp.warnings == ["量能略低"]
    assert len(resp.suggested_actions) == 1
    assert resp.suggested_actions[0]["action_type"] == "draft_indicator"
    assert resp.draft == {"indicator": "RSI", "params": {"period": 14}}
    assert resp.metadata["data_as_of"] == "2024-01-01"


def test_ai_response_to_dict():
    """【WP-AI.6】AIResponse.to_dict 输出符合规范的字典。"""
    resp = ai_response.AIResponse(
        answer="测试回答",
        evidence=[{"type": "rule", "source": "rule_v1", "content": "规则命中"}],
        warnings=["警告1"],
        suggested_actions=[{"action_type": "draft_alert", "description": "创建提醒"}],
        draft=None,
        metadata={"provider_used": "openai", "latency_ms": 200},
    )
    d = resp.to_dict()
    assert isinstance(d, dict)
    assert set(d.keys()) == {
        "answer", "evidence", "warnings", "suggested_actions", "draft", "metadata"
    }
    assert d["answer"] == "测试回答"
    assert d["draft"] is None
    assert d["metadata"]["provider_used"] == "openai"


def test_ai_response_to_message_content_roundtrip():
    """【WP-AI.6】to_message_content 与 from_message_content 可往返。"""
    resp = ai_response.AIResponse(
        answer="往返测试",
        evidence=[{"type": "t", "source": "s", "content": "c", "confidence": 0.5}],
        warnings=["w"],
        suggested_actions=[{"action_type": "draft_note", "description": "d"}],
        draft={"k": "v"},
        metadata={"tokens": 100},
    )
    content = resp.to_message_content()
    assert isinstance(content, str)

    restored = ai_response.AIResponse.from_message_content(content)
    assert restored.answer == "往返测试"
    assert restored.evidence == resp.evidence
    assert restored.warnings == ["w"]
    assert restored.suggested_actions == resp.suggested_actions
    assert restored.draft == {"k": "v"}
    assert restored.metadata == {"tokens": 100}


def test_ai_response_from_message_content_plain_text():
    """【WP-AI.6】from_message_content 解析失败时退化为 answer=content。"""
    restored = ai_response.AIResponse.from_message_content("这不是 JSON")
    assert restored.answer == "这不是 JSON"
    assert restored.evidence == []


def test_ai_response_default_values():
    """【WP-AI.6】AIResponse 默认值为空列表/None/空字典。"""
    resp = ai_response.AIResponse(answer="仅回答")
    assert resp.evidence == []
    assert resp.warnings == []
    assert resp.suggested_actions == []
    assert resp.draft is None
    assert resp.metadata == {}


# ----------------------------------------------------------------------------
# 2. build_response 最小/完整响应
# ----------------------------------------------------------------------------


def test_build_response_minimal():
    """【WP-AI.6】build_response 最小响应（仅 answer）。"""
    resp = ai_response.build_response(answer="最小回答")
    assert resp.answer == "最小回答"
    assert resp.evidence == []
    assert resp.warnings == []
    assert resp.suggested_actions == []
    assert resp.draft is None
    assert resp.metadata == {}


def test_build_response_full():
    """【WP-AI.6】build_response 完整响应（全部字段）。"""
    resp = ai_response.build_response(
        answer="完整回答",
        evidence=[{"type": "kline", "source": "bar", "content": "x", "confidence": 0.9}],
        warnings=["警告"],
        suggested_actions=[{"action_type": "draft_indicator", "description": "d"}],
        draft={"indicator": "MACD"},
        metadata={"model_version": "v2", "latency_ms": 300},
    )
    assert resp.answer == "完整回答"
    assert len(resp.evidence) == 1
    assert resp.warnings == ["警告"]
    assert len(resp.suggested_actions) == 1
    assert resp.draft == {"indicator": "MACD"}
    assert resp.metadata["model_version"] == "v2"


# ----------------------------------------------------------------------------
# 3. parse_llm_response JSON/纯文本/缺数据
# ----------------------------------------------------------------------------


def test_parse_llm_response_json():
    """【WP-AI.6】parse_llm_response 解析结构化 JSON 响应。"""
    raw = json.dumps({
        "answer": "600010 走势偏强",
        "evidence": [{"type": "kline", "source": "daily", "content": "连涨", "confidence": 0.8}],
        "warnings": ["量能不足"],
        "suggested_actions": [{"action_type": "draft_indicator", "description": "添加 RSI"}],
        "draft": {"indicator": "RSI"},
        "metadata": {"model_version": "v1"},
    }, ensure_ascii=False)
    resp = ai_response.parse_llm_response(raw, {"provider_used": "openai", "latency_ms": 200})
    assert resp.answer == "600010 走势偏强"
    assert len(resp.evidence) == 1
    assert resp.warnings == ["量能不足"]
    assert resp.suggested_actions[0]["action_type"] == "draft_indicator"
    assert resp.draft == {"indicator": "RSI"}
    # context_metadata 与响应 metadata 合并
    assert resp.metadata["provider_used"] == "openai"
    assert resp.metadata["model_version"] == "v1"


def test_parse_llm_response_plain_text():
    """【WP-AI.6】parse_llm_response 解析纯文本响应（非 JSON）。"""
    raw = "这是一段纯文本回答，不包含 JSON 结构。"
    resp = ai_response.parse_llm_response(raw, {"provider_used": "ollama"})
    assert resp.answer == raw
    assert resp.evidence == []
    assert resp.metadata["provider_used"] == "ollama"


def test_parse_llm_response_empty():
    """【WP-AI.6】parse_llm_response 空响应返回缺数据响应。"""
    resp = ai_response.parse_llm_response("", {"provider_used": "openai"})
    assert "我不知道" in resp.answer
    assert resp.warnings == [ai_response.NO_DATA_WARNING]
    assert resp.evidence == []


def test_parse_llm_response_json_without_answer():
    """【WP-AI.6】parse_llm_response JSON 无 answer 字段时退化为 draft。"""
    raw = json.dumps({"description": "结构化数据", "extra": {"k": "v"}}, ensure_ascii=False)
    resp = ai_response.parse_llm_response(raw, {})
    assert resp.answer == "结构化数据"
    assert resp.draft == {"description": "结构化数据", "extra": {"k": "v"}}


# ----------------------------------------------------------------------------
# 4. 缺数据响应说"不知道"
# ----------------------------------------------------------------------------


def test_no_data_response_says_unknown():
    """【WP-AI.6】缺数据响应明确说"不知道"。"""
    resp = ai_response.build_no_data_response(reason="未找到 600010 的 K 线数据")
    assert "我不知道" in resp.answer
    assert "无可用数据" in resp.answer
    assert resp.evidence == []
    assert ai_response.NO_DATA_WARNING in resp.warnings
    assert any(a["action_type"] == "request_more_data" for a in resp.suggested_actions)
    # reason 附加到 answer
    assert "未找到 600010 的 K 线数据" in resp.answer


def test_parse_llm_response_detects_no_data():
    """【WP-AI.6】parse_llm_response 自动检测"不知道"语义并补全 warnings。"""
    raw = json.dumps({"answer": "我不知道此股票的走势"}, ensure_ascii=False)
    resp = ai_response.parse_llm_response(raw, {})
    assert "我不知道" in resp.answer
    assert ai_response.NO_DATA_WARNING in resp.warnings
    assert any(a["action_type"] == "request_more_data" for a in resp.suggested_actions)


# ----------------------------------------------------------------------------
# 5. 审计记录创建（不含敏感数据/不含完整 K 线）
# ----------------------------------------------------------------------------


def test_create_audit_record_no_sensitive_data(db_session):
    """【WP-AI.6】审计记录不包含敏感数据（api_key/secret/webhook 等）。"""
    session = _make_session(db_session, title="审计敏感数据测试")
    msg = _make_message(db_session, session.id)

    context_pack = {
        "source_page": "discovery",
        "references": ["600010"],
        "metadata": {"data_as_of": "2024-01-01", "model_version": "v1"},
        # 敏感字段（不应出现在审计记录中）
        "api_key": "sk-secret-DO-NOT-LEAK",
        "webhook_url": "https://example.com/webhook-secret",
        "password": "p@ssw0rd",
        # 大字段（不应出现在审计记录中）
        "bars": [{"date": "2024-01-01", "open": 10, "close": 11} for _ in range(100)],
        "kline": {"full": "data"},
    }
    suggested_payload = {
        "indicator": "RSI",
        "params": {"period": 14},
        "api_key": "sk-in-payload-DO-NOT-LEAK",
    }

    audit = ai_audit.create_audit_record(
        db_session,
        session_id=session.id,
        message_id=msg.id,
        context_pack=context_pack,
        action_type="draft_indicator",
        suggested_payload=suggested_payload,
    )
    assert audit is not None
    assert audit.id is not None

    # 审计记录中不应出现任何敏感值
    audit_text = json.dumps({
        "suggested_payload": audit.suggested_payload,
        "preview_result": audit.preview_result,
    }, ensure_ascii=False)
    assert "sk-secret-DO-NOT-LEAK" not in audit_text
    assert "webhook-secret" not in audit_text
    assert "p@ssw0rd" not in audit_text
    assert "sk-in-payload-DO-NOT-LEAK" not in audit_text

    # 完整 K 线/行情数据不应出现
    assert "[STRIPPED]" in audit_text or "bars" not in audit_text
    assert "[STRIPPED]" in audit_text or "kline" not in audit_text

    # 敏感字段被替换为 [REDACTED]
    payload = json.loads(audit.suggested_payload)
    assert payload["api_key"] == "[REDACTED]"
    assert payload["indicator"] == "RSI"  # 非敏感字段保留


def test_create_audit_record_disabled(db_session, monkeypatch):
    """【WP-AI.6】AI_AUDIT_ENABLED=False 时 create_audit_record 返回 None。"""
    monkeypatch.setattr(settings, "AI_AUDIT_ENABLED", False)
    session = _make_session(db_session, title="审计禁用测试")
    msg = _make_message(db_session, session.id)

    audit = ai_audit.create_audit_record(
        db_session,
        session_id=session.id,
        message_id=msg.id,
        context_pack={"source_page": "discovery"},
        action_type="draft_indicator",
        suggested_payload={"indicator": "RSI"},
    )
    assert audit is None


# ----------------------------------------------------------------------------
# 6. summarize_context_for_audit 摘要去除完整数据
# ----------------------------------------------------------------------------


def test_summarize_context_strips_full_data():
    """【WP-AI.6】summarize_context_for_audit 去除完整 K 线和敏感配置。"""
    context_pack = {
        "source_page": "discovery",
        "references": ["600010", "600000"],
        "metadata": {
            "data_as_of": "2024-01-01",
            "model_version": "v1",
            "rule_version": "r1",
            "provider_used": "openai",
        },
        # 敏感字段
        "api_key": "sk-DO-NOT-LEAK",
        "secret_token": "secret-DO-NOT-LEAK",
        # 完整数据字段
        "bars": [{"o": 10, "c": 11} for _ in range(50)],
        "kline": {"full": "data"},
        "market_data": {"huge": "data"},
    }

    summary = ai_audit.summarize_context_for_audit(context_pack)

    # 关键摘要字段保留
    assert summary["source_page"] == "discovery"
    assert summary["references"] == ["600010", "600000"]
    assert summary["data_as_of"] == "2024-01-01"
    assert summary["model_version"] == "v1"

    # 敏感字段不出现
    summary_text = json.dumps(summary, ensure_ascii=False)
    assert "sk-DO-NOT-LEAK" not in summary_text
    assert "secret-DO-NOT-LEAK" not in summary_text

    # 完整数据不出现（被替换为 [STRIPPED]）
    assert "bars" not in summary or summary.get("bars") == "[STRIPPED]"
    assert "kline" not in summary or summary.get("kline") == "[STRIPPED]"


def test_summarize_context_object_duck_typing():
    """【WP-AI.6】summarize_context_for_audit 支持对象型上下文包。"""

    class FakeContextPack:
        source_page = "research"
        references = ["000001"]
        metadata = {"data_as_of": "2024-02-01", "model_version": "v2"}

    summary = ai_audit.summarize_context_for_audit(FakeContextPack())
    assert summary["source_page"] == "research"
    assert summary["references"] == ["000001"]
    assert summary["data_as_of"] == "2024-02-01"
    assert summary["model_version"] == "v2"


# ----------------------------------------------------------------------------
# 7. 审计导出脱敏（不含 Secret）
# ----------------------------------------------------------------------------


def test_audit_export_no_secrets(db_session):
    """【WP-AI.6】export_audit_records 导出结果不含任何敏感信息。"""
    session = _make_session(db_session, title="审计导出测试")
    msg = _make_message(db_session, session.id)

    # 创建一条审计记录（包含敏感字段的 payload）
    ai_audit.create_audit_record(
        db_session,
        session_id=session.id,
        message_id=msg.id,
        context_pack={
            "source_page": "discovery",
            "api_key": "sk-export-DO-NOT-LEAK",
        },
        action_type="draft_order",
        suggested_payload={
            "side": "buy",
            "price": 10.5,
            "api_key": "sk-payload-DO-NOT-LEAK",
        },
    )

    exported = ai_audit.export_audit_records(db_session, session.id)
    assert len(exported) == 1
    record = exported[0]
    assert record["action_type"] == "draft_order"
    assert record["user_confirmed"] is False

    # 导出文本中不应包含任何敏感值
    export_text = json.dumps(exported, ensure_ascii=False)
    assert "sk-export-DO-NOT-LEAK" not in export_text
    assert "sk-payload-DO-NOT-LEAK" not in export_text

    # 敏感字段被替换为 [REDACTED]
    payload = record["suggested_payload"]
    assert payload["api_key"] == "[REDACTED]"
    assert payload["side"] == "buy"  # 非敏感字段保留


def test_get_audit_history(db_session):
    """【WP-AI.6】get_audit_history 返回指定会话的审计记录。"""
    session = _make_session(db_session, title="审计历史测试")
    msg1 = _make_message(db_session, session.id, content="回答1")
    msg2 = _make_message(db_session, session.id, content="回答2")

    ai_audit.create_audit_record(
        db_session, session.id, msg1.id,
        context_pack={"source_page": "discovery"},
        action_type="draft_indicator",
        suggested_payload={"indicator": "RSI"},
    )
    ai_audit.create_audit_record(
        db_session, session.id, msg2.id,
        context_pack={"source_page": "discovery"},
        action_type="draft_alert",
        suggested_payload={"alert_type": "price"},
    )

    history = ai_audit.get_audit_history(db_session, session.id)
    assert len(history) == 2
    # 按创建时间倒序（最新的在前）
    assert history[0].action_type == "draft_alert"
    assert history[1].action_type == "draft_indicator"


# ----------------------------------------------------------------------------
# 8. 会话保留期管理
# ----------------------------------------------------------------------------


def test_session_retention_config():
    """【WP-AI.6】get_retention_days 从配置读取（默认 90）。"""
    days = session_retention.get_retention_days()
    assert days == 90  # 默认值

    # 通过 monkeypatch 修改配置
    original = settings.AI_SESSION_RETENTION_DAYS
    try:
        settings.AI_SESSION_RETENTION_DAYS = 30
        assert session_retention.get_retention_days() == 30
    finally:
        settings.AI_SESSION_RETENTION_DAYS = original


def test_cleanup_expired_sessions(db_session):
    """【WP-AI.6】cleanup_expired_sessions 归档超过保留期的 active 会话。"""
    # 创建一个会话并手动将 created_at 设为 100 天前（超过默认 90 天保留期）
    session = _make_session(db_session, title="过期会话")
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=100)
    session.created_at = old_time
    db_session.commit()

    # 创建一个新会话（不应被清理）
    fresh_session = _make_session(db_session, title="活跃会话")

    cleaned = session_retention.cleanup_expired_sessions(db_session)
    assert cleaned == 1

    # 验证过期会话已被归档
    db_session.refresh(session)
    assert session.status == SESSION_STATUS_ARCHIVED

    # 新会话仍是 active
    db_session.refresh(fresh_session)
    assert fresh_session.status == SESSION_STATUS_ACTIVE


def test_cleanup_expired_sessions_no_op_when_no_expired(db_session):
    """【WP-AI.6】无过期会话时 cleanup 返回 0。"""
    _make_session(db_session, title="活跃会话1")
    _make_session(db_session, title="活跃会话2")
    cleaned = session_retention.cleanup_expired_sessions(db_session)
    assert cleaned == 0


def test_delete_session_permanently(db_session):
    """【WP-AI.6】delete_session_permanently 物理删除会话及关联数据。"""
    session = _make_session(db_session, title="待永久删除")
    msg = _make_message(db_session, session.id, content="消息1")

    ai_audit.create_audit_record(
        db_session, session.id, msg.id,
        context_pack={"source_page": "discovery"},
        action_type="draft_indicator",
        suggested_payload={"indicator": "RSI"},
    )

    session_id = session.id
    msg_id = msg.id

    ok = session_retention.delete_session_permanently(db_session, session_id)
    assert ok is True

    # 验证会话、消息、审计全部被物理删除
    from sqlalchemy import select as sa_select
    assert db_session.execute(
        sa_select(AISession).where(AISession.id == session_id)
    ).scalars().first() is None
    assert db_session.execute(
        sa_select(AIMessage).where(AIMessage.id == msg_id)
    ).scalars().first() is None
    assert db_session.execute(
        sa_select(AIActionAudit).where(AIActionAudit.message_id == msg_id)
    ).scalars().first() is None


def test_delete_session_permanently_not_found(db_session):
    """【WP-AI.6】永久删除不存在的会话返回 False。"""
    ok = session_retention.delete_session_permanently(db_session, 999999)
    assert ok is False


# ----------------------------------------------------------------------------
# 9. API 端点
# ----------------------------------------------------------------------------


def test_api_list_sessions(db_session):
    """【WP-AI.6】GET /api/v1/ai/sessions 列出会话。"""
    _make_session(db_session, title="API 会话1")
    _make_session(db_session, title="API 会话2")

    client = _make_test_client(db_session)
    resp = client.get("/api/v1/ai/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    titles = {item["title"] for item in data["items"]}
    assert "API 会话1" in titles
    assert "API 会话2" in titles


def test_api_list_sessions_with_archived(db_session):
    """【WP-AI.6】GET /api/v1/ai/sessions?include_archived=true 包含归档会话。"""
    s1 = _make_session(db_session, title="活跃")
    s2 = _make_session(db_session, title="归档")
    ai_session_service.archive_session(db_session, s2.id)

    client = _make_test_client(db_session)
    # 默认不含归档
    resp = client.get("/api/v1/ai/sessions")
    titles = {item["title"] for item in resp.json()["items"]}
    assert "活跃" in titles
    assert "归档" not in titles

    # include_archived=true 含归档
    resp = client.get("/api/v1/ai/sessions?include_archived=true")
    titles = {item["title"] for item in resp.json()["items"]}
    assert "活跃" in titles
    assert "归档" in titles


def test_api_get_session_detail(db_session):
    """【WP-AI.6】GET /api/v1/ai/sessions/{id} 获取会话详情。"""
    session = _make_session(db_session, title="详情测试")

    client = _make_test_client(db_session)
    resp = client.get(f"/api/v1/ai/sessions/{session.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session.id
    assert data["title"] == "详情测试"


def test_api_get_session_detail_not_found(db_session):
    """【WP-AI.6】GET /api/v1/ai/sessions/{id} 不存在返回 404。"""
    client = _make_test_client(db_session)
    resp = client.get("/api/v1/ai/sessions/999999")
    assert resp.status_code == 404


def test_api_list_messages(db_session):
    """【WP-AI.6】GET /api/v1/ai/sessions/{id}/messages 获取消息列表。"""
    session = _make_session(db_session, title="消息列表测试")
    _make_message(db_session, session.id, role="user", content="用户提问")
    _make_message(db_session, session.id, role="assistant", content="AI 回答")

    client = _make_test_client(db_session)
    resp = client.get(f"/api/v1/ai/sessions/{session.id}/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    roles = [m["role"] for m in data["items"]]
    assert "user" in roles
    assert "assistant" in roles


def test_api_delete_session(db_session):
    """【WP-AI.6】DELETE /api/v1/ai/sessions/{id} 永久删除会话。"""
    session = _make_session(db_session, title="待 API 删除")
    _make_message(db_session, session.id, content="消息")

    client = _make_test_client(db_session)
    resp = client.delete(f"/api/v1/ai/sessions/{session.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["session_id"] == session.id

    # 再次获取应返回 404
    resp = client.get(f"/api/v1/ai/sessions/{session.id}")
    assert resp.status_code == 404


def test_api_delete_session_not_found(db_session):
    """【WP-AI.6】DELETE /api/v1/ai/sessions/{id} 不存在返回 404。"""
    client = _make_test_client(db_session)
    resp = client.delete("/api/v1/ai/sessions/999999")
    assert resp.status_code == 404


def test_api_get_audit(db_session):
    """【WP-AI.6】GET /api/v1/ai/sessions/{id}/audit 获取审计记录（脱敏）。"""
    session = _make_session(db_session, title="API 审计测试")
    msg = _make_message(db_session, session.id)

    ai_audit.create_audit_record(
        db_session, session.id, msg.id,
        context_pack={
            "source_page": "discovery",
            "api_key": "sk-api-audit-DO-NOT-LEAK",
        },
        action_type="draft_indicator",
        suggested_payload={
            "indicator": "RSI",
            "api_key": "sk-payload-api-DO-NOT-LEAK",
        },
    )

    client = _make_test_client(db_session)
    resp = client.get(f"/api/v1/ai/sessions/{session.id}/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session.id
    assert data["count"] == 1

    # 审计响应文本中不应包含任何敏感值
    resp_text = json.dumps(data, ensure_ascii=False)
    assert "sk-api-audit-DO-NOT-LEAK" not in resp_text
    assert "sk-payload-api-DO-NOT-LEAK" not in resp_text


def test_api_cleanup_sessions(db_session):
    """【WP-AI.6】POST /api/v1/ai/sessions/cleanup 清理过期会话。"""
    # 创建一个过期会话
    session = _make_session(db_session, title="API 清理测试")
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=100)
    session.created_at = old_time
    db_session.commit()

    client = _make_test_client(db_session)
    resp = client.post("/api/v1/ai/sessions/cleanup")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["cleaned"] == 1
    assert "retention_days" in data

    # 验证会话已归档
    db_session.refresh(session)
    assert session.status == SESSION_STATUS_ARCHIVED


def test_api_cleanup_sessions_no_op(db_session):
    """【WP-AI.6】POST /api/v1/ai/sessions/cleanup 无过期会话时 cleaned=0。"""
    _make_session(db_session, title="活跃会话")

    client = _make_test_client(db_session)
    resp = client.post("/api/v1/ai/sessions/cleanup")
    assert resp.status_code == 200
    assert resp.json()["cleaned"] == 0
