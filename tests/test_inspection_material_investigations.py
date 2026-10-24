"""Material investigation is traceable, not a substitute for missing execution."""
import copy
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.portfolio_inspection_workflow import InspectionEvidenceRequest
from app.models.sim_account import SimOrder, SimTrade
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.portfolio_state_machine import _get_status, _set_status
from tests.test_portfolio_inspection_reconciliation_services import DAY
from tests.test_portfolio_inspection_workflow_http import mounted

pytestmark = pytest.mark.whitebox


def draft(client, pid, material="execution", conclusion="NOT_FOUND"):
    report = client.get(f"/api/v1/portfolios/{pid}/reconciliation-preview", params={"trade_date": str(DAY)}).json()
    return {"trade_date": str(DAY), "material": material, "report_ref": report["report_id"],
        "report_version": report["report_version"], "conclusion": conclusion,
        "attempted_sources": ["本日来源及已保留原始备份"], "evidence_refs": [],
        "note": "已核查原任务和备份，仍缺少本交易日可验证的原始材料", "request_key": "material-note-test-0001"}


@pytest.mark.parametrize("material", ["execution", "schedule", "positions"])
def test_scoped_material_note_is_readable_and_in_related_history_without_releasing_any_gate(mounted, material):
    client, db, p = mounted
    _set_status(db, p, "RECONCILIATION_BLOCKED")
    p.status_reconciliation = "RECONCILIATION_BLOCKED"
    db.commit()
    endpoint = f"/api/v1/portfolios/{p.id}/inspection/material-investigations"
    payload = draft(client, p.id, material, "UNRECOVERABLE")
    before = copy.deepcopy({column.name: getattr(p, column.name) for column in p.__table__.columns})
    count = lambda model: db.scalar(select(func.count()).select_from(model))
    audit_before = count(DataGovernanceAuditEvent)
    protected = [count(model) for model in (SimOrder, SimTrade, InspectionEvidenceRequest)]
    query = {"trade_date": str(DAY), "material": material}
    assert client.get(endpoint, params=query).json()["items"] == []
    assert count(DataGovernanceAuditEvent) == audit_before
    result = client.post(endpoint, json=payload)
    assert result.status_code == 201, result.text
    item = result.json()["item"]
    assert not any(item[key] for key in ("proves_execution", "reconciliation_passed", "trading_resumed", "baseline_accepted"))
    assert client.get(endpoint, params=query).json()["items"][0] == item
    assert client.get(endpoint, params={**query, "material": "schedule" if material != "schedule" else "positions"}).json()["items"] == []
    assert client.get(endpoint, params={**query, "trade_date": "2026-08-19"}).json()["items"] == []
    history = client.get(f"/api/v1/portfolios/{p.id}/inspection/history", params={
        "report_ref": payload["report_ref"], "report_version": payload["report_version"],
        "trade_date_from": str(DAY), "trade_date_to": str(DAY),
        "occurred_from": "2020-01-01T00:00:00Z", "occurred_to": "2030-01-01T00:00:00Z"}).json()
    assert history["relation_status"] == "matched", history
    assert history["items"][0]["execution_result"] != "success"
    assert "核查" in history["items"][0]["title"]
    assert [count(model) for model in (SimOrder, SimTrade, InspectionEvidenceRequest)] == protected
    db.refresh(p)
    assert {column.name: getattr(p, column.name) for column in p.__table__.columns} == before
    assert _get_status(db, p) == "RECONCILIATION_BLOCKED"
    assert count(DataGovernanceAuditEvent) == audit_before + 1


def test_note_replay_preserves_original_and_rejects_changed_content(mounted):
    client, db, p = mounted
    endpoint = f"/api/v1/portfolios/{p.id}/inspection/material-investigations"
    payload = draft(client, p.id)
    first = client.post(endpoint, json=payload).json()
    second = client.post(endpoint, json=payload)
    assert second.status_code == 201
    assert second.json()["replayed"] and second.json()["item"]["id"] == first["item"]["id"]
    conflict = client.post(endpoint, json={**payload, "note": "这是一条与原先核查记录内容不同的说明"})
    assert conflict.status_code == 409
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 1


def test_changed_report_and_unsupported_policy_fields_cannot_be_saved(mounted):
    client, db, p = mounted
    endpoint = f"/api/v1/portfolios/{p.id}/inspection/material-investigations"
    payload = draft(client, p.id)
    assert client.post(endpoint, json={**payload, "report_version": "f" * 64}).status_code == 409
    assert client.post(endpoint, json={**payload, "conclusion": "FOUND_PENDING_VALIDATION"}).status_code == 422
    assert client.post(endpoint, json={**payload, "accept_baseline": True}).status_code == 422
    assert client.post(endpoint, json={**payload, "attempted_sources": ["  "]}).status_code == 422
    assert db.scalar(select(func.count()).select_from(DataGovernanceAuditEvent)) == 0


def test_found_material_is_only_a_pending_reference_and_reads_enforce_access(mounted):
    client, db, p = mounted
    endpoint = f"/api/v1/portfolios/{p.id}/inspection/material-investigations"
    payload = {**draft(client, p.id, "positions", "FOUND_PENDING_VALIDATION"), "evidence_refs": ["原始只读备份中的当日持仓快照"]}
    response = client.post(endpoint, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["item"]["requires_source_validation"] is True
    report = client.get(f"/api/v1/portfolios/{p.id}/reconciliation-preview", params={"trade_date": str(DAY)}).json()
    assert not report["zero_sum_check_passed"] and report["report_version"] == payload["report_version"]
    with patch("app.api.routes.portfolio_inspection_workflow._ensure_portfolio_access", side_effect=HTTPException(403, "无权访问")):
        assert client.get(endpoint, params={"trade_date": str(DAY), "material": "positions"}).status_code == 403
        assert client.post(endpoint, json=payload).status_code == 403
