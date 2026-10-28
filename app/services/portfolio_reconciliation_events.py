"""Same-connection source outbox: account writes and receipts commit or roll back together.

Captures SQLAlchemy ORM AND Core DML, including worker bulk status updates.
No after-commit callback and no background trading execution. Direct external
database writers must use a supported ingest connection or an equivalent CDC.
"""
from __future__ import annotations
import json
from datetime import date, datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import event, inspect, select, func
from sqlalchemy.engine import Engine
from sqlalchemy.sql.dml import Insert, Update, Delete
from app.models.portfolio_inspection_workflow import ReconciliationSourceEvent

TASK_TYPES = {"portfolio_auto_simulation", "portfolio_resume", "portfolio_auto_trade", "auto_trade", "portfolio_inspection_repair", "portfolio_data_repair"}
FIELDS = {
    "cash_ledger": {"id", "portfolio_id", "amount", "balance_after", "entry_type", "ref_type", "ref_id", "created_at"},
    "sim_orders": {"id", "portfolio_id", "symbol_id", "side", "quantity", "filled_quantity", "filled_price", "filled_amount", "fee", "status", "review_status", "decision_evidence_id", "source_type", "execution_mode", "decision_snapshot_json", "signal_snapshot_json", "client_order_key", "created_at", "filled_at"},
    "sim_trades": {"id", "portfolio_id", "symbol_id", "order_id", "side", "quantity", "price", "amount", "fee", "realized_pnl", "created_at"},
    "positions": {"id", "portfolio_id", "symbol_id", "quantity", "avg_cost"},
    "decision_runs": {"id", "portfolio_id", "run_type", "is_result_production_eligible", "blocking_status", "trade_date", "status", "decision_at", "data_cutoff_at", "execution_at", "created_at"},
    "decision_evidence": {"id", "portfolio_id", "decision_run_id", "trade_date", "symbol_id", "action", "target_quantity", "target_qty_delta", "execution_at", "executed_price", "content_hash", "versions_json"},
    "decision_order_plans": {"order_plan_id", "portfolio_id", "decision_run_id", "evidence_id", "symbol_id", "signal_date", "execution_date", "action", "target_quantity", "direction", "intended_price", "created_at"},
    "async_tasks": {"id", "task_type", "payload_json", "status", "result_json", "cancel_requested"},
    "inspection_evidence_decisions": {"id", "portfolio_id", "request_id", "decision", "source_hash"},
}
_installed = False


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _mapping(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _read_rows(conn, stmt, params, *, ids=None):
    table = stmt.table
    pk = next(iter(table.primary_key.columns))
    fields = [table.c[name] for name in FIELDS[table.name] if name in table.c]
    query = select(*fields)
    if ids:
        query = query.where(pk.in_(ids))
    elif isinstance(stmt, (Update, Delete)):
        query = query.where(*stmt._where_criteria)
    else:
        return []
    return [dict(row) for row in conn.execute(query, params or {}).mappings()]


def _observed(stmt):
    if not isinstance(stmt, (Insert, Update, Delete)) or stmt.table.name not in FIELDS:
        return False
    values = getattr(stmt, "_values", None)
    if isinstance(stmt, Update) and values is not None and len(values):
        return bool({getattr(key, "name", str(key)) for key in values} & FIELDS[stmt.table.name])
    return True


def _before(conn, stmt, multiparams, params, options):
    if not _observed(stmt):
        return
    groups = list(multiparams) if multiparams else [params]
    previous = []
    if not isinstance(stmt, Insert):
        for group in groups:
            previous.extend(_read_rows(conn, stmt, group))
    # Serialize source writes with confirmation/coverage changes on the same
    # account root. The receipt itself uses the identical connection/transaction.
    from app.models.portfolio import Portfolio
    compiled_values = dict(stmt.compile().params)
    owners = {row.get("portfolio_id") for row in previous + [compiled_values] + [g for g in groups if isinstance(g, dict)]}
    if stmt.table.name == "async_tasks":
        for row in previous + [compiled_values] + [g for g in groups if isinstance(g, dict)]:
            payload, result = _mapping(row.get("payload_json")), _mapping(row.get("result_json"))
            if payload.get("dry_run") is True:
                continue
            owners.add(payload.get("portfolio_id"))
            owners.update(payload.get("portfolio_ids") or [])
            for field in ("items", "portfolio_results"):
                owners.update(item.get("portfolio_id") for item in result.get(field, []) if isinstance(item, dict))
            if row.get("task_type") == "portfolio_auto_trade" and not payload.get("portfolio_id") and not payload.get("portfolio_ids"):
                owners.update(conn.scalars(select(Portfolio.id).where(Portfolio.auto_trade_enabled == 1, Portfolio.account_type == "simulated")))
    owners = {owner for owner in owners if isinstance(owner, int)}
    if owners and inspect(conn).has_table("portfolios"):
        list(conn.execute(select(Portfolio.id).where(Portfolio.id.in_(owners)).order_by(Portfolio.id).with_for_update()))
        if not isinstance(stmt, Insert):
            previous = [row for group in groups for row in _read_rows(conn, stmt, group)]
    # A stack handles the source SELECTs and nested Core inserts without
    # leaking transaction state to a pooled connection after exceptions.
    conn.info.setdefault("inspection_source_stack", []).append((stmt, previous))


def _after(conn, stmt, multiparams, params, options, result):
    if not _observed(stmt):
        return
    stack = conn.info.get("inspection_source_stack", [])
    previous = []
    for index in range(len(stack)-1, -1, -1):
        if stack[index][0] is stmt:
            previous = stack.pop(index)[1]
            break
    output = []
    pk_name = next(iter(stmt.table.primary_key.columns)).name
    if not isinstance(stmt, Delete):
        ids = [row[pk_name] for row in previous if row.get(pk_name) is not None]
        if isinstance(stmt, Insert):
            try:
                ids = [row[0] for row in result.inserted_primary_key_rows if row and row[0] is not None]
            except Exception:
                ids = []
        if ids:
            output = _read_rows(conn, stmt, {}, ids=ids)
        elif isinstance(stmt, Insert):
            output = [{key: value for key, value in row.items() if key in FIELDS[stmt.table.name]}
                      for row in result.context.compiled_parameters]
    old = {str(row.get(pk_name)): row for row in previous}
    new = {str(row[pk_name]) if row.get(pk_name) is not None else f"unresolved-{i}": row for i, row in enumerate(output)}
    receipts = []
    for key in sorted(old.keys() | new.keys()):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        current = after or before
        if stmt.table.name == "inspection_evidence_decisions" and current.get("decision") != "approve":
            continue
        if stmt.table.name == "decision_runs":
            if not any(row and row.get("run_type") == "auto_simulation" and row.get("is_result_production_eligible") == 1 for row in (before, after)):
                continue
        if stmt.table.name in {"decision_evidence", "decision_order_plans"}:
            from app.models.decision_engine import DecisionRun
            parents = {row["decision_run_id"] for row in (before, after) if row and row.get("decision_run_id")}
            if not conn.scalar(select(DecisionRun.id).where(DecisionRun.id.in_(parents),
                    DecisionRun.run_type == "auto_simulation", DecisionRun.is_result_production_eligible == 1).limit(1)):
                continue
        if stmt.table.name == "async_tasks":
            if current.get("task_type") not in TASK_TYPES:
                continue
            payload = _mapping(current.get("payload_json"))
            if payload.get("dry_run") is True:
                continue
            pid = payload.get("portfolio_id")
            task_result = _mapping(current.get("result_json"))
            task_owners = {item.get("portfolio_id") for field in ("items", "portfolio_results")
                for item in task_result.get(field, []) if isinstance(item, dict)} | set(payload.get("portfolio_ids") or [])
            if isinstance(pid, int):
                task_owners.add(pid)
            if not task_owners and current.get("task_type") == "portfolio_auto_trade":
                from app.models.portfolio import Portfolio
                task_owners = set(conn.scalars(select(Portfolio.id).where(Portfolio.auto_trade_enabled == 1, Portfolio.account_type == "simulated")))
        else:
            pid = current.get("portfolio_id")
            task_owners = {pid}
        # Account membership can change: invalidate both owning portfolios.
        owners = {p for p in task_owners if isinstance(p, int)} | {row["portfolio_id"] for row in (before, after) if row and isinstance(row.get("portfolio_id"), int)}
        for owner in owners:
            receipts.append({"id": f"source-{uuid4().hex}", "portfolio_id": owner, "source_table": stmt.table.name,
                "source_key": str(current[pk_name]) if current.get(pk_name) is not None else None,
                "operation": "delete" if isinstance(stmt, Delete) else "insert" if isinstance(stmt, Insert) else "update",
                "facts_json": canonical({"before": before, "after": after, "source_timezone": "UTC"}),
                "status": "pending", "created_at": datetime.now(timezone.utc).replace(tzinfo=None)})
    if not receipts:
        return
    if not inspect(conn).has_table(ReconciliationSourceEvent.__tablename__):
        # Legacy databases without any active scope retain their previous
        # behavior until migration. Monitored scopes must never silently lose
        # a receipt while reporting their source write as successful.
        if inspect(conn).has_table("portfolio_reconciliation_cursors"):
            from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor
            if conn.scalar(select(PortfolioReconciliationCursor.portfolio_id).where(
                    PortfolioReconciliationCursor.portfolio_id.in_({r["portfolio_id"] for r in receipts})).limit(1)):
                conn.rollback()
                raise RuntimeError("RECONCILIATION_SOURCE_MIGRATION_REQUIRED")
        return
    try:
        conn.execute(ReconciliationSourceEvent.__table__.insert(), receipts)
    except Exception:
        # Core callers can catch a statement exception and try to commit. Roll
        # back the source write too, even on dialects that keep failed SQL alive.
        conn.rollback()
        raise
    conn.info.setdefault("inspection_own_receipts", set()).update(row["id"] for row in receipts)


def _clear_transaction(conn, *_):
    try:
        conn.info.pop("inspection_source_stack", None)
        conn.info.pop("inspection_own_receipts", None)
    except Exception:
        # 回滚的连接可能已被服务端断开（例如 MySQL wait_timeout），此时访问
        # conn.info 会重新校验连接并抛 PendingRollbackError。清理标记属于
        # 簿记动作，不能反过来让调用方的 rollback 失败。
        pass


def _clear_error(context):
    if context.connection is not None:
        context.connection.info.pop("inspection_source_stack", None)


def install_source_receipts():
    global _installed
    if not _installed:
        event.listen(Engine, "before_execute", _before)
        event.listen(Engine, "after_execute", _after)
        event.listen(Engine, "begin", _clear_transaction)
        event.listen(Engine, "commit", _clear_transaction)
        event.listen(Engine, "rollback", _clear_transaction)
        event.listen(Engine, "handle_error", _clear_error)
        _installed = True


def pending_source_events(db, portfolio_id, *, ignore_own=False, current_read=False):
    """Read-only admission proof. New receipts must never leave stale green UI."""
    with db.no_autoflush:
        return _pending_source_events(db, portfolio_id, ignore_own=ignore_own, current_read=current_read)


def _pending_source_events(db, portfolio_id, *, ignore_own=False, current_read=False):
    if not inspect(db.connection()).has_table(ReconciliationSourceEvent.__tablename__):
        return {"count": 0, "status": "MIGRATION_REQUIRED", "events": []}
    query = select(ReconciliationSourceEvent).where(ReconciliationSourceEvent.portfolio_id == portfolio_id,
        ReconciliationSourceEvent.status.in_(["pending", "waiting_calendar", "waiting_coverage"]))
    if ignore_own:
        query = query.where(ReconciliationSourceEvent.id.not_in(db.connection().info.get("inspection_own_receipts", set())))
    rows = list(db.scalars((query.with_for_update() if current_read else query).order_by(ReconciliationSourceEvent.created_at, ReconciliationSourceEvent.id)))
    blocking = [row for row in rows if row.source_table != "async_tasks" or
        str((_mapping(row.facts_json).get("after") or {}).get("status", "")).lower() in {"failed", "cancelled", "done", "partial", "interrupted"}]
    material_count = sum(row.source_table in {"cash_ledger", "sim_orders", "sim_trades", "positions", "inspection_evidence_decisions"} for row in rows)
    from hashlib import sha256
    return {"count": len(rows), "blocking_count": len(blocking), "material_count": material_count,
        "fingerprint": sha256(canonical([row.id for row in rows]).encode()).hexdigest(),
        "status": "NEEDS_RECHECK" if rows else "CURRENT", "events": [
        {"id": row.id, "source_table": row.source_table, "source_key": row.source_key,
         "operation": row.operation, "status": row.status, "created_at": row.created_at.isoformat()} for row in rows[:30]]}


def acknowledge_baseline_sources(db, portfolio_id, baseline_id, generation):
    """Called only after locked independent account verification is accepted."""
    if not inspect(db.connection()).has_table(ReconciliationSourceEvent.__tablename__):
        return
    for row in db.scalars(select(ReconciliationSourceEvent).where(ReconciliationSourceEvent.portfolio_id == portfolio_id,
            ReconciliationSourceEvent.status.in_(["pending", "waiting_calendar", "waiting_coverage"])).with_for_update()):
        row.status, row.processed_at = "covered_by_baseline", datetime.now(timezone.utc).replace(tzinfo=None)
        row.processing_result_json = canonical({"baseline_id": baseline_id, "generation": generation,
            "reconciliation_passed": False, "reason": "已纳入本人接受的当前账户基线；旧期未知保留"})


def assert_account_sources_checked_for_buy(db, portfolio_id):
    from app.services.portfolio_reconciliation_activation import _active
    from app.services.portfolio_reconciliation_coverage import CoverageError
    active = _active(db, portfolio_id, current_read=True)
    if active is None:
        return
    pending = pending_source_events(db, portfolio_id, ignore_own=True, current_read=True)
    if pending["status"] == "MIGRATION_REQUIRED":
        raise CoverageError("RECONCILIATION_SOURCE_PENDING", "来源事件存储尚未部署，请先完成迁移并核验来源后再新买入")
    from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationDay
    unfinished = db.scalar(select(PortfolioReconciliationDay.id).where(
        PortfolioReconciliationDay.cursor_id == active["cursor_id"], PortfolioReconciliationDay.generation == active["generation"],
        PortfolioReconciliationDay.account_source_pending == 1,
        PortfolioReconciliationDay.requested_evidence_version > PortfolioReconciliationDay.processed_evidence_version).limit(1).with_for_update())
    if pending["status"] == "MIGRATION_REQUIRED" or pending.get("material_count", 0) or unfinished:
        raise CoverageError("RECONCILIATION_SOURCE_PENDING", "账户来源已有新变化，等待自动核对或手动重检后再新买入；风险退出仍独立可用")


def _business_date(value):
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        if len(str(value)) == 10:
            return date.fromisoformat(str(value))
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at).astimezone(ZoneInfo("Asia/Shanghai")).date()
    except ValueError:
        return None


