"""Inspection sessions keep server-owned actor/portfolio scope on generic APIs."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.routes import ai_sessions, portfolio_governance as governance
from app.db.session import get_db
from app.models.ai_session import AISession
from app.models.portfolio import Portfolio
from app.schemas.ai_session import AiSessionCreateRequest
from app.services import ai_session_service
from app.services.ai.llm_client import LLMCallResult
from tests.test_inspection_ai_controlled_metadata import _context, _offline_model

pytestmark = pytest.mark.whitebox


@pytest.fixture
def scoped(db_session, monkeypatch):
    portfolio = Portfolio(name="private inspection", account_type="simulated", total_capital=1000,
        investable_ratio=.95, cash_reserve_ratio=.05)
    db_session.add(portfolio); db_session.commit()
    pack = _context(portfolio.id)
    _offline_model(monkeypatch, {"answer": "PRIVATE_INSPECTION_RESULT"})
    created = ai_sessions.create_session_with_context(AiSessionCreateRequest(message="inspect"), db_session, controlled_pack=pack)
    ordinary = ai_session_service.create_session(db_session, title="PUBLIC_ORDINARY", source_page="task")
    app = FastAPI(); app.include_router(ai_sessions.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    monkeypatch.setenv("STRICT_AUTH", "1")
    with TestClient(app) as client:
        yield client, db_session, portfolio, created["session_id"], ordinary.id


def test_scope_is_persisted_at_creation_and_message_updates_do_not_replace_it(scoped):
    _client, db, portfolio, sid, _ordinary = scoped
    session = db.get(AISession, sid)
    marker = json.loads(session.context_summary)["inspection_access"]
    assert session.source_page == "portfolio-inspection"
    assert marker["actor"] == "alice" and marker["portfolio_id"] == portfolio.id
    original = session.context_summary
    assert "PRIVATE_INSPECTION_RESULT" not in original and "facts" not in original
    ai_session_service.add_message(db, session_id=sid, role="user", content="another message", context_summary='{"inspection_access":"forged-message-only"}')
    db.refresh(session)
    assert session.context_summary == original


@pytest.mark.parametrize("suffix", ["", "/messages", "/audit"])
def test_generic_reads_require_original_actor_and_strict_auth(scoped, suffix):
    client, _db, _portfolio, sid, _ordinary = scoped
    url = f"/api/v1/ai/sessions/{sid}{suffix}"
    assert client.get(url).status_code == 401
    other = client.get(url, params={"X-User": "bob"})
    assert other.status_code == 403 and "PRIVATE_INSPECTION_RESULT" not in other.text
    own = client.get(url, params={"X-User": "alice"})
    assert own.status_code == 200


def test_generic_list_omits_private_sessions_but_keeps_ordinary_behavior(scoped):
    client, _db, _portfolio, sid, ordinary = scoped
    for params in ({}, {"X-User": "bob"}):
        result = client.get("/api/v1/ai/sessions", params=params)
        assert result.status_code == 200
        ids = {item["id"] for item in result.json()["items"]}
        assert ordinary in ids and sid not in ids
        assert "PRIVATE_INSPECTION_RESULT" not in result.text
    own = client.get("/api/v1/ai/sessions", params={"X-User": "alice"})
    assert sid in {item["id"] for item in own.json()["items"]}
    assert client.get(f"/api/v1/ai/sessions/{ordinary}").status_code == 200


def test_delete_requires_actor_and_does_not_erase_another_inspection(scoped):
    client, db, _portfolio, sid, _ordinary = scoped
    url = f"/api/v1/ai/sessions/{sid}"
    assert client.delete(url).status_code == 401
    assert client.delete(url, params={"X-User": "bob"}).status_code == 403
    assert db.get(AISession, sid) is not None
    assert client.delete(url, params={"X-User": "alice"}).status_code == 200
    assert client.get(url, params={"X-User": "alice"}).status_code == 404


def test_legacy_inspection_without_reliable_marker_is_closed_to_everyone(scoped):
    client, db, _portfolio, _sid, _ordinary = scoped
    legacy = ai_session_service.create_session(db, title="LEGACY_PRIVATE", source_page="portfolio-inspection",
        context_summary='{"references":{"portfolio_id":999}}')
    for suffix in ("", "/messages", "/audit"):
        assert client.get(f"/api/v1/ai/sessions/{legacy.id}{suffix}", params={"X-User": "alice"}).status_code == 403
    listed = client.get("/api/v1/ai/sessions", params={"X-User": "alice"})
    assert "LEGACY_PRIVATE" not in listed.text
    assert client.delete(f"/api/v1/ai/sessions/{legacy.id}", params={"X-User": "alice"}).status_code == 403


def test_portfolio_permission_is_rechecked_on_reads_delete_and_continuation(scoped, monkeypatch):
    client, db, portfolio, sid, _ordinary = scoped
    seen = []
    def denied(session, portfolio_id, actor):
        seen.append((portfolio_id, actor))
        raise HTTPException(403, detail="portfolio access revoked")
    monkeypatch.setattr(governance, "_ensure_portfolio_access", denied)
    for suffix in ("", "/messages", "/audit"):
        assert client.get(f"/api/v1/ai/sessions/{sid}{suffix}", params={"X-User": "alice"}).status_code == 403
    assert client.delete(f"/api/v1/ai/sessions/{sid}", params={"X-User": "alice"}).status_code == 403
    result = client.post("/api/v1/ai/sessions/stream", params={"X-User": "alice"}, json={"session_id": sid, "message": "continue"})
    assert result.status_code == 403
    assert set(seen) == {(portfolio.id, "alice")} and db.get(AISession, sid) is not None


def test_stream_continuation_rejects_other_actor_before_reading_or_calling_model(scoped, monkeypatch):
    client, _db, _portfolio, sid, _ordinary = scoped
    model = Mock()
    monkeypatch.setattr(ai_sessions, "stream_llm_completion", model)
    for params, expected in (({}, 401), ({"X-User": "bob"}, 403)):
        result = client.post("/api/v1/ai/sessions/stream", params=params, json={"session_id": sid, "message": "continue"})
        assert result.status_code == expected
        assert "PRIVATE_INSPECTION_RESULT" not in result.text
    model.assert_not_called()


def test_owned_continuation_ignores_client_scope_and_rebuilds_current_server_facts(scoped, monkeypatch):
    client, db, portfolio, sid, _ordinary = scoped
    seen = []
    def build(session, pid, **kwargs):
        seen.append((pid, kwargs))
        return _context(pid)
    monkeypatch.setattr("app.services.portfolio_inspection_assistant.build_inspection_context", build)
    profile = ai_sessions.ai_profile_service.get_profile(db, 73)
    def stream(*args, **kwargs):
        yield {"type": "done", "result": LLMCallResult(success=True, profile_used=profile,
            raw_response=json.dumps({"answer": "current own facts", "metadata": {"scope": {"portfolio_id": 999},
                "access_actor": "mallory"}, "draft": {"draft_type": "draft_order"}}))}
    monkeypatch.setattr(ai_sessions, "stream_llm_completion", stream)
    result = client.post("/api/v1/ai/sessions/stream", params={"X-User": "alice"}, json={"session_id": sid,
        "source_page": "task", "context": {"portfolio_id": 999, "access_actor": "mallory"}, "message": "continue"})
    assert result.status_code == 200
    events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    response = next(item["response"] for item in events if "response" in item)
    assert seen[0][0] == portfolio.id and seen[0][1]["symbol_ids"] == [11, 12]
    assert response["metadata"]["scope"]["portfolio_id"] == portfolio.id
    assert response["metadata"]["access_actor"] == "alice" and response["draft"] is None
    assert json.loads(db.get(AISession, sid).context_summary)["inspection_access"]["actor"] == "alice"


@pytest.mark.parametrize("url", ["/api/v1/ai/sessions", "/api/v1/ai/sessions/stream"])
def test_client_cannot_create_an_inspection_scope_marker_by_body(scoped, url):
    client, _db, _portfolio, _sid, _ordinary = scoped
    result = client.post(url, params={"X-User": "bob"}, json={"source_page": "portfolio-inspection", "message": "forged",
        "context": {"inspection_access": {"actor": "alice", "portfolio_id": 999}}})
    assert result.status_code == 403
