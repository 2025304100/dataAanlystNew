"""Controlled inspection analysis must use scoped facts and never run model tools."""
from unittest.mock import Mock

import pytest

from app.services.portfolio_inspection_assistant import build_inspection_context
from tests.test_portfolio_inspection_workflow_http import mounted  # noqa: F401

pytestmark = pytest.mark.whitebox


def seed_context(monkeypatch):
    monkeypatch.setattr("app.services.portfolio_inspection_diagnostics.build_portfolio_inspection", lambda *a, **k: {
        "snapshot_id": "server-only", "symbols": [{"symbol_id": 1, "symbol": "TEST", "market": "SH",
            "expected_trade_date": "2026-10-21", "scoring": {}, "evidence": [
                {"evidence_id": "bar:1", "label": "日线", "status": "MISSING", "reason": "缺失", "depends_on": []}]}]})
    monkeypatch.setattr("app.services.portfolio_inspection_runtime.get_portfolio_runtime_inspection", lambda *a: {
        "permissions": {"allow_new_buys": False}, "recovery_summary": {"blockers": ["证据未齐"]}})
    monkeypatch.setattr("app.services.portfolio_resume_service.get_reconciliation_queue", lambda *a: {"days": []})
    monkeypatch.setattr("app.services.portfolio_reconciliation_job.get_portfolio_reconciliation_jobs", lambda *a: {"jobs": [], "schedules": []})


def test_context_reads_server_evidence_and_keeps_unknowns_and_whitelisted_navigation(monkeypatch):
    seed_context(monkeypatch)
    pack = build_inspection_context(Mock(), 8, symbol_ids=[1], trade_date="2026-10-20", step="data")
    assert pack.references == {"portfolio_id": 8}
    assert pack.metadata["evidence_refs"] == ["bar:1"]
    assert len(pack.metadata["inspection_snapshot_id"]) == 64
    assert any("不完全一致" in item for item in pack.metadata["unknowns"])
    assert pack.capabilities["execute_tools"] is False
    assert pack.metadata["data_as_of"] is None
    assert pack.metadata["source_as_of"][0]["expected_trade_date"] == "2026-10-21"
    assert {a["action_type"] for a in pack.metadata["available_actions"]} >= {"PREVIEW_DATA_REPAIR", "VIEW_JOB"}
    with pytest.raises(ValueError, match="不属于"):
        build_inspection_context(Mock(), 8, symbol_ids=[999])


def test_failed_read_is_unknown_not_empty_success(monkeypatch):
    seed_context(monkeypatch)
    def fail(*_):
        raise RuntimeError("sensitive database detail")
    monkeypatch.setattr("app.services.portfolio_reconciliation_job.get_portfolio_reconciliation_jobs", fail)
    pack = build_inspection_context(Mock(), 8)
    assert pack.untrusted_content[0]["facts"]["jobs"] == {"status": "READ_ERROR"}
    assert any("jobs读取失败" in item for item in pack.metadata["unknowns"])
    assert "sensitive database" not in str(pack)


def test_analysis_authorizes_portfolio_and_rejects_client_supplied_snapshots(mounted, monkeypatch):
    client, db, portfolio = mounted
    root = f"/api/v1/portfolios/{portfolio.id}/inspection/analysis"
    assert client.post(root, json={"runtime": {"allowed": True}}).status_code == 422
    monkeypatch.setenv("STRICT_AUTH", "1")
    assert client.post(root, json={}).status_code == 401
    seed_context(monkeypatch)
    seen = []
    def fake_session(payload, session, *, controlled_pack):
        seen.append(controlled_pack)
        return {"session": {"id": 81}, "response": {"answer": "formal tools only", "metadata": {}, "warnings": []}}
    monkeypatch.setattr("app.api.routes.ai_sessions.create_session_with_context", fake_session)
    result = client.post(root + "?X-User=actual-reviewer", json={"symbol_ids": [1], "step": "data"})
    assert result.status_code == 200
    assert seen[0].metadata["scope"]["portfolio_id"] == portfolio.id
    assert result.json()["response"]["metadata"]["inspection_audit_ref"].startswith("audit:")
    from sqlalchemy import select
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    row = db.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key == "inspection-ai:81"))
    assert row.operator_id == "actual-reviewer"
    assert '"ai_assisted":true' in row.attributes_json.replace(" ", "")