def event_dates(db, event_row):
    from app.models.decision_engine import DecisionRun, DecisionEvidence
    from app.models.sim_account import SimOrder
    facts = _mapping(event_row.facts_json)
    dates = set()
    for row in (facts.get("before"), facts.get("after")):
        if not row:
            continue
        for key in ("trade_date", "execution_date", "created_at", "filled_at", "execution_at"):
            value = _business_date(row.get(key))
            if value:
                dates.add(value)
        payload = _mapping(row.get("payload_json"))
        value = _business_date(payload.get("trade_date"))
        if value:
            dates.add(value)
        for operation in payload.get("operations", []):
            if isinstance(operation, dict) and (value := _business_date(operation.get("trade_date"))):
                dates.add(value)
        run_id = row.get("decision_run_id")
        evidence_id = row.get("decision_evidence_id")
        if event_row.source_table == "sim_trades" and row.get("order_id"):
            evidence_id = db.scalar(select(SimOrder.decision_evidence_id).where(SimOrder.id == row["order_id"]))
        if evidence_id:
            run_id = db.scalar(select(DecisionEvidence.decision_run_id).where(DecisionEvidence.id == evidence_id))
        if run_id:
            value = db.scalar(select(DecisionRun.trade_date).where(DecisionRun.id == run_id, DecisionRun.portfolio_id == event_row.portfolio_id))
            if value:
                dates.add(value)
        if event_row.source_table == "inspection_evidence_decisions":
            from app.models.portfolio_inspection_workflow import InspectionEvidenceRequest
            value = db.scalar(select(InspectionEvidenceRequest.trade_date).where(InspectionEvidenceRequest.id == row.get("request_id")))
            if value:
                dates.add(value)
    # Undated position corrections concern the current account, not a made-up
    # historical date. Preserve that observation and invalidate current scope.
    return dates


