"""WP0-2 / G3-3 组合对账服务：T+1 日对 T 日决策执行结果做一致性核对。

对账链路（沿 tasks.md §WP0-2 「last_reconciled_trade_date」口径）：
  Inputs（任一缺失仍要尽力对，结果含 NOT_CHECKED）：
    (A) 决策侧证据：DecisionRun(run_type=auto_simulation, trade_date=T) +
                    DecisionEvidence(symbol, action, target_quantity)
    (B) 订单侧计划：OrderPlan / SimulationOrder（本项目用 BacktestTrade 或 外部订单系统）
    (C) 撮合结果：simulation_matching_engine.MatchResult（FILLED / 部分成交 / 拒单）
    (D) 最终持仓：执行日快照，或完整模拟成交账本按执行日重建

  核对 4 类差异：
    R1. 有决策 BUY/SELL 但无订单计划 → MISSING_ORDER_PLAN
    R2. 有订单计划但无撮合结果（T+1 仍停留 PENDING）→ UNFILLED_PLAN
    R3. 撮合后最终持仓与预期不一致（数量/方向/成本）→ POSITION_MISMATCH
    R4. 现金 + Σ(持仓×收盘价) ≠ 期初资产（守恒破坏）→ NAV_BROKEN

  输出：
    - ReconciliationReport（差异明细 + 汇总）
    - 若无差异：Portfolio.last_reconciled_trade_date = T（只推进，不跳日，不等号安全）
    - 若有差异或关键证据未核验：写入 RECONCILIATION_RESULT 审计事件并进入
      RECONCILIATION_BLOCKED，留待人工修正 + 确认
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.models.backtest import BacktestRun, BacktestTrade  # legacy/import evidence
from app.models.decision_engine import DecisionRun as DecisionRunORM, DecisionEvidence
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.services.cash_ledger_diagnosis import diagnose_cash_ledger

from app.services.data_governance_audit import (
    AuditWriteResult,
    write_audit_event,
)


DiffKind = Literal[
    "MISSING_ORDER_PLAN",
    "UNFILLED_PLAN",
    "POSITION_MISMATCH",
    "NAV_BROKEN",
    "DECISION_RUN_NOT_FOUND",
    "NOT_CHECKED",
    "NOT_DUE",
    "EXECUTION_IN_PROGRESS",
    "EXECUTION_FAILED",
    "READ_ERROR",
]

_CURRENT_READ_KEY = "portfolio_reconciliation_current_read"


@contextmanager
def _read_mode(db: Session, current_read: bool):
    previous = db.info.get(_CURRENT_READ_KEY)
    db.info[_CURRENT_READ_KEY] = bool(current_read or previous)
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_CURRENT_READ_KEY, None)
        else:
            db.info[_CURRENT_READ_KEY] = previous


def _execute_read(db: Session, statement, parameters=None):
    if db.info.get(_CURRENT_READ_KEY):
        if hasattr(statement, "with_for_update"):
            statement = statement.with_for_update().execution_options(populate_existing=True)
        elif db.get_bind().dialect.name != "sqlite":
            # The only raw reads in this module fingerprint an entire
            # portfolio's source rows, so range locks also prevent new inserts.
            statement = text(str(statement) + " FOR UPDATE")
    return db.execute(statement, parameters or {})


@dataclass
class ReconciliationDiff:
    symbol_id: int | None
    kind: DiffKind
    expected: Any = None
    actual: Any = None
    detail: str | None = None


@dataclass
class ReconciliationReport:
    portfolio_id: int
    trade_date: date
    decision_run_id: str | None
    status: Literal["PASSED", "BLOCKED"]
    expected_position_count: int | None = None
    actual_position_count: int | None = None
    expected_cash: float | None = None
    actual_cash: float | None = None
    differences: list[ReconciliationDiff] = field(default_factory=list)
    audit: AuditWriteResult | None = None
    cash_audit: dict[str, Any] | None = None
    verdict: str = "EVIDENCE_INCOMPLETE"
    work_state: str = "needs_review"
    reason_codes: list[str] = field(default_factory=list)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    report_id: str | None = None
    report_version: str | None = None
    next_check_at: str | None = None
    progression: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] | None = None
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    activity_evidence: list[dict[str, Any]] = field(default_factory=list)
    execution_expected: bool | None = None
    execution_expected_evidence: list[dict[str, Any]] = field(default_factory=list)
    execution_status: str = "UNKNOWN"
    execution_window: dict[str, Any] = field(default_factory=dict)
    is_trading_day: bool | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[dict[str, Any]] = field(default_factory=list)
    can_confirm: bool = False
    confirm_blockers: list[str] = field(default_factory=list)
    generated_at: str | None = None
    evidence_hash: str | None = None
    expected_requested_evidence_version: int | None = None

    @property
    def has_any_diff(self) -> bool:
        # Missing evidence is an incomplete check, never a clean account.
        return bool(self.differences)


def _activity_evidence(db: Session, report: ReconciliationReport) -> list[dict[str, Any]]:
    """Read each source independently; successful zero and failed read differ.

    Order/trade/cash dates are Shanghai activity dates. Current positions are
    explicitly a current observation, never an alleged historical snapshot.
    """
    local_start = datetime.combine(report.trade_date, datetime.min.time(), ZoneInfo("Asia/Shanghai"))
    start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end = (local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    facts = []
    sources = ((SimOrder, "模拟订单", "sim_order"), (SimTrade, "模拟成交", "sim_trade"),
               (CashLedger, "现金流水账", "cash_ledger"), (Position, "当前持仓记录", "position"))
    for model, label, prefix in sources:
        code = model.__tablename__
        try:
            # Read the fields used by this evidence only. Optional UI/order
            # columns (for example limit_price/note/filled_at) are not proof of
            # activity and need not make an otherwise readable source unknown.
            columns = [getattr(model, key) for key in ("id", "symbol_id", "status", "side", "quantity", "amount",
                "source_type", "decision_evidence_id") if hasattr(model, key)]
            statement = select(*columns).where(model.portfolio_id == report.portfolio_id).order_by(model.id)
            if model is not Position:
                statement = statement.where(model.created_at >= start, model.created_at < end)
            rows = list(_execute_read(db, statement))
            facts.append({"evidence_id": f"activity:{report.portfolio_id}:{report.trade_date}:{code}",
                "source_code": code, "source_label": label, "query_status": "CURRENT_ONLY" if model is Position else "QUERIED",
                "count": len(rows), "trade_date": report.trade_date.isoformat(),
                "source_record_refs": [f"{prefix}:{row.id}" for row in rows],
                "records": [{"id": row.id, **{key: getattr(row, key) for key in (
                    "symbol_id", "status", "side", "quantity", "amount", "source_type", "decision_evidence_id") if hasattr(row, key)}} for row in rows],
                "reason": "仅当前持仓观察，不作为历史收盘持仓证明" if model is Position else "已读取本组合该业务日的真实记录，数量为零代表查询成功且无记录"})
        except Exception:
            facts.append({"evidence_id": f"activity:{report.portfolio_id}:{report.trade_date}:{code}",
                "source_code": code, "source_label": label, "query_status": "READ_ERROR", "count": None,
                "trade_date": report.trade_date.isoformat(), "source_record_refs": [], "records": [],
                "reason": "该来源读取失败，不能视为零活动；请恢复来源读取后重检"})
    return facts

def _describe_report(db: Session, report: ReconciliationReport) -> None:
    """Keep the legacy status while exposing evidence and work as separate facts.

    The version is an immutable content fingerprint, not an invented database
    report sequence. It can be compared between a read-only preview and the
    mandatory confirmation-time check without adding a migration.
    """
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    calendar = resolve_inspection_calendar(db, current_read=bool(db.info.get(_CURRENT_READ_KEY)))
    report.generated_at = datetime.now(timezone.utc).isoformat()
    report.is_trading_day = report.trade_date in calendar["trading_dates"] if calendar["status"] == "PASSED" and report.trade_date.year == datetime.now(ZoneInfo("Asia/Shanghai")).year else None
    report.activity_evidence = _activity_evidence(db, report)
    from app.services.portfolio_execution_history import read_execution_dispatch_history
    dispatch_history = read_execution_dispatch_history(db, report.portfolio_id, report.trade_date)
    report.activity_evidence.append(dispatch_history)
    for fact in report.activity_evidence:
        if fact["query_status"] == "READ_ERROR":
            report.differences.append(ReconciliationDiff(None, "READ_ERROR", detail=f"{fact['source_label']}读取失败，活动数量未知"))
    if report.coverage and calendar.get("status") != "PASSED" and not any(
            d.kind == "NOT_CHECKED" and "日历" in (d.detail or "") for d in report.differences):
        report.differences.append(ReconciliationDiff(None, "NOT_CHECKED",
            detail="当前真实交易所日历来源尚未核验，不能将新范围核对记为通过"))
    kinds = {d.kind for d in report.differences}
    report.reason_codes = sorted(kinds)
    real_diffs = kinds & {"NAV_BROKEN", "POSITION_MISMATCH", "MISSING_ORDER_PLAN", "UNFILLED_PLAN"}
    if real_diffs:
        report.verdict, report.work_state = "DIFF_FOUND", "needs_review"
        code, label = "REVIEW_DIFFERENCES", "查看差异并修复"
    elif "NOT_DUE" in kinds or "EXECUTION_IN_PROGRESS" in kinds:
        report.verdict = "NOT_DUE" if "NOT_DUE" in kinds else "EXECUTION_IN_PROGRESS"
        report.work_state = "waiting_execution"
        code, label = "VIEW_EXECUTION_PROGRESS", "查看执行进度"
    elif "EXECUTION_FAILED" in kinds:
        report.verdict, report.work_state = "EXECUTION_FAILED", "needs_review"
        code, label = "INSPECT_EXECUTION", "核查未完成执行与已有成交"
    elif "DECISION_RUN_NOT_FOUND" in kinds and any(fact["count"] for fact in report.activity_evidence
            if fact["query_status"] == "QUERIED"):
        report.verdict, report.work_state = "UNVERIFIED_ACTIVITY", "needs_review"
        code, label = "INSPECT_ACTIVITY_SOURCES", "核对真实活动来源"
    elif kinds:
        # A missing run is not proof that a historical run was required or
        # missed. Until historical configuration is available it stays unknown.
        report.verdict, report.work_state = "EVIDENCE_INCOMPLETE", "needs_review"
        code, label = ("INSPECT_EXECUTION", "核查执行证据") if "DECISION_RUN_NOT_FOUND" in kinds else ("INSPECT_MISSING_EVIDENCE", "查看缺失证据")
    else:
        report.verdict, report.work_state = "PASSED", "done"
        code, label = "VIEW_RECONCILIATION_REPORT", "查看核对报告"
    report.missing_evidence = [
        {"code": d.kind, "symbol_id": d.symbol_id, "detail": d.detail}
        for d in report.differences if d.kind in {"NOT_CHECKED", "DECISION_RUN_NOT_FOUND"}
    ]
    report.actions = [{"code": code, "label": label, "enabled": True,
                       "portfolio_id": report.portfolio_id,
                       "trade_date": report.trade_date.isoformat(),
                       "requires_confirmation": False}]
    report.report_id = f"reconciliation::{report.portfolio_id}::{report.trade_date.isoformat()}"
    sources: dict[str, Any] = {}
    inspector = inspect(db.connection())
    tables = set(inspector.get_table_names())
    # Reflect only existing columns: old deployments can lack later ORM-only
    # columns. Include complete source rows so new activity invalidates a prior
    # confirmation even when it happens to leave the final balance unchanged.
    quote = db.get_bind().dialect.identifier_preparer.quote
    for table in ("decision_runs", "decision_evidence", "sim_orders", "sim_trades", "cash_ledger", "positions"):
        if table not in tables:
            sources[table] = {"available": False}
            continue
        columns = [c["name"] for c in inspector.get_columns(table)]
        if "portfolio_id" not in columns:
            continue
        statement = f"SELECT * FROM {quote(table)} WHERE portfolio_id=:pid"
        if "id" in columns:
            statement += " ORDER BY id"
        sources[table] = [dict(row) for row in _execute_read(db, text(statement), {"pid": report.portfolio_id}).mappings()]
    from app.services.portfolio_inspection_supplement import approved_order_associations
    associations = approved_order_associations(db, report.portfolio_id, report.decision_run_id) if report.decision_run_id else {}
    sources["execution_dispatch_history"] = dispatch_history['records'] if dispatch_history['query_status'] == 'CONTEXT_ONLY' else {'available': False}
    sources["reviewed_order_associations"] = [{"id": item["decision_id"], "order_id": oid, **item}
        for oid, item in sorted(associations.items())]
    report.source_refs = [{"source": name, "available": not isinstance(rows, dict),
        "row_ids": [str(row["id"]) for row in rows if "id" in row] if isinstance(rows, list) else [],
        "content_hash": hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()}
        for name, rows in sources.items()]
    report.source_refs.append({"source": "exchange_trade_calendar", "available": calendar.get("status") == "PASSED",
        "source_identity": calendar.get("source"), "version": calendar.get("version")})
    if report.coverage:
        report.source_refs.append({"source": "accepted_account_baseline", "available": True,
            "baseline_id": report.coverage["baseline_id"], "generation": report.coverage["generation"],
            "content_hash": hashlib.sha256(json.dumps(report.coverage["baseline_snapshot"],
                sort_keys=True, default=str).encode()).hexdigest()})
    baseline_identity = {key: report.coverage.get(key) for key in (
        "generation", "baseline_id", "coverage_start_trade_date", "calendar_version")} if report.coverage else None
    payload = {"portfolio_id": report.portfolio_id, "trade_date": report.trade_date,
               "verdict": report.verdict, "sources": sources,
               "coverage": baseline_identity,
               "observed_calendar": {key: calendar.get(key) for key in ("status", "source", "version")},
               "differences": [vars(d) for d in report.differences]}
    report.report_version = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
    report.evidence_hash = report.report_version
    report.execution_expected_evidence = [{"status": "UNKNOWN" if report.execution_expected is None else "VERIFIED",
        "source_label": "目标日执行义务历史", "source_code": "historical_execution_configuration",
        "reason": "缺少带生效区间的历史接管及调度开关，不能由当前开关倒推当时是否应执行" if report.execution_expected is None else
                  "已找到本组合本日实际执行尝试，仍需按原执行窗口核对结果"}]
    report.findings = [vars(diff).copy() for diff in report.differences]
    report.checks = [
        {"check_code": "EXECUTION_CHAIN", "label": "执行链完整性", "status": report.execution_status,
         "expected": report.execution_expected, "actual": report.decision_run_id, "source_label": "决策运行记录",
         "source_record_ref": f"decision_run:{report.decision_run_id}" if report.decision_run_id else None},
        {"check_code": "POSITIONS", "label": "历史持仓核对", "status": "FAILED" if "POSITION_MISMATCH" in kinds else
            "PASSED" if report.actual_position_count is not None and report.expected_position_count is not None else "NOT_CHECKED",
         "expected": report.expected_position_count, "actual": report.actual_position_count,
         "reason": None if report.actual_position_count is not None else "缺少对应执行日的可信持仓快照或完整账本重建证据"},
        {"check_code": "CASH", "label": "资金守恒核对", "status": (report.cash_audit or {}).get("status", "NOT_CHECKED"),
         "expected": report.expected_cash, "actual": report.actual_cash,
         "source_label": "现金流水账", "scope": "当前完整账本及已接受基线后的流水，历史时点另需快照来源"}]
    report.next_actions = [{"action_id": action["code"].lower(), "kind": "inspect", "is_primary": True,
        "target": "governance:reconciliation", "required_role": None, "expected_effect": "查看真实证据与缺失项后重新核对",
        "confirmation_required": False, **action} for action in report.actions]
    if report.decision_run_id is None:
        report.next_actions.extend([
            {"action_id": "resolve_historical_missed_execution", "kind": "confirm", "is_primary": False,
             "label": "记录历史未执行并申请恢复", "enabled": False, "disabled_code": "MISSED_EXECUTION_POLICY_NOT_APPROVED",
             "disabled_reason": "历史应执行依据和允许未执行处置的批准策略尚未齐全，不能凭备注推进或伪造执行",
             "target": "governance:reconciliation", "confirmation_required": True},
            {"action_id": "review_current_account_coverage", "kind": "inspect", "is_primary": False,
             "label": "核查当前账户与历史不可恢复出口", "enabled": True, "target": "governance:reconciliation",
             "expected_effect": "通过实际账户覆盖预览核验当前资产与义务；未知历史保持原样", "confirmation_required": False}])
    report.can_confirm = report.verdict == "PASSED" and not report.differences
    report.confirm_blockers = list(dict.fromkeys(diff.detail or diff.kind for diff in report.differences))


def _active_coverage(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict | None:
    if "portfolio_reconciliation_cursors" not in inspect(db.connection()).get_table_names():
        return None
    from app.services import portfolio_reconciliation_coverage as coverage_service
    reader = getattr(coverage_service, "get_active_coverage", None)
    if reader is not None:
        return reader(db, portfolio_id, current_read=current_read)
    # Additive-deployment compatibility while the coverage reader is upgraded.
    from app.models.portfolio_reconciliation_coverage import PortfolioAccountBaseline
    result = coverage_service.get_reconciliation_coverage(db, portfolio_id)
    cursor = result.get("cursor")
    if cursor is None:
        return None
    baseline = db.get(PortfolioAccountBaseline, cursor["baseline_id"])
    if baseline is None:
        raise ValueError("活动覆盖范围缺少已接受的账户基线，请重新读取核验来源")
    return {**cursor, "cursor_id": cursor["id"], "baseline_snapshot": json.loads(baseline.snapshot_json)}


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _utc_stamp(value: Any) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _baseline_account_at_execution_date(db: Session, p: Portfolio, report: ReconciliationReport,
        execution_date: date) -> dict[int, float] | None:
    """Replay only journal rows after a verified, personally accepted baseline.

    Old unknown decision history is never rebuilt from today's positions.
    The frozen row IDs are the boundary; a newly inserted backdated row must
    trigger review instead of silently changing the accepted opening book.
    """
    baseline = report.coverage["baseline_snapshot"]
    try:
        opening_at = _utc_stamp(baseline["as_of_at"])
        opening_cash = Decimal(str(baseline["cash"]))
        if not opening_cash.is_finite() or opening_cash < 0:
            return None
        opening = {int(row["symbol_id"]): Decimal(str(row["quantity"])) for row in baseline["positions"]}
        costs = {int(row["symbol_id"]): Decimal(str(row["avg_cost"])) for row in baseline["positions"]}
        if any(not q.is_finite() or q < 0 for q in opening.values()):
            return None
        if any(not cost.is_finite() or cost < 0 for cost in costs.values()):
            return None
        watermarks = {row["source"]: set(map(str, row["row_ids"])) for row in baseline["source_watermarks"]}
        if not {"cash_ledger", "sim_trades", "sim_orders"}.issubset(watermarks):
            return None
        new_trades = [row for row in _execute_read(db, select(SimTrade.id, SimTrade.order_id, SimTrade.symbol_id,
            SimTrade.side, SimTrade.quantity, SimTrade.price, SimTrade.amount, SimTrade.fee, SimTrade.created_at).where(
            SimTrade.portfolio_id == p.id).order_by(SimTrade.id)).all()
            if str(row.id) not in watermarks["sim_trades"]]
        cash_rows = [row for row in _execute_read(db, select(CashLedger.id, CashLedger.created_at, CashLedger.entry_type,
            CashLedger.amount, CashLedger.balance_after, CashLedger.ref_type, CashLedger.ref_id).where(
            CashLedger.portfolio_id == p.id).order_by(CashLedger.id)).all()
            if str(row.id) not in watermarks["cash_ledger"]]
        orders = {row.id: row for row in _execute_read(db, select(SimOrder.id, SimOrder.symbol_id, SimOrder.side,
            SimOrder.filled_quantity).where(SimOrder.portfolio_id == p.id)).all()}
        current = dict(opening)
        historical = dict(opening)
        fill_qty, fill_cash = {}, {}
        for row in new_trades:
            at = _utc_stamp(row.created_at)
            order = orders.get(row.order_id)
            side, qty = str(row.side).lower(), Decimal(str(row.quantity))
            if (at <= opening_at or order is None or str(order.id) in watermarks["sim_orders"]
                or order.symbol_id != row.symbol_id or str(order.side).lower() != side
                or side not in {"buy", "sell"} or not qty.is_finite() or qty <= 0):
                return None
            amount, fee, price = Decimal(str(row.amount)), Decimal(str(row.fee)), Decimal(str(row.price))
            if (not amount.is_finite() or not fee.is_finite() or not price.is_finite()
                or price <= 0 or amount < 0 or fee < 0 or abs(qty * price - amount) > Decimal("0.01")):
                return None
            delta = qty if side == "buy" else -qty
            before_qty = current.get(row.symbol_id, Decimal(0))
            current[row.symbol_id] = current.get(row.symbol_id, Decimal(0)) + delta
            if current[row.symbol_id] < 0:
                return None
            if side == "buy":
                # Match the actual simulated account's per-fill cost contract,
                # including its two-decimal rounding after every buy.
                from app.services.sim_accounts import _round_money
                costs[row.symbol_id] = Decimal(str(_round_money(
                    (float(before_qty) * float(costs.get(row.symbol_id, Decimal(0))) + float(qty) * float(price))
                    / float(current[row.symbol_id]))))
            elif current[row.symbol_id] == 0:
                costs[row.symbol_id] = Decimal(0)
            if at.astimezone(ZoneInfo("Asia/Shanghai")).date() <= execution_date:
                historical[row.symbol_id] = historical.get(row.symbol_id, Decimal(0)) + delta
            fill_qty[order.id] = fill_qty.get(order.id, Decimal(0)) + qty
            movement = -amount - fee if side == "buy" else amount - fee
            fill_cash[order.id] = fill_cash.get(order.id, Decimal(0)) + movement
        if any(abs(Decimal(str(order.filled_quantity or 0)) - fill_qty.get(oid, Decimal(0))) > Decimal("0.000001")
               for oid, order in orders.items() if str(oid) not in watermarks["sim_orders"]):
            return None
        position_rows = list(_execute_read(db, select(Position.symbol_id, Position.quantity, Position.avg_cost).where(Position.portfolio_id == p.id)))
        if len(position_rows) != len({row.symbol_id for row in position_rows}):
            return None
        actual_positions = {row.symbol_id: Decimal(str(row.quantity)) for row in position_rows}
        if any(not value.is_finite() or value < 0 for value in actual_positions.values()):
            return None
        actual_costs = {row.symbol_id: Decimal(str(row.avg_cost)) for row in position_rows}
        if any(not value.is_finite() or value < 0 for value in actual_costs.values()):
            return None
        for sid in set(actual_positions) | set(current):
            actual_qty, expected_qty = actual_positions.get(sid, Decimal(0)), current.get(sid, Decimal(0))
            if (abs(actual_qty - expected_qty) > Decimal("0.000001") or
                actual_qty > 0 and abs(actual_costs.get(sid, Decimal(0)) - costs.get(sid, Decimal(0))) > Decimal("0.01")):
                report.differences.append(ReconciliationDiff(sid, "POSITION_MISMATCH",
                    expected={"quantity": str(expected_qty), "avg_cost": str(costs.get(sid, Decimal(0)))},
                    actual={"quantity": str(actual_qty), "avg_cost": str(actual_costs.get(sid, Decimal(0)))},
                    detail="当前账户持仓或成本与已接受期初加新范围完整成交不一致"))
        running_cash, order_cash = opening_cash, {}
        for row in cash_rows:
            if _utc_stamp(row.created_at) <= opening_at:
                return None
            amount, balance = Decimal(str(row.amount)), Decimal(str(row.balance_after))
            if not amount.is_finite() or not balance.is_finite():
                return None
            running_cash += amount
            if abs(running_cash - balance) > Decimal("0.01"):
                report.differences.append(ReconciliationDiff(None, "NAV_BROKEN",
                    expected={"cash": str(running_cash)}, actual={"cash": str(balance)},
                    detail=f"新覆盖范围现金流水 {row.id} 与已接受期初资金不守恒"))
            kind = str(row.entry_type).lower()
            if kind in {"buy", "sell"}:
                if row.ref_type != "sim_order" or row.ref_id not in orders:
                    return None
                order_cash[row.ref_id] = order_cash.get(row.ref_id, Decimal(0)) + amount
            elif not (kind in {"deposit", "withdraw"} and row.ref_type == "portfolio" and row.ref_id == p.id
                      and ((kind == "deposit" and amount > 0) or (kind == "withdraw" and amount < 0))):
                return None
        if any(abs(order_cash.get(oid, Decimal(0)) - fill_cash.get(oid, Decimal(0))) > Decimal("0.01")
               for oid in set(order_cash) | set(fill_cash)):
            report.differences.append(ReconciliationDiff(None, "NAV_BROKEN",
                detail="新覆盖范围逐单现金与实际成交金额及费用不一致"))
        report.expected_cash = float(running_cash)
        report.actual_cash = float(cash_rows[-1].balance_after) if cash_rows else float(opening_cash)
        report.cash_audit = {"status": "BROKEN" if any(d.kind == "NAV_BROKEN" for d in report.differences) else "CONSISTENT",
            "source": "accepted_baseline_and_post_baseline_journal", "baseline_id": report.coverage["baseline_id"]}
        return {sid: float(qty) for sid, qty in historical.items()}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def latest_reconciliation_results(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict[date, dict[str, Any]]:
    """Read immutable report facts; incomplete/invalid history remains unknown."""
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    statement = select(DataGovernanceAuditEvent).where(
        DataGovernanceAuditEvent.portfolio_id == portfolio_id,
        DataGovernanceAuditEvent.action == "RECONCILIATION_RESULT",
    ).order_by(DataGovernanceAuditEvent.id.desc())
    if current_read:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    rows = _execute_read(db, statement).scalars()
    results: dict[date, dict[str, Any]] = {}
    for row in rows:
        try:
            attrs = json.loads(row.attributes_json or "{}")
            day = date.fromisoformat(attrs["trade_date"])
        except (ValueError, TypeError, KeyError):
            continue
        if day not in results:
            results[day] = {**attrs, "audit_event_id": row.id}
    return results


def _checkpoint_progression(db: Session, p: Portfolio, report: ReconciliationReport) -> None:
    """Never jump an established checkpoint over unknown or blocked dates.

    Bootstrap keeps the legacy first verified day, and expressly does not
    claim an accepted account baseline or verified history preceding that day.
    The persisted baseline/generation workflow will replace this adapter.
    """
    previous = p.last_reconciled_trade_date
    report.progression = {"can_advance": True, "verified_through": previous.isoformat() if previous else None,
                          "coverage_source": "legacy_checkpoint" if previous else "legacy_first_verified_day",
                          "historical_coverage_unknown": True}
    if previous is None:
        p.last_reconciled_trade_date = report.trade_date
        report.progression["verified_through"] = report.trade_date.isoformat()
        return
    if report.trade_date <= previous:
        return
    from app.models.market_data import MarketCalendar
    calendar = list(_execute_read(db, select(MarketCalendar).where(
        MarketCalendar.trade_date > previous, MarketCalendar.trade_date <= report.trade_date,
    ).order_by(MarketCalendar.trade_date)).scalars())
    if len(calendar) != (report.trade_date - previous).days:
        report.progression.update(can_advance=False, reason="CALENDAR_UNKNOWN")
        return
    results = latest_reconciliation_results(db, p.id)
    through = previous
    for entry in calendar:
        if not entry.is_trading_day:
            continue
        attrs = results.get(entry.trade_date, {})
        passed = entry.trade_date == report.trade_date or attrs.get("status") == "PASSED"
        if not passed:
            report.progression.update(can_advance=False, reason="PREVIOUS_DATE_UNVERIFIED",
                                      first_blocked_trade_date=entry.trade_date.isoformat())
            break
        through = entry.trade_date
    p.last_reconciled_trade_date = through
    report.progression["verified_through"] = through.isoformat()


@dataclass(frozen=True)
class _ExecutionFill:
    """Read-only normalized fill used by the reconciliation seam."""

    symbol_id: int
    quantity: float
    side: str


def _load_auto_simulation_execution(
    db: Session,
    *,
    portfolio_id: int,
    decision_run_id: str,
) -> tuple[set[str], list[_ExecutionFill]]:
    """Load the exact simulated execution chain for one DecisionRun.

    ``SimOrder.decision_evidence_id`` is the durable ownership boundary.  A
    historical order for the same symbol or date must never be used merely
    because it happens to look similar.  ``SimTrade`` is authoritative for
    ledger fills; ``SimOrder.filled_quantity`` is retained as a compatibility
    fallback for orders created before per-fill rows were available.
    """
    # Select only the stable attribution columns.  Older Alembic-created
    # ``sim_orders`` tables can legitimately lack later ORM-only fields such
    # as ``limit_price``; selecting the entity would make a read-only
    # reconciliation fail before it can inspect its actual evidence link.
    order_columns = {
        column["name"]
        for column in inspect(db.connection()).get_columns("sim_orders")
    }
    required_order_columns = {
        "id", "portfolio_id", "symbol_id", "side", "filled_quantity",
        "decision_evidence_id",
    }
    if not required_order_columns.issubset(order_columns):
        return set(), []

    from app.services.portfolio_inspection_supplement import approved_order_associations
    from sqlalchemy import or_, case
    associations = approved_order_associations(db, portfolio_id, decision_run_id)
    attribution = case({oid: item["evidence_id"] for oid, item in associations.items()}, value=SimOrder.id,
        else_=SimOrder.decision_evidence_id) if associations else SimOrder.decision_evidence_id
    orders = list(_execute_read(db,
        select(
            SimOrder.id,
            SimOrder.symbol_id,
            SimOrder.side,
            SimOrder.filled_quantity,
            attribution.label("decision_evidence_id"),
        )
        .outerjoin(DecisionEvidence, DecisionEvidence.id == SimOrder.decision_evidence_id)
        .where(
            SimOrder.portfolio_id == portfolio_id,
            or_(DecisionEvidence.decision_run_id == str(decision_run_id), SimOrder.id.in_(list(associations))),
        )
        .order_by(SimOrder.id)
    ).all())
    if not orders:
        return set(), []

    order_ids = [int(order.id) for order in orders]
    trade_columns = {
        column["name"]
        for column in inspect(db.connection()).get_columns("sim_trades")
    }
    required_trade_columns = {"order_id", "symbol_id", "side", "quantity"}
    fills_by_order: dict[int, list[tuple[int, str, float]]] = {}
    if required_trade_columns.issubset(trade_columns):
        for fill in _execute_read(db,
            select(
                SimTrade.order_id,
                SimTrade.symbol_id,
                SimTrade.side,
                SimTrade.quantity,
            )
            .where(SimTrade.order_id.in_(order_ids))
            .order_by(SimTrade.order_id, SimTrade.id)
        ).all():
            fills_by_order.setdefault(int(fill.order_id), []).append(
                (int(fill.symbol_id), str(fill.side or ""), float(fill.quantity or 0.0))
            )

    order_evidence_ids: set[str] = set()
    normalized: list[_ExecutionFill] = []
    for order in orders:
        if order.decision_evidence_id:
            order_evidence_ids.add(str(order.decision_evidence_id))
        fills = fills_by_order.get(int(order.id), [])
        if fills:
            normalized.extend(
                _ExecutionFill(
                    symbol_id=symbol_id,
                    quantity=quantity,
                    side=side or str(order.side or ""),
                )
                for symbol_id, side, quantity in fills
                if quantity > 0
            )
            continue

        # Transitional/repair compatibility: a persisted filled quantity is
        # still evidence of matching even when old data has no SimTrade row.
        filled_quantity = float(order.filled_quantity or 0.0)
        if filled_quantity > 0:
            normalized.append(_ExecutionFill(
                symbol_id=int(order.symbol_id),
                quantity=filled_quantity,
                side=str(order.side or ""),
            ))
    return order_evidence_ids, normalized


def _positions_at_execution_date(
    db: Session, portfolio: Portfolio, execution_date: date,
    cash_audit: dict[str, Any] | None,
) -> dict[int, float] | None:
    """Rebuild only a complete, attributable simulated account from its fills.

    Position has no historical date. It is used only to validate completeness
    of the lifetime fill ledger, never as the requested historical snapshot.
    Unexplained manual positions and legacy missing fills remain unverified.
    """
    if not cash_audit or cash_audit.get("status") != "CONSISTENT":
        return None
    opening = _execute_read(db, select(CashLedger.entry_type, CashLedger.ref_type,
        CashLedger.ref_id, CashLedger.note, CashLedger.amount, CashLedger.balance_after)
        .where(CashLedger.portfolio_id == portfolio.id)
        .order_by(CashLedger.id).limit(1)).first()
    if opening is None or opening.entry_type != "deposit":
        return None
    standard_funding = (portfolio.account_type == "simulated"
        and opening.ref_type == "portfolio" and opening.ref_id == portfolio.id
        and opening.note == "Initial simulated funding")
    replay_funding = False
    if opening.ref_type == "backtest_run" and opening.ref_id is not None:
        source = _execute_read(db, select(BacktestRun.status, BacktestRun.initial_capital)
            .where(BacktestRun.id == opening.ref_id)).first()
        # Applying a run to a different simulated portfolio is supported.
        # Its source portfolio therefore cannot be used as the ownership test.
        replay_funding = (portfolio.account_type == "simulated" and source is not None
            and source.status == "success" and float(source.initial_capital or 0) > 0
            and round(float(opening.amount), 2) == round(float(source.initial_capital), 2)
            and round(float(opening.balance_after), 2) == round(float(source.initial_capital), 2)
            and opening.note in {
                f"Apply backtest run #{opening.ref_id} initial capital",
                f"Apply backtest run #{opening.ref_id} initial capital (no trades)",
            })
    if not standard_funding and not replay_funding:
        return None

    tables = inspect(db.connection()).get_table_names()
    if "sim_orders" not in tables or "sim_trades" not in tables:
        return None
    orders = {int(row.id): row for row in _execute_read(db, select(SimOrder.id,
        SimOrder.symbol_id, SimOrder.side, SimOrder.filled_quantity)
        .where(SimOrder.portfolio_id == portfolio.id)).all()}
    fills = _execute_read(db, select(SimTrade.id, SimTrade.order_id, SimTrade.symbol_id,
        SimTrade.side, SimTrade.quantity, SimTrade.created_at)
        .where(SimTrade.portfolio_id == portfolio.id)
        .order_by(SimTrade.created_at, SimTrade.id)).all()
    cash_refs = _execute_read(db, select(CashLedger.ref_type, CashLedger.ref_id)
        .where(CashLedger.portfolio_id == portfolio.id,
            CashLedger.entry_type.in_(["buy", "sell"]))).all()
    if any(row.ref_type != "sim_order" or row.ref_id is None for row in cash_refs):
        return None
    filled_by_order: dict[int, float] = {}
    total: dict[int, float] = {}
    historical: dict[int, float] = {}
    for fill in fills:
        order = orders.get(int(fill.order_id))
        side = str(fill.side or "").lower()
        quantity = float(fill.quantity or 0)
        if (order is None or order.symbol_id != fill.symbol_id
            or str(order.side or "").lower() != side
            or side not in {"buy", "sell"} or quantity <= 0 or fill.created_at is None):
            return None
        filled_by_order[int(fill.order_id)] = filled_by_order.get(int(fill.order_id), 0) + quantity
        sid = int(fill.symbol_id)
        delta = quantity if side == "buy" else -quantity
        total[sid] = total.get(sid, 0) + delta
        if total[sid] < -1e-6:  # A sale requiring an unknown opening position.
            return None
        timestamp = fill.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date() <= execution_date:
            historical[sid] = historical.get(sid, 0) + delta
    if any(abs(float(order.filled_quantity or 0) - filled_by_order.get(oid, 0)) > 1e-6
        for oid, order in orders.items()):
        return None
    current = {int(sid): float(qty or 0) for sid, qty in _execute_read(db,
        select(Position.symbol_id, Position.quantity).where(Position.portfolio_id == portfolio.id))}
    if any(abs(current.get(sid, 0) - total.get(sid, 0)) > 1e-6
        for sid in set(current) | set(total)):
        return None
    return historical


# ──────────────────────────────────────────────────────────── 主函数
def reconcile_trade_date(
    db: Session,
    portfolio_id: int,
    trade_date: date,
    *,
    operator_id: str = "cron_worker",
    correlation_id: str | None = None,
    # 注入点：仿真交易撮合订单表；本项目若未建专用 order_plan，使用 BacktestTrade 作为
    # 撮合明细（回测路径兼容），真实自动仿真需接 SimulationOrder。
    trades_provider=None,  # Callable[[Session, int, date], list[BacktestTradeLike]]
    positions_provider=None,  # Callable[[Session, int, date], dict[int, int] | None]
    persist: bool = True,
    now: datetime | None = None,
    current_read: bool = False,
    expected_requested_evidence_version: int | None = None,
) -> ReconciliationReport:
    if persist:
        current_read = True
        with db.no_autoflush:
            db.scalar(select(Portfolio.id).where(Portfolio.id == portfolio_id).with_for_update())
        from app.services.portfolio_reconciliation_events import consume_source_events
        consume_source_events(db, portfolio_id=portfolio_id)
    with _read_mode(db, current_read), db.no_autoflush:
        return _reconcile_trade_date(db, portfolio_id, trade_date, operator_id=operator_id,
            correlation_id=correlation_id, trades_provider=trades_provider,
            positions_provider=positions_provider, persist=persist, now=now,
            expected_requested_evidence_version=expected_requested_evidence_version)


def _reconcile_trade_date(
    db: Session, portfolio_id: int, trade_date: date, *, operator_id="cron_worker",
    correlation_id=None, trades_provider=None, positions_provider=None,
    persist=True, now=None, expected_requested_evidence_version=None,
) -> ReconciliationReport:
    """对 T 日决策 → T+1 最终持仓做对账。返回差异明细 + 推进 last_reconciled_trade_date。"""
    current_read = bool(db.info.get(_CURRENT_READ_KEY))
    p = db.get(Portfolio, portfolio_id, with_for_update=current_read, populate_existing=current_read)
    if p is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    # 1. 找 DecisionRun(auto_simulation, trade_date=T)
    dr = _execute_read(db,
        select(DecisionRunORM).where(
            DecisionRunORM.portfolio_id == portfolio_id,
            DecisionRunORM.run_type.in_(["auto_simulation"]),  # DB value
            DecisionRunORM.trade_date == trade_date,
            DecisionRunORM.status == "SUCCEEDED",
        ).order_by(DecisionRunORM.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    report = ReconciliationReport(
        portfolio_id=portfolio_id,
        trade_date=trade_date,
        decision_run_id=None if dr is None else str(dr.id),
        status="PASSED",
        coverage=_active_coverage(db, portfolio_id, current_read=current_read),
        expected_requested_evidence_version=expected_requested_evidence_version,
    )

    def finish() -> None:
        _describe_report(db, report)
        if persist:
            _finalize_report(db, p, report, operator_id, correlation_id)
        else:
            report.status = "BLOCKED" if report.has_any_diff else "PASSED"

    check_at = _utc_stamp(now or datetime.now(timezone.utc))
    baseline_positions = None
    if report.coverage:
        start = _as_date(report.coverage["coverage_start_trade_date"])
        if trade_date < start:
            report.differences.append(ReconciliationDiff(None, "NOT_CHECKED",
                detail=f"本日早于当前已接受覆盖起点 {start.isoformat()}，旧期历史证据保持未知，不能用新基线覆盖"))
            finish()
            return report
        if trade_date > check_at.astimezone(ZoneInfo("Asia/Shanghai")).date():
            report.differences.append(ReconciliationDiff(None, "NOT_DUE",
                detail="目标交易日尚未到来，等待真实执行证据后核对新覆盖范围"))
            finish()
            return report
        at_date = _utc_stamp(dr.execution_at).astimezone(ZoneInfo("Asia/Shanghai")).date() if dr and dr.execution_at else trade_date
        baseline_positions = _baseline_account_at_execution_date(db, p, report, at_date)
        if baseline_positions is None:
            report.differences.append(ReconciliationDiff(None, "NOT_CHECKED",
                detail="已接受基线之后的账户流水、成交、持仓或来源边界未核清，请核查新增来源后重检"))

    # Cash arithmetic is meaningful even when today's automatic execution
    # evidence is missing. Its absence must not hide an existing book gap.
    if not report.coverage and "cash_ledger" in inspect(db.connection()).get_table_names():
        rows = _execute_read(db, select(CashLedger.id, CashLedger.entry_type,
            CashLedger.amount, CashLedger.balance_after, CashLedger.ref_type, CashLedger.ref_id)
            .where(CashLedger.portfolio_id == portfolio_id)
            .order_by(CashLedger.id)).mappings().all()
        fills = None
        if 'sim_trades' in inspect(db.connection()).get_table_names():
            fills = [dict(row) for row in _execute_read(db, select(SimTrade.order_id,
                SimTrade.side, SimTrade.amount, SimTrade.fee)
                .where(SimTrade.portfolio_id == portfolio_id)).mappings()]
        report.cash_audit = diagnose_cash_ledger([dict(row) for row in rows], fills)
        if report.cash_audit.get('expected_cash') is not None:
            report.expected_cash = report.cash_audit['expected_cash']
            report.actual_cash = report.cash_audit['actual_cash']
        if report.cash_audit['status'] == 'BROKEN':
            report.differences.append(ReconciliationDiff(
                symbol_id=None, kind='NAV_BROKEN',
                expected={"cash": report.expected_cash}, actual={"cash": report.actual_cash},
                detail='完整现金账本的收支累计与记录余额不一致；需核对历史流水，未经确认不调整账本。',
            ))
    if not report.coverage and (not report.cash_audit or report.cash_audit.get("status") in {"NOT_CHECKED", "PARTIAL"}):
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail="现金流水缺失或缺少可核验的期初资金，现金核对尚未完成。",
        ))

    if dr is None:
        latest = _execute_read(db, select(DecisionRunORM).where(
            DecisionRunORM.portfolio_id == portfolio_id,
            DecisionRunORM.run_type == "auto_simulation",
            DecisionRunORM.trade_date == trade_date,
        ).order_by(DecisionRunORM.created_at.desc(), DecisionRunORM.id.desc()).limit(1)).scalar_one_or_none()
        if latest is not None and str(latest.status).upper() in {"PENDING", "QUEUED", "RUNNING"}:
            report.decision_run_id = str(latest.id)
            report.differences.append(ReconciliationDiff(
                symbol_id=None, kind="EXECUTION_IN_PROGRESS",
                detail="本日决策任务仍在排队或执行，等待实际结果后核对；不补造历史交易。",
            ))
            finish()
            return report
        if latest is not None and str(latest.status).upper() in {"FAILED", "INTERRUPTED", "CANCELLED"}:
            report.decision_run_id = str(latest.id)
            report.execution_expected = True
            report.execution_status = str(latest.status).upper()
            report.differences.append(ReconciliationDiff(None, "EXECUTION_FAILED",
                detail="本日真实执行尝试未成功，先核查已成交与未完成对象，不重新执行整日"))
        report.differences.append(ReconciliationDiff(
            symbol_id=None,
            kind="DECISION_RUN_NOT_FOUND",
            detail=(
                f"portfolio_id={portfolio_id} trade_date={trade_date.isoformat()} "
                "未找到可核验的当日自动运行及决策明细；历史接管和调度配置未知，不能据此判断当时是否应执行或已执行，请核查原始来源"
            ),
        ))
        finish()
        return report

    check_at = now or datetime.now(timezone.utc)
    check_at = check_at.replace(tzinfo=timezone.utc) if check_at.tzinfo is None else check_at.astimezone(timezone.utc)
    execution_at = dr.execution_at
    report.execution_expected = True
    report.execution_status = str(dr.status).upper()
    report.execution_window = {"decision_at": _utc_stamp(dr.decision_at).isoformat() if dr.decision_at else None,
                               "execution_at": _utc_stamp(execution_at).isoformat() if execution_at else None}
    if execution_at is None:
        report.differences.append(ReconciliationDiff(symbol_id=None, kind="NOT_CHECKED",
            detail="决策记录缺少预期执行时点，无法判断是否已到核对时间。"))
        finish()
        return report
    execution_at = execution_at.replace(tzinfo=timezone.utc) if execution_at.tzinfo is None else execution_at.astimezone(timezone.utc)
    if execution_at > check_at:
        report.next_check_at = execution_at.isoformat()
        report.differences.append(ReconciliationDiff(symbol_id=None, kind="NOT_DUE",
            detail=f"预计执行时点为 {execution_at.isoformat()}，当前尚未到期，等待执行结果。"))
        finish()
        return report

    # 2. 决策侧预期（只关心 BUY/SELL/HOLD）
    evidences = _execute_read(db,
        select(DecisionEvidence).where(
            DecisionEvidence.decision_run_id == str(dr.id),
        )
    ).scalars().all()
    if not evidences and (dr.universe_count or dr.member_count):
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail="决策运行已完成，但缺少对应的决策明细，尚不能完成对账。",
        ))
    expected_qty_delta: dict[int, int] = {}  # symbol_id -> target_qty_delta
    expected_target_quantity: dict[int, int] = {}
    unknown_target_symbols: set[int] = set()
    evidence_id_by_symbol: dict[int, str] = {}
    for ev in evidences:
        if ev.symbol_id is None:
            continue
        delta = int(getattr(ev, "target_qty_delta", 0) or 0)
        target = int(getattr(ev, "target_quantity", 0) or 0)
        action = str(getattr(ev, "action", "") or "")
        if ev.target_quantity is None:
            unknown_target_symbols.add(int(ev.symbol_id))
            report.differences.append(ReconciliationDiff(
                symbol_id=int(ev.symbol_id), kind="NOT_CHECKED",
                detail="决策记录缺少目标持仓数量，不能按零持仓核对。",
            ))
        if action in {"BUY", "SELL"}:
            expected_qty_delta[ev.symbol_id] = delta
            expected_target_quantity[ev.symbol_id] = target
            evidence_id_by_symbol[ev.symbol_id] = str(ev.id)
        elif action in {"HOLD", "NO_ACTION", "DATA_BLOCKED", "REJECTED"}:
            # 仍在范围内，记录为 0 变化（便于 R3 比对最终持仓与目标）
            expected_qty_delta.setdefault(ev.symbol_id, 0)
            if ev.target_quantity is not None:
                expected_target_quantity.setdefault(ev.symbol_id, target)

    # 3. 订单 / 撮合侧实际
    trades: list = []
    execution_source_connected = False
    order_evidence_ids: set[str] = set()
    if trades_provider is not None:
        trades = list(trades_provider(db, portfolio_id, trade_date) or [])
        execution_source_connected = True
    else:
        # The auto-simulation path owns a stable Evidence -> SimOrder foreign
        # key.  Prefer it to any symbol/date heuristic or historical backtest
        # row, and keep the source scoped to this exact DecisionRun.
        order_evidence_ids, trades = _load_auto_simulation_execution(
            db,
            portfolio_id=portfolio_id,
            decision_run_id=str(dr.id),
        )
        execution_source_connected = bool(order_evidence_ids)

        # Legacy/import data predating SimOrder attribution may still be
        # uncheckable. Never let that absence hide a partial linked order.
        if not execution_source_connected:
            try:
                bt_rows = _execute_read(db,
                    select(BacktestTrade).where(
                        getattr(BacktestTrade, "portfolio_id", None) == portfolio_id,
                        BacktestTrade.trade_date == trade_date,
                    )
                ).scalars().all()
                trades = list(bt_rows)
                execution_source_connected = bool(trades)
            except Exception:
                trades = []

    if trades_provider is None and not execution_source_connected and any(expected_qty_delta.values()):
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail=(
                "未接入统一 order_plan / SimulationOrder 数据源；"
                "仅对 决策侧预期 vs 最终持仓 做 R3 对比。"
            ),
        ))

    actual_filled_delta: dict[int, int] = {}
    for t in trades:
        sid = int(getattr(t, "symbol_id", 0))
        qty = int(getattr(t, "quantity", 0) or 0)
        side = str(getattr(t, "side", "") or "").upper()
        sign = 1 if side in {"BUY", "OPEN", "LONG"} else (-1 if side in {"SELL", "CLOSE", "SHORT"} else 0)
        if sign != 0:
            actual_filled_delta[sid] = actual_filled_delta.get(sid, 0) + sign * qty

    # R1: 有 BUY/SELL 决策但无对应撮合变化且无订单计划（简化：缺订单记录即 MISSING）
    for sid, delta in expected_qty_delta.items():
        if delta == 0:
            continue
        filled = actual_filled_delta.get(sid, 0)
        if filled == 0 and not execution_source_connected:
            # 订单表空 → 已标记 NOT_CHECKED，不再重复记 MISSING
            pass
        elif filled == 0:
            evidence_id = evidence_id_by_symbol.get(sid)
            if order_evidence_ids and evidence_id in order_evidence_ids:
                report.differences.append(ReconciliationDiff(
                    symbol_id=sid,
                    kind="UNFILLED_PLAN",
                    expected={"target_qty_delta": delta},
                    actual={"filled_delta": 0},
                    detail=(
                        "已创建与 DecisionEvidence 精确关联的模拟订单，"
                        "但尚未产生可对账成交。"
                    ),
                ))
            else:
                report.differences.append(ReconciliationDiff(
                    symbol_id=sid,
                    kind="MISSING_ORDER_PLAN",
                    expected={"target_qty_delta": delta},
                    actual={"filled_delta": 0},
                    detail=(
                        f"DecisionEvidence 动作={delta}，但该 symbol 在 trade_date={trade_date} "
                        "无任何订单/撮合记录。"
                    ),
                ))
        elif abs(filled - delta) > 0:
            report.differences.append(ReconciliationDiff(
                symbol_id=sid,
                kind="UNFILLED_PLAN",
                expected={"target_qty_delta": delta},
                actual={"filled_delta": filled},
            ))

    # 4. R3：最终持仓与目标持仓一致
    execution_date = execution_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    post_positions: dict[int, int] | None
    if positions_provider is not None:
        # A provider supplies a dated closing snapshot, not today's portfolio.
        supplied = positions_provider(db, portfolio_id, execution_date)
        post_positions = None if supplied is None else dict(supplied)
    else:
        post_positions = baseline_positions if report.coverage else _positions_at_execution_date(db, p, execution_date, report.cash_audit)
    if post_positions is None:
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail=f"缺少 {execution_date.isoformat()} 的可核验持仓记录，不能用当前持仓代替历史持仓。",
        ))
        finish()
        return report
    report.actual_position_count = sum(1 for q in post_positions.values() if q > 0)
    report.expected_position_count = None if unknown_target_symbols or (not evidences and (dr.universe_count or dr.member_count)) else sum(
        1 for sid, q in expected_target_quantity.items() if q > 0
    )

    for sid in sorted(set(expected_target_quantity) | set(post_positions)):
        if sid in unknown_target_symbols:
            continue
        target_qty = expected_target_quantity.get(sid, 0)
        actual = int(post_positions.get(sid, 0))
        if target_qty != actual:
            report.differences.append(ReconciliationDiff(
                symbol_id=sid,
                kind="POSITION_MISMATCH",
                expected={"target_quantity": target_qty},
                actual={"final_position_quantity": actual},
                detail=(
                    f"{sid}: 目标持仓 {target_qty} ≠ 实际 {actual}"
                ),
            ))

    finish()
    return report


# ──────────────────────────────────────────────────────────── 推进 last_reconciled_trade_date
def _finalize_report(
    db: Session,
    p: Portfolio,
    report: ReconciliationReport,
    operator_id: str,
    correlation_id: str | None,
) -> None:
    context = {"source_timezone": "UTC", "activity_evidence": report.activity_evidence, "checks": report.checks,
        "execution_expected": report.execution_expected, "execution_expected_evidence": report.execution_expected_evidence,
        "execution_status": report.execution_status, "execution_window": report.execution_window,
        "is_trading_day": report.is_trading_day, "next_actions": report.next_actions,
        "can_confirm": report.can_confirm, "confirm_blockers": report.confirm_blockers,
        "evidence_hash": report.evidence_hash}
    diffs_public = [
        {
            "symbol_id": d.symbol_id,
            "kind": d.kind,
            "expected": d.expected,
            "actual": d.actual,
            "detail": d.detail,
        }
        for d in report.differences
    ]
    if report.coverage and report.trade_date >= _as_date(report.coverage["coverage_start_trade_date"]):
        from app.services.portfolio_reconciliation_coverage import record_coverage_reconciliation
        recorded = record_coverage_reconciliation(db, portfolio_id=p.id, trade_date=report.trade_date,
            verdict=report.verdict, checks={**context, "source_report_id": report.report_id,
                "source_report_version": report.report_version, "cash": report.cash_audit,
                "expected_position_count": report.expected_position_count,
                "actual_position_count": report.actual_position_count,
                "decision_run_id": report.decision_run_id, "next_check_at": report.next_check_at},
            findings=diffs_public, source_refs=report.source_refs, evidence_hash=report.report_version,
            closure_type="RECONCILED" if report.verdict == "PASSED" else None,
            expected_generation=report.coverage["generation"], expected_cursor_row_version=report.coverage["row_version"],
            expected_requested_evidence_version=report.expected_requested_evidence_version, operator_id=operator_id,
            correlation_id=correlation_id)
        report.progression = {**recorded, "coverage_source": "accepted_account_baseline",
            "verified_through": recorded.get("verified_through_trade_date").isoformat() if recorded.get("verified_through_trade_date") else None,
            "historical_coverage_unknown": bool(report.coverage.get("history_gaps"))}
    elif report.coverage:
        report.progression = {"can_advance": False, "coverage_source": "outside_current_coverage",
                              "reason": "HISTORICAL_EVIDENCE_UNKNOWN"}
    if report.has_any_diff:
        report.status = "BLOCKED"
        # The audit result and the order-entry stop must be one transaction.
        # Incomplete historical evidence also blocks advancement; callers
        # must never advertise an unverified account as reconciled.
        from app.services.portfolio_state_machine import _get_status, _set_status

        current_state = _get_status(db, p)
        waiting = report.work_state == "waiting_execution"
        outside_current_coverage = bool(report.coverage and report.trade_date < _as_date(report.coverage["coverage_start_trade_date"]))
        if current_state != "ADMIN_PAUSED" and not waiting and not outside_current_coverage:
            _set_status(db, p, "RECONCILIATION_BLOCKED")
            p.status_reconciliation = "RECONCILIATION_BLOCKED"
        attributes = {**context,
            "status": report.status,
            "trade_date": report.trade_date.isoformat(),
            "decision_run_id": report.decision_run_id,
            "expected_position_count": report.expected_position_count,
            "actual_position_count": report.actual_position_count,
            "expected_cash": report.expected_cash,
            "actual_cash": report.actual_cash,
            "diffs_count_non_checked": sum(
                1 for d in report.differences if d.kind != "NOT_CHECKED"
            ),
            "incomplete_checks_count": sum(
                1 for d in report.differences if d.kind == "NOT_CHECKED"
            ),
            "portfolio_state_before": current_state,
            "portfolio_state_after": (
                current_state if current_state == "ADMIN_PAUSED" or waiting or outside_current_coverage
                else "RECONCILIATION_BLOCKED"
            ),
            "diffs": diffs_public,
            "verdict": report.verdict, "work_state": report.work_state,
            "report_id": report.report_id, "report_version": report.report_version,
            "reason_codes": report.reason_codes, "actions": report.actions,
            "missing_evidence": report.missing_evidence,
            "next_check_at": report.next_check_at,
            "progression": report.progression,
            "coverage_generation": report.coverage["generation"] if report.coverage else None,
            "baseline_id": report.coverage["baseline_id"] if report.coverage else None,
            "outside_current_coverage": outside_current_coverage,
        }
        # 不推进 last_reconciled_trade_date；留待人工 confirm_reconciliation_fixed
        report.audit = write_audit_event(
            db, "RECONCILIATION_RESULT",
            portfolio_id=p.id,
            business_key=report.decision_run_id or f"portfolio::{p.id}::{report.trade_date.isoformat()}",
            attributes=attributes,
            note=("旧期历史证据保持未知，不影响当前新覆盖范围。" if outside_current_coverage else "等待执行结果，尚未到核对时间。" if waiting else
                  "对账存在真实差异，等待修复。" if report.verdict == "DIFF_FOUND" else
                  "对账证据尚未齐全，请核查缺失来源后重检。"),
            # MySQL DATETIME(0) rounds fractional seconds up. Truncate before
            # storing so an immediate history query cannot miss a fresh audit
            # because its persisted occurrence appears up to 0.5s in future.
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
            operator_id=operator_id,
            correlation_id=correlation_id,
        )
    else:
        report.status = "PASSED"
        # 单调推进 last_reconciled_trade_date（只前进，不允许回退，跨日安全）
        if not report.coverage:
            _checkpoint_progression(db, p, report)
        attributes = {**context,
            "status": report.status, "verdict": report.verdict,
            "work_state": report.work_state,
            "report_id": report.report_id, "report_version": report.report_version,
            "reason_codes": report.reason_codes, "actions": report.actions,
            "progression": report.progression,
            "coverage_generation": report.coverage["generation"] if report.coverage else None,
            "baseline_id": report.coverage["baseline_id"] if report.coverage else None,
            "trade_date": report.trade_date.isoformat(),
            "decision_run_id": report.decision_run_id,
            "promoted_last_reconciled_trade_date": (
                p.last_reconciled_trade_date.isoformat()
                if p.last_reconciled_trade_date else None
            ),
        }
        report.audit = write_audit_event(
            db, "RECONCILIATION_RESULT",
            portfolio_id=p.id,
            business_key=report.decision_run_id or f"portfolio::{p.id}::{report.trade_date.isoformat()}",
            attributes=attributes,
            note="按已接受账户基线完成本日核对，连续游标仍由当前代范围决定。" if report.coverage else "对账差异清零，last_reconciled_trade_date 单调推进。",
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
            operator_id=operator_id,
            correlation_id=correlation_id,
        )
