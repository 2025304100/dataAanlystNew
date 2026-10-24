"""Inspection AI must preserve server authority across model output and budgets."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from app.api.routes import ai_sessions
from app.models.ai_session import AIActionAudit, AIMessage, AISession
from app.models.portfolio import Portfolio
from app.schemas.ai_session import AiSessionCreateRequest
from app.services.ai.context_pack import ContextPack, _estimate_tokens, to_prompt_dict
from app.services.ai.llm_client import LLMCallResult


pytestmark = pytest.mark.whitebox


def _material_hash(pack: ContextPack) -> str:
    encoded = json.dumps(
        {"scope": pack.metadata["scope"], "material": pack.untrusted_content},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _evidence_refs(value) -> set[str]:
    """Collect only evidence actually present in the sent material."""
    if isinstance(value, dict):
        refs = {str(value["evidence_id"])} if value.get("evidence_id") else set()
        for child in value.values():
            refs.update(_evidence_refs(child))
        return refs
    if isinstance(value, list):
        return set().union(*(_evidence_refs(child) for child in value)) if value else set()
    return set()


@pytest.fixture
def portfolio(db_session):
    row = Portfolio(
        name="controlled-inspection-ai", account_type="simulated", total_capital=100000,
        investable_ratio=0.8, cash_reserve_ratio=0.2,
        status_reconciliation="RECONCILIATION_BLOCKED",
    )
    db_session.add(row)
    db_session.commit()
    return row


def _context(portfolio_id: int, *, large: bool = False) -> ContextPack:
    symbols = [
        {"symbol_id": 11, "symbol": "ONE", "evidence": [
            {"evidence_id": "bar:11", "status": "MISSING", "reason": "行情尚缺"},
        ]},
        {"symbol_id": 12, "symbol": "TWO", "evidence": [
            {"evidence_id": "score:12", "status": "STALE", "reason": "评分尚需刷新"},
        ]},
    ]
    if large:
        symbols[0]["evidence"][0]["reason"] += "用于验证上下文预算的原始事实。" * 4000
        symbols[1]["evidence"][0]["reason"] += "另一条不得被虚构为已读的事实。" * 4000
    pack = ContextPack(
        user_question="解释本组合的当前阻断，仅提供正式入口建议。",
        source_page="portfolio-inspection", references={"portfolio_id": portfolio_id},
        capabilities={"execute_tools": False, "human_confirmation_required": True},
        data_status={"credibility": "requires_evidence", "missing_items": ["资金执行证据待核实"]},
        untrusted_content=[{
            "is_untrusted": True, "kind": "inspection_evidence",
            "facts": {"data": {"snapshot_id": "server-data", "symbols": symbols}},
        }],
        metadata={
            "version": 1, "access_actor": "alice",
            "scope": {"portfolio_id": portfolio_id, "symbol_ids": [11, 12],
                      "trade_date": "2026-10-22", "step": "data"},
            "data_as_of": "2026-10-23T01:00:00+00:00",
            "generated_at": "2026-10-23T01:01:00+00:00",
            "unknowns": ["资金执行证据待核实"],
            "available_actions": [{"action_type": "VIEW_JOB", "label": "查看自动对账作业"}],
            "evidence_refs": ["bar:11", "score:12"],
        },
    )
    pack.metadata["inspection_snapshot_id"] = _material_hash(pack)
    return pack


def _offline_model(monkeypatch, output: dict, *, max_context_tokens: int = 8192):
    profile = SimpleNamespace(
        id=73, name="offline-inspection", provider="openai_compatible", model="offline-model",
        is_enabled=True, max_context_tokens=max_context_tokens, max_tokens=512,
    )
    monkeypatch.setattr(ai_sessions, "get_failover_manager", lambda: Mock(select_profile=Mock(return_value=profile)))
    monkeypatch.setattr(ai_sessions.ai_profile_service, "get_profile", lambda *args: profile)
    captured = []
    original_prompt = ai_sessions._build_system_prompt

    def capture_prompt(pack):
        captured.append(deepcopy(to_prompt_dict(pack)))
        return original_prompt(pack)

    monkeypatch.setattr(ai_sessions, "_build_system_prompt", capture_prompt)
    llm = Mock(return_value=LLMCallResult(
        success=True, profile_used=profile, raw_response=json.dumps(output, ensure_ascii=False),
        latency_ms=1, prompt_tokens=10, completion_tokens=10, total_tokens=20,
    ))
    monkeypatch.setattr(ai_sessions, "call_llm_with_failover", llm)
    draft_audit = Mock()
    monkeypatch.setattr(ai_sessions.ai_audit, "create_audit_record", draft_audit)
    return captured, llm, draft_audit


def _create(db_session, portfolio: Portfolio, pack: ContextPack):
    result = ai_sessions.create_session_with_context(
        AiSessionCreateRequest(
            profile_id=73, source_page="portfolio-inspection",
            references={"portfolio_id": portfolio.id}, message="分析当前阻断",
        ), db_session, controlled_pack=pack,
    )
    return result if isinstance(result, dict) else json.loads(result.body)


def _stored_response(db_session, session_id: int):
    message = db_session.scalar(select(AIMessage).where(
        AIMessage.session_id == session_id, AIMessage.role == "assistant",
    ))
    assert message is not None
    return json.loads(message.content), json.loads(message.metadata_json)


def test_controlled_metadata_cannot_be_replaced_by_model_claims(db_session, portfolio, monkeypatch):
    pack = _context(portfolio.id)
    trusted = deepcopy(pack.metadata)
    spoof = {
        "scope": {"portfolio_id": 999999, "symbol_ids": [999999]},
        "data_as_of": "2099-01-01", "generated_at": "2099-01-01",
        "inspection_snapshot_id": "model-forged-snapshot", "unknowns": [],
        "access_actor": "mallory", "version": 999,
        "available_actions": [{"action_type": "FORCE_RESUME"}], "evidence_refs": ["fake:999"],
        "provider_used": "model-forged-provider", "model_used": "model-forged-model",
        "latency_ms": -1, "tokens": -1,
    }
    _offline_model(monkeypatch, {"answer": "先核实执行来源", "metadata": spoof})
    body = _create(db_session, portfolio, pack)
    response = body["response"]
    stored, stored_metadata = _stored_response(db_session, body["session_id"])
    for key, value in trusted.items():
        assert response["metadata"][key] == value, key
        assert stored["metadata"][key] == value, key
        assert stored_metadata[key] == value, key
    assert response["metadata"]["provider_used"] == "openai_compatible"
    assert response["metadata"]["model_used"] == "offline-model"
    assert response["metadata"]["latency_ms"] == 1
    assert response["metadata"]["tokens"] == 20


def test_model_draft_is_never_persisted_as_executable_audit_or_business_write(db_session, portfolio, monkeypatch):
    pack = _context(portfolio.id)
    before = (portfolio.total_capital, portfolio.status_reconciliation, portfolio.auto_trade_enabled)
    _, _, draft_audit = _offline_model(monkeypatch, {
        "answer": "仅供参考", "draft": {
            "draft_type": "draft_order", "portfolio_id": portfolio.id,
            "side": "buy", "quantity": 100, "force_resume": True,
        },
    })
    body = _create(db_session, portfolio, pack)
    stored, _ = _stored_response(db_session, body["session_id"])
    assert body["response"]["draft"] is None
    assert stored["draft"] is None
    draft_audit.assert_not_called()
    assert db_session.scalar(select(func.count()).select_from(AIActionAudit)) == 0
    db_session.refresh(portfolio)
    assert (portfolio.total_capital, portfolio.status_reconciliation, portfolio.auto_trade_enabled) == before


@pytest.mark.parametrize("allow_view_job", [True, False])
def test_suggested_actions_use_server_whitelist_even_when_model_expands_it(db_session, portfolio, monkeypatch, allow_view_job):
    pack = _context(portfolio.id)
    if not allow_view_job:
        pack.metadata["available_actions"] = []
    _offline_model(monkeypatch, {
        "answer": "请走正式核实入口",
        "suggested_actions": [
            {"action_type": "VIEW_JOB", "description": "查看本组合核对作业"},
            {"action_type": "FORCE_RESUME", "description": "模型请求强制恢复"},
            {"action_type": "RUN_SQL", "description": "模型请求写数据库"},
        ],
        "metadata": {"available_actions": [{"action_type": "FORCE_RESUME"}, {"action_type": "RUN_SQL"}]},
    })
    body = _create(db_session, portfolio, pack)
    stored, _ = _stored_response(db_session, body["session_id"])
    expected = ["VIEW_JOB"] if allow_view_job else []
    assert [item["action_type"] for item in body["response"]["suggested_actions"]] == expected
    assert [item["action_type"] for item in stored["suggested_actions"]] == expected


def test_small_budget_updates_original_pack_and_hash_to_actual_sent_material(db_session, portfolio, monkeypatch):
    pack = _context(portfolio.id, large=True)
    original_scope = deepcopy(pack.metadata["scope"])
    original_material = deepcopy(pack.untrusted_content)
    original_hash = pack.metadata["inspection_snapshot_id"]
    captured, llm, _ = _offline_model(monkeypatch, {"answer": "材料不全，请回正式来源核实"}, max_context_tokens=1024)
    body = _create(db_session, portfolio, pack)

    # A budget that removes all facts must skip the model. If any facts survive,
    # compare against the actual pack used to construct its prompt.
    if captured:
        sent = captured[0]
    else:
        llm.assert_not_called()
        assert body["response"]["metadata"]["model_called"] is False
        assert body["response"]["evidence"] == []
        assert body["response"]["draft"] is None
        sent = to_prompt_dict(pack)
    assert pack.untrusted_content != original_material
    assert _estimate_tokens(asdict(pack)) <= 1024
    assert sent["untrusted_content"] == pack.untrusted_content
    assert pack.metadata["scope"] == original_scope == sent["metadata"]["scope"]
    assert pack.metadata["access_actor"] == sent["metadata"]["access_actor"] == "alice"
    assert pack.metadata["inspection_snapshot_id"] != original_hash
    assert pack.metadata["inspection_snapshot_id"] == _material_hash(pack)
    assert sent["metadata"]["inspection_snapshot_id"] == _material_hash(pack)
    retained_refs = _evidence_refs(sent["untrusted_content"])
    assert set(pack.metadata["evidence_refs"]) == retained_refs
    assert set(sent["metadata"]["evidence_refs"]) == retained_refs
    assert any(any(word in str(item) for word in ("预算", "截断", "未进入", "材料不足"))
               for item in pack.metadata["unknowns"])
    metadata = body["response"]["metadata"]
    assert metadata["inspection_snapshot_id"] == _material_hash(pack)
    assert metadata["scope"] == original_scope
    assert metadata["access_actor"] == "alice"
    assert metadata["unknowns"] == pack.metadata["unknowns"]
    assert set(metadata["evidence_refs"]) == retained_refs
    stored, stored_metadata = _stored_response(db_session, body["session_id"])
    assert stored["metadata"]["inspection_snapshot_id"] == _material_hash(pack)
    assert stored_metadata["inspection_snapshot_id"] == _material_hash(pack)
    session = db_session.get(AISession, body["session_id"])
    assert session is not None


def test_nested_sensitive_keys_are_redacted_before_prompt_response_and_storage(db_session, portfolio, monkeypatch):
    pack = _context(portfolio.id)
    secrets = {
        "api_key": "plain-secret-without-sk-prefix",
        "password": "plain-password",
        "authorization": "basic-credentials",
    }
    secret_values = tuple(secrets.values())
    pack.untrusted_content[0]["facts"]["data"]["provider_details"] = {
        "nested": [{"credentials": secrets}], "note": "保留可核实的非敏感说明",
    }
    pack.metadata["inspection_snapshot_id"] = _material_hash(pack)
    captured, llm, _ = _offline_model(monkeypatch, {"answer": "placeholder"})
    profile = llm.return_value.profile_used

    def echo_actual_prompt(_db, messages, **kwargs):
        # A provider echo makes any prompt leak observable in every subsequent
        # response/storage channel, without giving the provider secrets itself.
        return LLMCallResult(
            success=True, profile_used=profile,
            raw_response=json.dumps({"answer": messages[0]["content"]}, ensure_ascii=False),
            latency_ms=1, prompt_tokens=10, completion_tokens=10, total_tokens=20,
        )

    llm.side_effect = echo_actual_prompt
    body = _create(db_session, portfolio, pack)
    llm.assert_called_once()
    assert captured and captured[0]["untrusted_content"]
    prompt = llm.call_args.args[1]
    session = db_session.get(AISession, body["session_id"])
    messages = db_session.scalars(select(AIMessage).where(AIMessage.session_id == body["session_id"])).all()
    persisted = {
        "session_context": session.context_summary,
        "messages": [{"content": message.content, "context_summary": message.context_summary,
                      "metadata_json": message.metadata_json} for message in messages],
    }
    channels = {
        "pack": asdict(pack), "prompt_pack": captured[0], "prompt": prompt,
        "response": body, "storage": persisted,
    }
    for channel, material in channels.items():
        text = json.dumps(material, ensure_ascii=False, default=str)
        for value in secret_values:
            assert value not in text, f"Unredacted sensitive value reached {channel}"
    assert "保留可核实的非敏感说明" in json.dumps(prompt, ensure_ascii=False)
    assert pack.metadata["inspection_snapshot_id"] == _material_hash(pack)
