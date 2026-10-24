"""A retained plan key can repair attribution, never manufacture execution."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import func, select, update
from app.models.decision_engine import DecisionEvidence, DecisionOrderPlanRecord
from app.models.portfolio import Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.portfolio_inspection_workflow import InspectionEvidenceRequest, InspectionEvidenceDecision
from app.services import portfolio_inspection_supplement as service
from app.services.portfolio_reconciliation import _load_auto_simulation_execution, reconcile_trade_date
from tests.test_portfolio_inspection_reconciliation_services import account, seed_run, DAY
from tests.test_portfolio_inspection_workflow_http import mounted

pytestmark = pytest.mark.whitebox
NOTE = "本人核查原订单计划键与执行流水，申请修复关联"
REVIEW = "本人比对原订单与原决策和实际成交，确认本次关联"


def seed(db, p):
    run = seed_run(db, p)
    ev = db.get(DecisionEvidence, "inspection-evidence")
    ev.action, ev.target_quantity, ev.target_qty_delta = "BUY", 100, 100
    db.add(DecisionOrderPlanRecord(order_plan_id="retained-plan", decision_run_id=run.id, evidence_id=ev.id,
        portfolio_id=p.id, symbol_id=1, action="BUY", signal_date=DAY, execution_date=DAY+timedelta(days=1), target_quantity=100))
    db.add(SimOrder(id=1, portfolio_id=p.id, symbol_id=1, side="buy", quantity=100, submitted_price=10,
        status="filled", filled_quantity=100, filled_amount=1000, fee=1, filled_price=10,
        execution_mode="auto", client_order_key="retained-plan", created_at=datetime(2026,8,21,1,30), filled_at=datetime(2026,8,21,1,30)))
    db.add(SimTrade(id=1, portfolio_id=p.id, symbol_id=1, order_id=1, side="buy", quantity=100, amount=1000,
        price=10, fee=1, created_at=datetime(2026,8,21,1,30)))
    db.add(CashLedger(portfolio_id=p.id, entry_type="buy", amount=-1001, balance_after=98999,
        ref_type="sim_order", ref_id=1, created_at=datetime(2026,8,21,1,30)))
    db.add(Position(portfolio_id=p.id, symbol_id=1, quantity=100, avg_cost=10.01, asset_type="stock"))
    db.commit()
    return run


def candidate(db, pid):
    page = service.list_evidence_supplements(db, pid, DAY)
    assert page["candidate_count"] == 1
    return page["candidates"][0]


def request(db, pid, item=None, key="request-unit-1"):
    item = item or candidate(db, pid)
    return service.submit_evidence_supplement(db, pid, trade_date=DAY, candidate_id=item["candidate_id"],
        expected_source_hash=item["source_hash"], request_key=key, note=NOTE, actor="reviewer")


def review(db, pid, request_id, source_hash, **kw):
    return service.review_evidence_supplement(db, pid, request_id, decision=kw.get("decision","approve"),
        expected_source_hash=source_hash, decision_key=kw.get("key","review-unit-1"), review_note=REVIEW, actor="reviewer")


def test_approved_real_source_fixes_attribution_and_recheck_without_rewriting_order_or_creating_trades(account):
    db,p = account
    run = seed(db,p)
    item = candidate(db,p.id)
    assert item["name"] == "Test" and item["fill_count"] == item["ledger_count"] == 1
    assert _load_auto_simulation_execution(db,portfolio_id=p.id,decision_run_id=run.id) == (set(), [])
    req = request(db,p.id,item)
    assert _load_auto_simulation_execution(db,portfolio_id=p.id,decision_run_id=run.id) == (set(), [])
    decision = review(db,p.id,req["request_id"],item["source_hash"])
    db.commit()
    assert decision["recheck_required"] and decision["trading_resumed"] is False
    ids,fills = _load_auto_simulation_execution(db,portfolio_id=p.id,decision_run_id=run.id)
    assert ids == {"inspection-evidence"} and len(fills) == 1 and fills[0].quantity == 100
    report = reconcile_trade_date(db,p.id,DAY,persist=False)
    assert report.status == "PASSED", report.differences
    assert db.get(SimOrder,1).decision_evidence_id is None
    assert db.scalar(select(func.count()).select_from(SimTrade)) == 1
    assert service.list_evidence_supplements(db,p.id,DAY)["requests"][0]["usable"]


@pytest.mark.parametrize("field,value", [("execution_mode","manual"),("client_order_key","different-plan"),
    ("side","sell"),("quantity",200),("filled_at",datetime(2026,8,22,1,30))])
def test_no_symbol_date_or_manual_activity_heuristic_is_an_automatic_candidate(account, field, value):
    db,p = account
    seed(db,p)
    setattr(db.get(SimOrder,1),field,value)
    db.commit()
    assert service.list_evidence_supplements(db,p.id,DAY)["candidate_count"] == 0


def test_other_portfolio_or_day_has_no_candidate_and_cannot_read_request(account):
    db,p = account
    seed(db,p)
    item = candidate(db,p.id)
    assert not service.list_evidence_supplements(db,p.id,DAY+timedelta(days=1))["candidates"]
    assert not service.list_evidence_supplements(db,999,DAY)["candidates"]
    req = request(db,p.id,item)
    with pytest.raises(service.CoverageError,match="不属于当前组合"):
        review(db,999,req["request_id"],item["source_hash"])


def test_source_change_rejects_approval_and_old_approval_stops_being_usable(account):
    db,p=account;run=seed(db,p);item=candidate(db,p.id);req=request(db,p.id,item)
    db.get(SimTrade,1).fee=2
    db.commit()
    with pytest.raises(service.CoverageError,match="来源在申请后已变化"):
        review(db,p.id,req["request_id"],item["source_hash"])
    assert db.scalar(select(func.count()).select_from(InspectionEvidenceDecision)) == 0
    item=candidate(db,p.id);req=request(db,p.id,item,key="request-unit-2")
    review(db,p.id,req["request_id"],item["source_hash"]);db.commit()
    assert service.approved_order_associations(db,p.id,run.id)
    db.get(SimOrder,1).filled_quantity=99;db.commit()
    assert service.approved_order_associations(db,p.id,run.id) == {}
    assert all(row["status"]=="NEEDS_RECHECK" for row in service.list_evidence_supplements(db,p.id,DAY)["requests"])
    assert db.scalar(select(func.count()).select_from(InspectionEvidenceDecision)) == 1


def test_request_review_replay_and_key_conflict_do_not_duplicate_or_erase_audit(account):
    db,p=account;seed(db,p);item=candidate(db,p.id);req=request(db,p.id,item)
    assert request(db,p.id,item)["replayed"]
    decision=review(db,p.id,req["request_id"],item["source_hash"])
    assert review(db,p.id,req["request_id"],item["source_hash"])["replayed"]
    with pytest.raises(service.CoverageError,match="已有审核结论"):
        review(db,p.id,req["request_id"],item["source_hash"],decision="reject")
    with pytest.raises(service.CoverageError,match="不同内容"):
        request(db,p.id,{**item,"source_hash":"b"*64})
    assert db.scalar(select(func.count()).select_from(InspectionEvidenceDecision)) == 1
    assert db.scalar(select(func.count()).select_from(InspectionEvidenceRequest)) == 1


def test_rejection_records_decision_and_keeps_missing_link(account):
    db,p=account;run=seed(db,p);item=candidate(db,p.id);req=request(db,p.id,item)
    result=review(db,p.id,req["request_id"],item["source_hash"],decision="reject");db.commit()
    assert result["recheck_required"] is False and service.approved_order_associations(db,p.id,run.id) == {}
    assert service.list_evidence_supplements(db,p.id,DAY)["requests"][0]["status"] == "REJECTED"


def test_scope_change_and_append_only_record_guards(account,monkeypatch):
    db,p=account;seed(db,p);item=candidate(db,p.id);req=request(db,p.id,item)
    monkeypatch.setattr(service,"_scope",lambda *a,**k:{"baseline_id":"new-generation-baseline","generation":2})
    with pytest.raises(service.CoverageError,match="覆盖代次已变化"):
        review(db,p.id,req["request_id"],item["source_hash"])
    saved=db.get(InspectionEvidenceRequest,req["request_id"]);saved.note="attempt overwrite"
    with pytest.raises(ValueError,match="不可改写"):
        db.flush()
    db.rollback()


def test_cached_fill_cannot_hide_core_source_change_during_review(account):
    db,p=account;seed(db,p);item=candidate(db,p.id);req=request(db,p.id,item)
    old_fill=db.get(SimTrade,1)
    db.execute(update(SimTrade).where(SimTrade.id==1).values(fee=2).execution_options(synchronize_session=False));db.commit()
    assert old_fill.fee==1  # Deliberately retain an obsolete identity-map object.
    with pytest.raises(service.CoverageError,match="来源在申请后已变化"):
        review(db,p.id,req["request_id"],item["source_hash"])
    assert db.scalar(select(func.count()).select_from(InspectionEvidenceDecision)) == 0


def test_http_candidates_batch_review_acknowledgement_and_no_trade_side_effects(mounted):
    client,db,p=mounted;seed(db,p)
    root=f"/api/v1/portfolios/{p.id}/inspection/evidence-supplements"
    page=client.get(root,params={"trade_date":DAY.isoformat()}).json();item=page["candidates"][0]
    payload={"trade_date":DAY.isoformat(),"items":[{"candidate_id":item["candidate_id"],"expected_source_hash":item["source_hash"]}],"request_key":"batch-http-1","note":NOTE}
    submitted=client.post(root+"/batch",json=payload);assert submitted.status_code==201,submitted.text
    request_id=submitted.json()["items"][0]["request_id"]
    body={"decision":"approve","expected_source_hash":item["source_hash"],"decision_key":"decision-http-1","review_note":REVIEW,"acknowledge_scope":False}
    assert client.post(root+f"/{request_id}/review",json=body).status_code==422
    body["acknowledge_scope"]=True
    reviewed=client.post(root+f"/{request_id}/review",json=body);assert reviewed.status_code==200,reviewed.text
    assert reviewed.json()["trading_resumed"] is False and db.get(SimOrder,1).decision_evidence_id is None
    assert client.post(root+f"/{request_id}/review",json=body).json()["replayed"]


def test_http_batch_conflict_rolls_back_every_request(mounted):
    client,db,p=mounted;seed(db,p);item=candidate(db,p.id)
    root=f"/api/v1/portfolios/{p.id}/inspection/evidence-supplements"
    result=client.post(root+"/batch",json={"trade_date":DAY.isoformat(),"items":[
        {"candidate_id":item["candidate_id"],"expected_source_hash":item["source_hash"]},
        {"candidate_id":"missing-real-source","expected_source_hash":"a"*64}],"request_key":"batch-conflict-1","note":NOTE})
    assert result.status_code==409
    assert db.scalar(select(func.count()).select_from(InspectionEvidenceRequest)) == 0
