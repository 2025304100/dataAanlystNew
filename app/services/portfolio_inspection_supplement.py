"""Reviewed attribution of retained executions, never synthetic fills or runs.

The only enabled adapter links a retained automatic order's immutable plan key
to its exact retained decision. Manual orders, date/symbol heuristics and user
notes cannot prove automatic execution. Original rows are never rewritten.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import inspect, select
from app.models.portfolio import Portfolio
from app.models.sim_account import SimOrder, SimTrade, CashLedger
from app.models.decision_engine import DecisionRun, DecisionEvidence, DecisionOrderPlanRecord
from app.models.portfolio_inspection_workflow import InspectionEvidenceRequest, InspectionEvidenceDecision
from app.services.portfolio_reconciliation_coverage import CoverageError, _hash, _canonical


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _record(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _scope(db, portfolio_id, *, current_read=False):
    from app.services.portfolio_reconciliation_coverage import get_active_coverage
    active = get_active_coverage(db, portfolio_id, current_read=current_read)
    return {"baseline_id": active["baseline_id"] if active else None,
            "generation": active["generation"] if active else None}


def _supported(db):
    names = set(inspect(db.connection()).get_table_names())
    required = {"inspection_evidence_requests", "inspection_evidence_decisions", "decision_runs",
        "decision_evidence", "decision_order_plans", "sim_orders", "sim_trades", "cash_ledger"}
    if not required <= names:
        raise CoverageError("EVIDENCE_STORAGE_UNAVAILABLE", "补证来源或存储尚未部署，请保留原缺口", status_code=503)


def _candidate(db, portfolio_id, trade_date, order, plan, evidence, run, *, current_read=False):
    from app.services.portfolio_reconciliation_events import _business_date
    if (order.decision_evidence_id is not None or order.execution_mode != "auto"
        or run.status != "SUCCEEDED" or run.is_result_production_eligible != 1
        or run.execution_at is None or run.execution_at.replace(tzinfo=None) > _now()
        or evidence.trade_date != trade_date or evidence.portfolio_id != portfolio_id
        or plan.signal_date != trade_date or evidence.strategy_snapshot_id != run.strategy_snapshot_id
        or plan.portfolio_id != portfolio_id or plan.symbol_id != order.symbol_id
        or evidence.symbol_id != order.symbol_id or str(order.side).upper() != evidence.action
        or plan.action != evidence.action or plan.action not in {"BUY", "SELL"}
        or not (0 < float(order.quantity) <= abs(float(evidence.target_qty_delta or 0)) + 1e-6)
        or _business_date(order.created_at) not in {trade_date, plan.execution_date}
        or (order.filled_at is not None and _business_date(order.filled_at) != plan.execution_date)):
        return None
    fill_query = select(SimTrade).where(SimTrade.order_id == order.id).order_by(SimTrade.id).execution_options(populate_existing=True)
    ledger_query = select(CashLedger).where(CashLedger.ref_type == "sim_order", CashLedger.ref_id == order.id).order_by(CashLedger.id).execution_options(populate_existing=True)
    fills = list(db.scalars(fill_query.with_for_update() if current_read else fill_query))
    ledgers = list(db.scalars(ledger_query.with_for_update() if current_read else ledger_query))
    if any(row.portfolio_id != portfolio_id or row.symbol_id != order.symbol_id
           or str(row.side).lower() != str(order.side).lower() for row in fills):
        return None
    if any(row.portfolio_id != portfolio_id for row in ledgers):
        return None
    from app.models.symbol import Symbol
    symbol = db.get(Symbol, order.symbol_id, with_for_update=current_read, populate_existing=True)
    if symbol is None:
        return None
    snapshot = {"adapter": "retained_auto_order_plan_v1", "portfolio_id": portfolio_id, "trade_date": trade_date,
        "run": _record(run), "evidence": _record(evidence), "plan": _record(plan), "order": _record(order),
        "fills": [_record(row) for row in fills], "ledger": [_record(row) for row in ledgers]}
    return {"candidate_id": f"order:{order.id}:evidence:{evidence.id}", "source_kind": "auto_order_plan",
        "source_id": str(order.id), "evidence_id": evidence.id, "run_id": run.id,
        "symbol_id": order.symbol_id, "symbol": symbol.symbol, "name": symbol.name, "market": symbol.market,
        "trade_date": trade_date.isoformat(), "execution_date": plan.execution_date.isoformat(),
        "order_id": order.id, "order_status": order.status, "quantity": order.quantity,
        "filled_quantity": order.filled_quantity, "fill_count": len(fills), "ledger_count": len(ledgers),
        "source_hash": _hash(snapshot), "snapshot": snapshot,
        "match_reasons": ["同组合、同决策交易日和同证券", "订单原有计划键与确切决策计划一致",
            "实际自动执行模式、方向、数量与执行日期吻合"],
        "effect": "仅补充原订单与原决策的核对关联；不改原始来源，不生成成交，不代表对账通过"}


def _candidates(db, portfolio_id, trade_date, *, current_read=False):
    statement = (select(SimOrder, DecisionOrderPlanRecord, DecisionEvidence, DecisionRun)
        .join(DecisionOrderPlanRecord, SimOrder.client_order_key == DecisionOrderPlanRecord.order_plan_id)
        .join(DecisionEvidence, DecisionEvidence.id == DecisionOrderPlanRecord.evidence_id)
        .join(DecisionRun, DecisionRun.id == DecisionEvidence.decision_run_id)
        .where(SimOrder.portfolio_id == portfolio_id, DecisionRun.portfolio_id == portfolio_id,
            DecisionRun.trade_date == trade_date, DecisionRun.run_type == "auto_simulation",
            DecisionOrderPlanRecord.decision_run_id == DecisionRun.id,
            SimOrder.decision_evidence_id.is_(None)).order_by(SimOrder.id).execution_options(populate_existing=True))
    rows = db.execute(statement.with_for_update() if current_read else statement).all()
    return [item for order, plan, evidence, run in rows
        if (item := _candidate(db, portfolio_id, trade_date, order, plan, evidence, run, current_read=current_read)) is not None]


def list_evidence_supplements(db, portfolio_id: int, trade_date: date):
    with db.no_autoflush:
        _supported(db)
        scope = _scope(db, portfolio_id)
        candidates = _candidates(db, portfolio_id, trade_date)
        requests = list(db.scalars(select(InspectionEvidenceRequest).where(
            InspectionEvidenceRequest.portfolio_id == portfolio_id, InspectionEvidenceRequest.trade_date == trade_date
        ).order_by(InspectionEvidenceRequest.created_at.desc(), InspectionEvidenceRequest.id.desc())))
        decisions = {row.request_id: row for row in db.scalars(select(InspectionEvidenceDecision).where(
            InspectionEvidenceDecision.portfolio_id == portfolio_id).order_by(InspectionEvidenceDecision.reviewed_at))}
        current = {row["candidate_id"]: row for row in candidates}
        items = []
        for request in requests:
            candidate_id = json.loads(request.source_snapshot_json)["candidate_id"]
            latest = current.get(candidate_id)
            decision = decisions.get(request.id)
            stale = (latest is None or latest["source_hash"] != request.source_hash or
                (scope["baseline_id"], scope["generation"]) != (request.baseline_id, request.generation))
            items.append({"id": request.id, "candidate_id": candidate_id, "source_hash": request.source_hash,
                "order_id": int(request.source_id), "created_by": request.created_by, "created_at": request.created_at,
                "note": request.note, "decision": decision.decision if decision else None,
                "reviewed_by": decision.reviewed_by if decision else None, "review_note": decision.review_note if decision else None,
                "status": "NEEDS_RECHECK" if stale else "APPROVED" if decision and decision.decision == "approve" else
                    "REJECTED" if decision else "PENDING_REVIEW", "usable": bool(not stale and decision and decision.decision == "approve")})
        registered = {(row["candidate_id"], row["source_hash"]) for row in items if row["status"] in {"PENDING_REVIEW", "APPROVED"}}
        available = [row for row in candidates if (row["candidate_id"], row["source_hash"]) not in registered]
        return {"portfolio_id": portfolio_id, "trade_date": trade_date, **scope,
            "candidates": [{key: value for key, value in row.items() if key != "snapshot"} for row in available[:100]],
            "candidate_count": len(available), "matched_source_count": len(candidates), "has_more": len(available) > 100, "requests": items,
            "limitations": ["仅已留存且保留确切自动计划键的订单可关联；人工成交、备份导入和无来源记录需先核查独立材料",
                "关联成功后仍需重检资金、持仓与差异，最后本人审核；不会自动恢复交易"]}


def submit_evidence_supplement(db, portfolio_id: int, *, trade_date: date, candidate_id: str,
        expected_source_hash: str, request_key: str, note: str, actor: str, batch_id: str | None = None):
    _supported(db)
    if len(note.strip()) < 10:
        raise CoverageError("EVIDENCE_NOTE_REQUIRED", "请写明至少10个字的来源核查说明", status_code=422)
    with db.begin_nested():
        db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
        request_hash = _hash({"trade_date": trade_date, "candidate_id": candidate_id, "source_hash": expected_source_hash, "note": note.strip(), "actor": actor, "batch_id": batch_id})
        existing = db.scalar(select(InspectionEvidenceRequest).where(InspectionEvidenceRequest.portfolio_id == portfolio_id,
            InspectionEvidenceRequest.request_key == request_key).with_for_update().execution_options(populate_existing=True))
        if existing:
            if existing.request_hash != request_hash:
                raise CoverageError("EVIDENCE_KEY_REUSED", "该申请键已用于不同内容")
            return {"request_id": existing.id, "replayed": True, "status": "PENDING_REVIEW"}
        candidate = next((row for row in _candidates(db, portfolio_id, trade_date, current_read=True) if row["candidate_id"] == candidate_id), None)
        if candidate is None or candidate["source_hash"] != expected_source_hash:
            raise CoverageError("EVIDENCE_SOURCE_CHANGED", "实际来源、匹配关系或执行状态已变化，请刷新候选并重新核查")
        scope = _scope(db, portfolio_id, current_read=True)
        row = InspectionEvidenceRequest(id=f"supplement-{uuid4().hex}", portfolio_id=portfolio_id, trade_date=trade_date,
            **scope, request_key=request_key, request_hash=request_hash, source_kind=candidate["source_kind"],
            source_id=candidate["source_id"], source_hash=candidate["source_hash"],
            source_snapshot_json=_canonical({**candidate, "batch_id": batch_id}), note=note.strip(), created_by=actor)
        db.add(row)
        db.flush()
        _audit(db, row, actor, "EVIDENCE_SUPPLEMENT_REQUESTED", {"request_id": row.id, "review_decision": "pending", "execution_result": "not_executed"})
        return {"request_id": row.id, "replayed": False, "status": "PENDING_REVIEW"}


def review_evidence_supplement(db, portfolio_id: int, request_id: str, *, decision: str,
        expected_source_hash: str, decision_key: str, review_note: str, actor: str):
    _supported(db)
    if actor in {"system", "cron_worker", "reconciliation-source-consumer"} or not actor.strip():
        raise CoverageError("PERSONAL_REVIEW_REQUIRED", "补证关联必须由本人审核", status_code=403)
    if decision not in {"approve", "reject"} or len(review_note.strip()) < 10:
        raise CoverageError("EVIDENCE_REVIEW_REQUIRED", "请确认结论并填写至少10个字的审核理由", status_code=422)
    with db.begin_nested():
        db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
        request = db.scalar(select(InspectionEvidenceRequest).where(InspectionEvidenceRequest.id == request_id,
            InspectionEvidenceRequest.portfolio_id == portfolio_id).with_for_update())
        if request is None:
            raise CoverageError("EVIDENCE_REQUEST_NOT_FOUND", "补证申请不属于当前组合或不存在", status_code=404)
        saved = db.scalar(select(InspectionEvidenceDecision).where(InspectionEvidenceDecision.request_id == request.id).with_for_update().execution_options(populate_existing=True))
        if saved:
            if (saved.decision_key, saved.decision, saved.source_hash, saved.review_note, saved.reviewed_by) != (
                    decision_key, decision, expected_source_hash, review_note.strip(), actor):
                raise CoverageError("EVIDENCE_ALREADY_REVIEWED", "该申请已有审核结论；原记录保留，变化后请新增申请")
            return {"request_id": request.id, "decision_id": saved.id, "decision": saved.decision, "replayed": True}
        scope = _scope(db, portfolio_id, current_read=True)
        if decision == "approve" and (scope["baseline_id"], scope["generation"]) != (request.baseline_id, request.generation):
            raise CoverageError("EVIDENCE_SCOPE_CHANGED", "账户覆盖代次已变化，请在当前范围重新核查")
        frozen = json.loads(request.source_snapshot_json)
        current = next((row for row in _candidates(db, portfolio_id, request.trade_date, current_read=True) if row["candidate_id"] == frozen["candidate_id"]), None)
        if expected_source_hash != request.source_hash or (decision == "approve" and (current is None or current["source_hash"] != request.source_hash)):
            raise CoverageError("EVIDENCE_SOURCE_CHANGED", "来源在申请后已变化；原申请继续保留，请重新核查并申请")
        row = InspectionEvidenceDecision(id=f"evidence-review-{uuid4().hex}", request_id=request.id, portfolio_id=portfolio_id,
            decision=decision, decision_key=decision_key, request_hash=request.request_hash, source_hash=request.source_hash,
            review_note=review_note.strip(), reviewed_by=actor)
        db.add(row)
        db.flush()  # Approval and the durable source event share this transaction.
        audit = _audit(db, request, actor, "EVIDENCE_SUPPLEMENT_REVIEWED", {"decision_id": row.id,
            "review_decision": "approved" if decision == "approve" else "rejected", "review_note": review_note.strip(),
            "reconciliation_passed": False, "trading_resumed": False, "execution_result": "not_executed"})
        from app.services.portfolio_reconciliation_events import consume_source_events
        if decision == "approve":
            consume_source_events(db, portfolio_id=portfolio_id)
        return {"request_id": request.id, "decision_id": row.id, "decision": decision, "audit_event_id": audit.event_id,
            "replayed": False, "recheck_required": decision == "approve", "reconciliation_passed": False, "trading_resumed": False}


def _audit(db, request, actor, action, extra):
    from app.services.data_governance_audit import write_audit_event
    frozen = json.loads(request.source_snapshot_json)
    return write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=request.portfolio_id,
        business_key=request.id, operator_id=actor, correlation_id=request.id, occurred_at=_now(),
        after={"source_hash": request.source_hash, "source_snapshot": frozen, **extra},
        attributes={"confirmation_type": action, "trade_date": request.trade_date.isoformat(), "actor_type": "human",
            "objects": [{"symbol_id": frozen["symbol_id"], "trade_date": request.trade_date.isoformat()}],
            "decision_run_id": frozen["run_id"], "order_id": frozen["order_id"], "evidence_id": frozen["evidence_id"],
            "handling_batch_id": frozen.get("batch_id") or request.id,
            "source_record_ref": f"sim_orders:{frozen['order_id']}", "supplement_request_id": request.id,
            "baseline_id": request.baseline_id, "generation": request.generation, "source_hash": request.source_hash,
            "bypass": False, **extra}, note=request.note)


def approved_order_associations(db, portfolio_id, decision_run_id):
    """Revalidate every retained approval against its exact current source."""
    if not {"inspection_evidence_requests", "inspection_evidence_decisions"} <= set(inspect(db.connection()).get_table_names()):
        return {}  # Unmigrated legacy readers cannot use supplemental evidence.
    current_read = bool(db.info.get("portfolio_reconciliation_current_read"))
    run = db.get(DecisionRun, decision_run_id, with_for_update=current_read, populate_existing=True)
    if run is None or run.portfolio_id != portfolio_id:
        return {}
    statement = select(InspectionEvidenceRequest, InspectionEvidenceDecision).join(
        InspectionEvidenceDecision, InspectionEvidenceDecision.request_id == InspectionEvidenceRequest.id).where(
        InspectionEvidenceRequest.portfolio_id == portfolio_id, InspectionEvidenceRequest.trade_date == run.trade_date,
        InspectionEvidenceDecision.decision == "approve").execution_options(populate_existing=True)
    rows = list(db.execute(statement.with_for_update() if current_read else statement))
    if not rows:
        return {}
    candidates = {row["candidate_id"]: row for row in _candidates(db, portfolio_id, run.trade_date, current_read=current_read)}
    scope, result = _scope(db, portfolio_id, current_read=current_read), {}
    for request, decision in rows:
        frozen = json.loads(request.source_snapshot_json)
        current = candidates.get(frozen["candidate_id"])
        if (current and current["run_id"] == decision_run_id and current["source_hash"] == request.source_hash == decision.source_hash
            and decision.request_hash == request.request_hash
            and (request.baseline_id, request.generation) == (scope["baseline_id"], scope["generation"])):
            result[current["order_id"]] = {"evidence_id": current["evidence_id"], "request_id": request.id,
                "decision_id": decision.id, "source_hash": current["source_hash"]}
    return result
