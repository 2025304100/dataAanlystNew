"""Final review binds all prerequisite proofs, scope, actor and replay key."""
import json
import pytest
from sqlalchemy import func, select
from app.models.async_task import AsyncTaskRecord
from app.models.sim_account import CashLedger
from app.services.portfolio_inspection_runtime import get_portfolio_runtime_inspection
from app.services.portfolio_inspection_guide import confirm_inspection_guide
from app.services.portfolio_reconciliation_coverage import CoverageError
from app.services.portfolio_state_machine import _set_status, _get_status
from app.services.data_governance_audit import DataGovernanceAuditEvent
from tests.test_portfolio_inspection_reconciliation_services import account
from tests.test_portfolio_inspection_workflow_http import mounted

pytestmark=pytest.mark.whitebox
NOTE="本人核查当前账户范围与三个前置步骤材料后批准恢复"


def pause(db,p):
    _set_status(db,p,"ADMIN_PAUSED");db.commit()
    return get_portfolio_runtime_inspection(db,p.id)


def confirm(db,p,runtime,**kwargs):
    return confirm_inspection_guide(db,p.id,expected_snapshot_id=runtime["guide"]["snapshot_id"],
        expected_action=runtime["recovery_summary"]["confirmation_action"],idempotency_key=kwargs.get("key","guide-review-unit-1"),
        review_note=kwargs.get("note",NOTE),actor=kwargs.get("actor","reviewer"))


def test_guide_reads_reproducible_proofs_and_explicit_nonapplicability(account):
    db,p=account;runtime=pause(db,p)
    assert runtime["guide"]["steps"][0]["status"]=="NOT_APPLICABLE"
    assert runtime["guide"]["steps"][1]["status"]=="NOT_APPLICABLE"
    assert "无授权新买入标的" in runtime["guide"]["steps"][1]["reason"]
    assert runtime["guide"]["steps"][2]["status"]=="NOT_APPLICABLE"
    assert runtime["guide"]["can_confirm"]
    assert get_portfolio_runtime_inspection(db,p.id)["guide"]["snapshot_id"]==runtime["guide"]["snapshot_id"]
    assert len(runtime["guide"]["completion_proofs"])==3


def test_confirm_current_three_proofs_is_atomic_audited_and_idempotent(account):
    db,p=account;runtime=pause(db,p)
    result=confirm(db,p,runtime);db.commit()
    assert result["current_state"]=="READY" and result["trading_resumed"]
    event=db.get(DataGovernanceAuditEvent,result["audit_event_id"])
    before=json.loads(event.before_json);attrs=json.loads(event.attributes_json)
    assert before["snapshot_id"]==runtime["guide"]["snapshot_id"]
    assert len(attrs["completion_proofs"])==3 and attrs["bypass"] is False
    repeated=confirm(db,p,runtime)
    assert repeated["replayed"] and repeated["audit_event_id"]==result["audit_event_id"]
    with pytest.raises(CoverageError,match="不同材料或理由"):
        confirm(db,p,runtime,note="本人修改了审核理由不得覆盖原确认")


def test_old_snapshot_rejected_on_new_source_without_clearing_pause_or_note(account):
    db,p=account;runtime=pause(db,p)
    db.add(CashLedger(portfolio_id=p.id,entry_type="deposit",amount=10,balance_after=100010));db.commit()
    with pytest.raises(CoverageError,match="已变化"):
        confirm(db,p,runtime)
    assert _get_status(db,p)=="ADMIN_PAUSED"
    assert not list(db.scalars(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.business_key=="inspection-guide-review")))


def test_active_repair_step_remains_running_and_cannot_be_reviewed(account):
    db,p=account;runtime=pause(db,p)
    db.add(AsyncTaskRecord(id="active-repair",task_type="portfolio_data_repair",status="running",stage="sync",
        payload_json=json.dumps({"portfolio_id":p.id})));db.commit()
    current=get_portfolio_runtime_inspection(db,p.id)
    assert current["guide"]["steps"][1]["status"]=="RUNNING"
    assert current["guide"]["can_confirm"] is False and current["recovery_summary"]["can_confirm"] is False
    with pytest.raises(CoverageError,match="前置步骤仍未通过"):
        confirm(db,p,current)
    assert _get_status(db,p)=="ADMIN_PAUSED"


def test_http_final_review_ack_scope_conflict_and_replay(mounted):
    client,db,p=mounted;runtime=pause(db,p)
    root=f"/api/v1/portfolios/{p.id}/inspection/guide/review"
    payload={"expected_snapshot_id":runtime["guide"]["snapshot_id"],"expected_action":runtime["recovery_summary"]["confirmation_action"],
        "idempotency_key":"guide-http-unit-1","review_note":NOTE,"acknowledge_current_proofs":False}
    assert client.post(root,json=payload).status_code==422
    payload["acknowledge_current_proofs"]=True
    response=client.post(root,json=payload);assert response.status_code==200,response.text
    assert client.post(root,json=payload).json()["replayed"]


def test_http_stale_guide_returns_structured_conflict(mounted):
    client,db,p=mounted;runtime=pause(db,p)
    payload={"expected_snapshot_id":runtime["guide"]["snapshot_id"],"expected_action":runtime["recovery_summary"]["confirmation_action"],
        "idempotency_key":"guide-http-unit-2","review_note":NOTE,"acknowledge_current_proofs":True}
    db.add(CashLedger(portfolio_id=p.id,entry_type="deposit",amount=10,balance_after=100010));db.commit()
    result=client.post(f"/api/v1/portfolios/{p.id}/inspection/guide/review",json=payload)
    assert result.status_code==409 and result.json()["error_code"]=="GUIDE_EVIDENCE_CHANGED"
    assert _get_status(db,p)=="ADMIN_PAUSED"


@pytest.mark.parametrize("dispatch_failed",[False,True])
def test_manual_recheck_dispatches_only_evidence_task_and_retains_demand_on_failure(mounted,monkeypatch,dispatch_failed):
    from types import SimpleNamespace
    import app.services.portfolio_reconciliation_job as job
    client,db,p=mounted
    calls=[]
    def dispatch(**kw):
        calls.append(kw)
        if dispatch_failed:raise RuntimeError("isolated dispatch failure")
        return SimpleNamespace(id="actual-check-task")
    monkeypatch.setattr(job,"create_portfolio_reconciliation_task",dispatch)
    result=client.post(f"/api/v1/portfolios/{p.id}/inspection/reconciliation-job/recheck",json={})
    assert result.status_code==202,result.text
    body=result.json()
    assert body["dispatch_status"]==("WAITING_RETRY" if dispatch_failed else "QUEUED")
    assert body["reconciliation_passed"] is False and body["trading_resumed"] is False
    assert calls==[{"portfolio_id":p.id}]