def consume_source_events(db, *, portfolio_id=None, limit=200):
    from app.models.portfolio import Portfolio
    from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationCursor, PortfolioReconciliationDay
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    from app.services.portfolio_reconciliation_job import _work_day, request_reconciliation_check
    from app.services.data_governance_audit import write_audit_event
    if not inspect(db.connection()).has_table(ReconciliationSourceEvent.__tablename__):
        return {"processed": 0, "status": "MIGRATION_REQUIRED"}
    # Legacy portfolios retain receipts but cannot starve monitored accounts.
    statement = select(ReconciliationSourceEvent.portfolio_id).join(PortfolioReconciliationCursor,
        PortfolioReconciliationCursor.portfolio_id == ReconciliationSourceEvent.portfolio_id).where(
        ReconciliationSourceEvent.status.in_(["pending", "waiting_calendar", "waiting_coverage"]),
        PortfolioReconciliationCursor.scope_key == "account")
    if portfolio_id is not None:
        statement = statement.where(ReconciliationSourceEvent.portfolio_id == portfolio_id)
    owners = sorted(set(db.scalars(statement.group_by(ReconciliationSourceEvent.portfolio_id).order_by(
        func.min(ReconciliationSourceEvent.created_at), ReconciliationSourceEvent.portfolio_id).limit(limit))))
    events = []
    for owner in owners:
        # Same lock order as source writers, baseline acceptance and confirms.
        if db.scalar(select(Portfolio.id).where(Portfolio.id == owner).with_for_update()) is None:
            continue
        events.extend(db.scalars(select(ReconciliationSourceEvent).where(ReconciliationSourceEvent.portfolio_id == owner,
            ReconciliationSourceEvent.status.in_(["pending", "waiting_calendar", "waiting_coverage"])).order_by(
            ReconciliationSourceEvent.created_at, ReconciliationSourceEvent.id).limit(
                min(max(1, limit//max(1,len(owners))), max(0, limit-len(events)))).with_for_update()))
    calendar = resolve_inspection_calendar(db)
    actual_dates = sorted(calendar.get("trading_dates", [])) if calendar.get("status") == "PASSED" else []
    done = 0
    for receipt in events:
        cursor = db.scalar(select(PortfolioReconciliationCursor).where(PortfolioReconciliationCursor.portfolio_id == receipt.portfolio_id,
            PortfolioReconciliationCursor.scope_key == "account").with_for_update())
        if cursor is None:
            receipt.status = "waiting_coverage"
            continue
        if not actual_dates or cursor.coverage_start_trade_date not in actual_dates:
            receipt.status = "waiting_calendar"
            continue
        dates = event_dates(db, receipt)
        first = min(dates) if dates else cursor.coverage_start_trade_date
        # Recheck conservatively from the actual affected business date (or
        # the preceding true session for T+1/account activity), never assign
        # an arbitrary weekend as an executed trading day.
        has_decision_date = receipt.source_table in {"decision_runs", "decision_evidence", "decision_order_plans", "inspection_evidence_decisions"}
        facts = _mapping(receipt.facts_json)
        if receipt.source_table == "async_tasks":
            has_decision_date = any(_mapping(row.get("payload_json")).get("trade_date") or
                _mapping(row.get("payload_json")).get("operations") for row in (facts.get("before"), facts.get("after")) if row)
        preceding = [day for day in actual_dates if cursor.coverage_start_trade_date <= day < first]
        target = first if has_decision_date and first in actual_dates else preceding[-1] if preceding else cursor.coverage_start_trade_date
        if first < cursor.coverage_start_trade_date:
            target = cursor.coverage_start_trade_date
        affected = set(db.scalars(select(PortfolioReconciliationDay.target_trade_date).where(
            PortfolioReconciliationDay.cursor_id == cursor.id, PortfolioReconciliationDay.generation == cursor.generation,
            PortfolioReconciliationDay.target_trade_date >= target))) | {target}
        before = {"verified_through": str(cursor.verified_through_trade_date), "row_version": cursor.row_version}
        for day_date in sorted(affected):
            day = _work_day(db, receipt.portfolio_id, day_date, create=True, lock=True)
            day.requested_evidence_version += 1
            if receipt.source_table in {"cash_ledger", "sim_orders", "sim_trades", "positions", "inspection_evidence_decisions"}:
                day.account_source_pending = 1
            day.work_state, day.next_check_at, day.last_error_code, day.attempt_count = "queued", datetime.now(timezone.utc).replace(tzinfo=None), None, 0
        if cursor.verified_through_trade_date and target <= cursor.verified_through_trade_date:
            prefix = [day for day in actual_dates if cursor.coverage_start_trade_date <= day < target]
            cursor.verified_through_trade_date = prefix[-1] if prefix else None
        cursor.row_version += 1
        # Wake existing schedules without counting this demand a second time.
        request_reconciliation_check(db, receipt.portfolio_id, trade_date=target, register_demand=False)
        receipt.status, receipt.processed_at = "processed", datetime.now(timezone.utc).replace(tzinfo=None)
        receipt.processing_result_json = canonical({"generation": cursor.generation, "baseline_id": cursor.baseline_id,
            "business_dates": sorted(dates), "recheck_from": target, "affected_dates": sorted(affected)})
        write_audit_event(db, "UNKNOWN_AUDIT_ACTION", portfolio_id=receipt.portfolio_id,
            business_key=receipt.id, operator_id="reconciliation-source-consumer", occurred_at=receipt.processed_at,
            before=before, after={"verified_through": str(cursor.verified_through_trade_date), "row_version": cursor.row_version},
            attributes={"original_action": "RECONCILIATION_SOURCE_RECEIVED", "source_event_id": receipt.id,
                "source_table": receipt.source_table, "source_record_ref": f"{receipt.source_table}:{receipt.source_key}" if receipt.source_key else receipt.id,
                "trade_date": str(target), "generation": cursor.generation, "bypass": False,
                "reconciliation_passed": False, "affected_dates": [str(day) for day in affected]})
        done += 1
    db.flush()
    return {"processed": done, "observed": len(events), "status": "SCANNED"}
