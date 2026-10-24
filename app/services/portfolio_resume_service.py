"""FR-P1-2 Portfolio Resume：故障/中断后逐日补算，以 idempotency_key 防重复。

核心逻辑：
1. 找出 next_unprocessed_trade_date：
   - base = 取 max(last_reconciled_trade_date, last_decision_trade_date, effective_start_date-1d, created_at.date()-1d)
   - next = base 的下一交易日（通过 market_calendar 查）。
   - 若 next 超过 today() → 无待补交易日，返回空计划。
2. 每日补算使用 **相同 idempotency_key**（resume|portfolio_id|trade_date|fixed_hash），
   再叠加 async_tasks.idempotency_key 防重复创建任务；
   同时如果 decision_runs 上同 (portfolio_id, trade_date, idempotency_key) 已有成功结果，
   则跳过该日（AC-10：绝不重复下单/重复成交）。
3. 补算任务 payload 中明确写 `correlation_id` 与 `resume_mode=true`，
   使 Outbox/DedupLog 可跨日串联。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.portfolio import Portfolio
from app.models.decision_engine import DecisionRun
from app.models.async_task import AsyncTaskRecord

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_cid() -> str:
    return uuid4().hex[:8]


# ---------------------------------------------------------------------------
# 交易日历辅助（未知时保持未知，禁止工作日近似）
# ---------------------------------------------------------------------------
def _next_trade_date(db: Session, after: date) -> date | None:
    """返回 `after` 之后的第一个交易日。"""
    try:
        from app.models.market_data import MarketCalendar
        stmt = (
            select(MarketCalendar.trade_date)
            .where(MarketCalendar.trade_date > after, MarketCalendar.is_trading_day == 1)
            .order_by(MarketCalendar.trade_date.asc())
            .limit(1)
        )
        row = db.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row
    except Exception:
        logger.warning(
            "portfolio_resume: trade_calendar 查询失败，日期保持未知，不生成近似交易日",
            exc_info=True,
        )
    return None


def get_reconciliation_queue(db: Session, portfolio_id: int, *, today: date | None = None) -> dict[str, Any]:
    """Read report facts separately from the historical execution-resume queue.

    No account baseline is invented from net-value totals or current positions.
    Until baseline/generation tables are deployed the legacy checkpoint is
    explicitly labelled, with unknown coverage exposed to the caller.
    """
    from zoneinfo import ZoneInfo
    from sqlalchemy import inspect
    from app.models.market_data import MarketCalendar
    from app.services.portfolio_reconciliation import latest_reconciliation_results
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    from app.services.portfolio_reconciliation import _active_coverage
    active = _active_coverage(db, portfolio_id)
    if active:
        return _coverage_reconciliation_queue(db, portfolio_id, active, today)
    results = latest_reconciliation_results(db, portfolio_id)
    verified = portfolio.last_reconciled_trade_date
    limitations = ["NO_ACCEPTED_ACCOUNT_BASELINE", "LEGACY_CHECKPOINT_NOT_FULL_HISTORY_PROOF"]
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    calendar = resolve_inspection_calendar(db)
    calendar_status = "KNOWN" if calendar["status"] == "PASSED" else "UNKNOWN"
    start = verified + timedelta(days=1) if verified else min(results, default=today)
    end = min(today, calendar["expected_trade_date"]) if calendar["expected_trade_date"] else None
    calendar_days = [day for day in calendar["trading_dates"] if end and start <= day <= end] if calendar_status == "KNOWN" else []
    if calendar_status == "UNKNOWN":
        limitations.append("CALENDAR_UNKNOWN")
    # Only an existing checkpoint defines a legacy continuation. Without it,
    # expose already observed report days, not invented mandatory history.
    days = sorted(set(results) | (set(calendar_days) if verified else set()))
    items = []
    for day in days:
        attrs = results.get(day, {})
        verdict = attrs.get("verdict") or attrs.get("status") or "NOT_CHECKED"
        history_only = bool(verified and day <= verified and verdict == "NOT_CHECKED")
        if history_only:
            verdict = "HISTORICAL_EVIDENCE_UNKNOWN"
        items.append({"trade_date": day.isoformat(), "status": verdict,
            "history_only": history_only,
            "reason": "旧版核验日期之前的材料没有完整结论，保留历史未知，仅供追溯" if history_only else "；".join(attrs.get("reason_codes") or []) or
                      ("已有核对通过报告" if verdict == "PASSED" else "等待读取本日证据并核对"),
            "work_state": "history_unknown" if history_only else attrs.get("work_state", "queued"),
            "report_id": attrs.get("report_id"), "report_version": attrs.get("report_version"),
            "audit_event_id": attrs.get("audit_event_id")})
    pending = [date.fromisoformat(item["trade_date"]) for item in items
               if item["status"] != "PASSED" and (verified is None or date.fromisoformat(item["trade_date"]) > verified)]
    target = min(pending) if pending else portfolio.last_decision_trade_date
    return {"portfolio_id": portfolio_id, "coverage_start": None,
        "verified_through": verified.isoformat() if verified else None,
        "target_trade_date": target.isoformat() if target else None,
        "calendar_status": calendar_status, "days": items,
        "calendar_version": calendar.get("version"), "calendar_reason": calendar.get("reason"),
        "limitations": limitations, "coverage_source": "legacy_checkpoint" if verified else "unknown",
        **_reconciliation_queue_capabilities(db, portfolio_id)}


def _reconciliation_queue_capabilities(db: Session, portfolio_id: int) -> dict[str, bool]:
    from sqlalchemy import inspect
    from app.models.scheduled_task import ScheduledTask
    inspector = inspect(db.connection())
    tables = set(inspector.get_table_names())
    registered = False
    schedule_columns = {col['name'] for col in inspector.get_columns('scheduled_tasks')} if 'scheduled_tasks' in tables else set()
    if {"task_type", "enabled", "payload_json"} <= schedule_columns:
        for payload_json in db.scalars(select(ScheduledTask.payload_json).where(
                ScheduledTask.task_type == "portfolio_reconciliation", ScheduledTask.enabled == 1)):
            try:
                payload = json.loads(payload_json or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and (payload.get("portfolio_id") is None or payload.get("portfolio_id") == portfolio_id):
                registered = True
                break
    return {"baseline_creation_available": {"portfolio_account_baselines", "portfolio_reconciliation_cursors",
                "portfolio_reconciliation_days", "portfolio_reconciliation_reports"} <= tables,
            "automatic_job_registered": registered}


def _coverage_reconciliation_queue(db: Session, portfolio_id: int, active: dict, today: date) -> dict[str, Any]:
    from app.models.market_data import MarketCalendar
    from app.models.portfolio_reconciliation_coverage import PortfolioReconciliationDay, PortfolioReconciliationReport
    start = active["coverage_start_trade_date"]
    verified = active["verified_through_trade_date"]
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    calendar = resolve_inspection_calendar(db)
    known = calendar["status"] == "PASSED" and calendar.get("version") == active["calendar_version"]
    end = min(today, calendar["expected_trade_date"]) if known and calendar["expected_trade_date"] else None
    calendar_dates = {day for day in calendar["trading_dates"] if end and start <= day <= end} if known else set()
    days = {row.target_trade_date: row for row in db.scalars(select(PortfolioReconciliationDay).where(
        PortfolioReconciliationDay.cursor_id == active["cursor_id"],
        PortfolioReconciliationDay.generation == active["generation"]))}
    dates = sorted(calendar_dates | set(days))
    items = []
    for day in dates:
        record = days.get(day)
        report = db.get(PortfolioReconciliationReport, record.current_report_id) if record and record.current_report_id else None
        checks = json.loads(report.checks_json) if report else {}
        if not isinstance(checks, dict):
            checks = {}
        items.append({"trade_date": day.isoformat(), "status": record.verdict if record else "NOT_CHECKED",
            "reason": "本日已完成当前代核对" if record and record.verdict == "PASSED" else
                      "等待合法执行结果" if record and record.work_state == "waiting_execution" else
                      "本日尚待基于已接受账户基线核对",
            "work_state": record.work_state if record else "queued",
            "report_id": checks.get("source_report_id") or (report.id if report else None),
            "report_version": checks.get("source_report_version") or (str(report.report_version) if report else None),
            "persistent_report_id": report.id if report else None,
            "persistent_report_version": report.report_version if report else None,
            "next_check_at": record.next_check_at.replace(tzinfo=timezone.utc).isoformat() if record and record.next_check_at else checks.get("next_check_at"),
            "audit_event_id": report.audit_event_id if report else None})
    # The oldest open day remains the target even when later reports exist.
    pending = [date.fromisoformat(item["trade_date"]) for item in items
        if item["status"] != "PASSED" and (verified is None or date.fromisoformat(item["trade_date"]) > verified)]
    target = min(pending) if pending else next((day for day in calendar["trading_dates"] if day > verified), None) if verified and known else start if known else None
    return {"portfolio_id": portfolio_id, "coverage_start": start.isoformat(),
        "verified_through": verified.isoformat() if verified else None,
        "target_trade_date": target.isoformat() if target else None,
        "calendar_status": "KNOWN" if known else "UNKNOWN", "days": items,
        "coverage_source": "accepted_account_baseline", "baseline_id": active["baseline_id"],
        "generation": active["generation"], "cursor_row_version": active["row_version"],
        "calendar_version": active["calendar_version"],
        "observed_calendar_version": calendar.get("version"), "calendar_reason": calendar.get("reason"),
        "limitations": (["HISTORICAL_EVIDENCE_REMAINS_UNKNOWN"] if active.get("history_gaps") else []) + ([] if known else ["CALENDAR_UNKNOWN"]),
        **_reconciliation_queue_capabilities(db, portfolio_id)}


def review_resume_execution_date(db: Session, portfolio_id: int, trade_date: date,
                                 *, strategy_snapshot_id: str | None = None, current_read: bool = False) -> dict[str, Any]:
    """Historical gaps are diagnostic. Only a real plan due today can resume.

    A previous signal date is allowed solely for a frozen, unfinished order
    plan whose original execution date is today. No current configuration or
    freshly backfilled bar authorizes creating a missing historical decision.
    """
    from app.services.decision_schedule_gate import as_shanghai, shanghai_now
    from app.models.decision_engine import DecisionOrderPlanRecord, DecisionEvidence
    from app.models.sim_account import SimOrder
    current_time = as_shanghai(shanghai_now())
    today = current_time.date()
    statement = select(DecisionOrderPlanRecord, DecisionRun, DecisionEvidence).join(
        DecisionRun, DecisionRun.id == DecisionOrderPlanRecord.decision_run_id).join(
        DecisionEvidence, DecisionEvidence.id == DecisionOrderPlanRecord.evidence_id).where(
        DecisionOrderPlanRecord.portfolio_id == portfolio_id,
        DecisionOrderPlanRecord.signal_date == trade_date,
        DecisionOrderPlanRecord.execution_date == today,
        DecisionOrderPlanRecord.action.in_(["BUY", "SELL"]),
        DecisionRun.portfolio_id == portfolio_id, DecisionRun.trade_date == trade_date,
        DecisionRun.run_type == "auto_simulation", DecisionRun.status == "SUCCEEDED",
        DecisionEvidence.portfolio_id == portfolio_id, DecisionEvidence.decision_run_id == DecisionRun.id,
        DecisionEvidence.symbol_id == DecisionOrderPlanRecord.symbol_id, DecisionEvidence.action == DecisionOrderPlanRecord.action,
    ).order_by(DecisionOrderPlanRecord.order_plan_id)
    plans = list(db.execute(statement.with_for_update().execution_options(populate_existing=True) if current_read else statement).all()) if trade_date < today else []
    valid = []
    for plan, run, evidence in plans:
        if strategy_snapshot_id and run.strategy_snapshot_id != strategy_snapshot_id:
            continue
        if evidence.execution_at is None or evidence.execution_at.replace(tzinfo=timezone.utc).astimezone(current_time.tzinfo).date() != today:
            continue
        order_statement = select(SimOrder).where(SimOrder.portfolio_id == portfolio_id,
            SimOrder.decision_evidence_id == plan.evidence_id).order_by(SimOrder.id)
        orders = list(db.scalars(order_statement.with_for_update().execution_options(populate_existing=True) if current_read else order_statement))
        if any(order.status not in {"pending", "partial"} or order.symbol_id != plan.symbol_id or
               order.side.lower() != plan.action.lower() or float(order.quantity) != float(plan.target_quantity) for order in orders):
            continue
        if sum(float(order.filled_quantity or 0) for order in orders) < float(plan.target_quantity):
            valid.append({"source_code": "decision_order_plans", "source_record_ref": f"decision_order_plan:{plan.order_plan_id}",
                "decision_run_id": run.id, "strategy_snapshot_id": run.strategy_snapshot_id,
                "execution_date": plan.execution_date.isoformat()})
    if trade_date == today or valid:
        from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
        calendar = resolve_inspection_calendar(db, at=current_time, current_read=current_read)
        if calendar["status"] != "PASSED" or trade_date not in calendar["trading_dates"] or today not in calendar["trading_dates"]:
            return {"enabled": False, "mode": "DIAGNOSTIC_ONLY", "disabled_code": "RESUME_CALENDAR_UNKNOWN",
                "reason": "原业务日或当前执行日缺少可信交易所日历，不能按工作日近似派发", "source_refs": valid,
                "next_action": {"action_id": "inspect_calendar", "label": "核查交易日历来源", "kind": "inspect", "enabled": True,
                                "target": "settings:data-center"}}
        if len({ref["strategy_snapshot_id"] for ref in valid}) > 1:
            return {"enabled": False, "mode": "DIAGNOSTIC_ONLY", "disabled_code": "RESUME_PLAN_SCOPE_AMBIGUOUS",
                "reason": "原日期有多份不同冻结策略的未完成计划，请先核查执行来源与范围", "source_refs": valid}
        return {"enabled": True, "mode": "CURRENT_ORIGINAL_PLAN" if valid else "CURRENT_DECISION", "disabled_code": None,
                "reason": "仅继续原定今日执行且尚未完成的冻结计划，不补造历史决策" if valid else "仅可在现有执行门禁和调度窗口内处理当前业务日",
                "source_refs": valid}
    return {"enabled": False, "mode": "DIAGNOSTIC_ONLY", "disabled_code": "HISTORICAL_EXECUTION_NOT_AUTHORIZED",
        "reason": "历史缺口没有仍在当前执行日有效的冻结计划；不能为消除提示补下历史交易",
        "source_refs": [], "next_action": {"action_id": "inspect_historical_execution", "kind": "inspect",
            "label": "核查历史证据与未执行事实", "enabled": True, "target": "governance:reconciliation",
            "portfolio_id": portfolio_id, "trade_date": trade_date.isoformat()}}


def _is_trade_date(db: Session, d: date) -> bool:
    """判断 `d` 是否为交易日（查交易日历表）。

    查不到或表不可用时一律返回 False —— **不再回退到 `d.weekday() < 5`**：
    那个回退会把国庆这类假期认成交易日，与本模块「逐日补算」的语义直接冲突。
    """
    try:
        from app.models.market_data import MarketCalendar
        stmt = select(MarketCalendar).where(
            MarketCalendar.trade_date == d,
            MarketCalendar.is_trading_day == 1,
        )
        return db.execute(stmt).first() is not None
    except Exception:
        logger.warning(
            "portfolio_resume: trade_calendar 查询失败（_is_trade_date），按非交易日处理",
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# idempotency_key 计算
# ---------------------------------------------------------------------------
def resume_idempotency_key(portfolio_id: int, trade_date: date, resumption_cid: str) -> str:
    """恢复模式下（portfolio + 某交易日）幂等键稳定值，跨 resume 调用不变。

    说明：跨不同 cid 的多次 resume，对同 (portfolio_id, trade_date) 必须给出相同
    idempotency_key，否则 decision_already_succeeded 的 where idempotency_key=X
    会不命中，导致重复下单。因此把 core 的 sha1 作为 suffix（resumption_cid 参数
    仅保留 API 兼容，不再参与哈希，以便旧代码调用不报错）。
    """
    core = f"resume|{int(portfolio_id)}|{trade_date.isoformat()}|v1"
    return f"{core}|{hashlib.sha1(core.encode('utf-8')).hexdigest()[:12]}"


def _decision_dedup_token(portfolio_id: int, trade_date: date, idem_key: str) -> str:
    return f"decision_run|portfolio={portfolio_id}|d={trade_date.isoformat()}|idem={idem_key}"


# ---------------------------------------------------------------------------
# next_unprocessed_trade_date 计算
# ---------------------------------------------------------------------------
def compute_next_unprocessed_trade_date(db: Session, portfolio: Portfolio, *,
                                        today: date | None = None) -> date | None:
    """根据 portfolio 锚点列计算下一个未处理的交易日。

    定义（对齐 project_memory）：
    - 若对账有进展（last_reconciled_trade_date 非空）→ next = reconciliation 下一日
    - 否则若决策有进展（last_decision_trade_date 非空）→ next = 决策完成日下一日
    - 否则 next = effective_start_date 或 created_at.date() 或今天 - 20 天 的下一交易日
    - 若 next > 今天 → 无待处理日（返回 None）。
    """
    today = today or datetime.now(timezone.utc).date()
    # An explicit progress marker is authoritative.  Using ``max`` across
    # these fields lets a newly-created row's ``created_at`` move the resume
    # anchor past an older reconciliation/decision checkpoint, silently
    # skipping valid historical days.  Only use the later fallbacks when no
    # explicit progress marker exists.
    if portfolio.last_reconciled_trade_date is not None:
        base = portfolio.last_reconciled_trade_date
    elif portfolio.last_decision_trade_date is not None:
        base = portfolio.last_decision_trade_date
    elif portfolio.effective_start_date is not None:
        base = portfolio.effective_start_date - timedelta(days=1)
    elif portfolio.created_at is not None:
        # naive vs tz-aware → date()
        ca = portfolio.created_at
        if ca.tzinfo is not None:
            ca = ca.astimezone(timezone.utc).replace(tzinfo=None)
        base = ca.date() - timedelta(days=1)
    else:
        base = today - timedelta(days=20)
    nxt = _next_trade_date(db, base)
    if nxt is None or nxt > today:
        return None
    return nxt


def compute_pending_trade_dates(db: Session, portfolio: Portfolio, *,
                                today: date | None = None,
                                max_days: int = 20) -> list[date]:
    """列出所有待补算的交易日（最多 max_days 个）。用于 UI 展示或 resume 批量执行。"""
    today = today or datetime.now(timezone.utc).date()
    out: list[date] = []
    current = compute_next_unprocessed_trade_date(db, portfolio, today=today)
    while current is not None and len(out) < max_days:
        out.append(current)
        current = _next_trade_date(db, current)
        if current is None or current > today:
            break
    return out


# ---------------------------------------------------------------------------
# 决策/任务去重：同 idempotency_key 不重复
# ---------------------------------------------------------------------------
def decision_already_succeeded(db: Session, portfolio_id: int, trade_date: date,
                               idempotency_key: str) -> DecisionRun | None:
    """若同 portfolio + trade_date + idempotency_key 的 DecisionRun 已成功，返回之。

    （真实生产环境可把 idempotency_key 作为 DecisionRun 唯一约束的一部分；
     这里在 ORM 层做 SELECT 前置防御即可满足 AC-10「绝不重复下单」。）
    """
    stmt = (
        select(DecisionRun)
        .where(
            DecisionRun.portfolio_id == int(portfolio_id),
            DecisionRun.trade_date == trade_date,
            DecisionRun.status == "SUCCEEDED",
            DecisionRun.idempotency_key == idempotency_key,
        )
        .order_by(DecisionRun.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def async_task_already_exists_for_resume(db: Session, portfolio_id: int, trade_date: date,
                                         idempotency_key: str) -> AsyncTaskRecord | None:
    """同 (portfolio_id, trade_date, idempotency_key) 的 async_task 已存在则返回之。

    恢复接口 / portoflio_auto_simulation 扫描都会先查本函数，避免任务重复创建。
    """
    stmt = (
        select(AsyncTaskRecord)
        .where(
            or_(AsyncTaskRecord.idempotency_key == idempotency_key,
                AsyncTaskRecord.idempotency_key.startswith(idempotency_key + "|retry:")),
            AsyncTaskRecord.task_type.in_(("portfolio_backtest", "portfolio_auto_run", "portfolio_resume")),
            AsyncTaskRecord.status.in_(("queued", "running", "done")),
        )
        .order_by(AsyncTaskRecord.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Public：plan_resume & execute_resume_plan（返回结构化结果）
# ---------------------------------------------------------------------------
def plan_portfolio_resume(db: Session, portfolio_id: int, *,
                          today: date | None = None,
                          max_days: int = 20,
                          operator_id: str = "system:resume",
                          ) -> dict[str, Any]:
    """生成组合恢复计划（不实际跑任务，仅返回待补日期与幂等 token 去重结果）。

    返回：
    {
      "portfolio_id": int,
      "resumption_correlation_id": str,
      "today": date.iso,
      "next_unprocessed_trade_date": str | None,
      "pending_days": [{"date": str, "decision_done": bool, "task_exists": bool, "will_skip": bool, "idempotency_key": str}],
      "will_create_tasks": int,
      "will_skip_days": int,
      "max_days": int,
    }
    """
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    cid = _gen_cid()
    days = compute_pending_trade_dates(db, portfolio, today=today, max_days=max_days)
    pending_days: list[dict] = []
    will_create = 0
    will_skip = 0
    today_out = today or datetime.now(timezone.utc).date()
    next_un = compute_next_unprocessed_trade_date(db, portfolio, today=today_out)
    for d in days:
        idem = resume_idempotency_key(portfolio.id, d, cid)
        dec_done = decision_already_succeeded(db, portfolio.id, d, idem) is not None
        task_ex = async_task_already_exists_for_resume(db, portfolio.id, d, idem) is not None
        review = review_resume_execution_date(db, portfolio.id, d)
        will_skip_ = dec_done or task_ex or not review["enabled"]
        if will_skip_:
            will_skip += 1
        else:
            will_create += 1
        pending_days.append({
            "date": d.isoformat(),
            "decision_done": dec_done,
            "task_exists": task_ex,
            "will_skip": will_skip_,
            "idempotency_key": idem,
            "execution_review": review, "executable": review["enabled"],
        })
    return {
        "portfolio_id": portfolio.id,
        "resumption_correlation_id": cid,
        "today": today_out.isoformat(),
        "next_unprocessed_trade_date": next_un.isoformat() if next_un else None,
        "pending_days": pending_days,
        "will_create_tasks": will_create,
        "will_skip_days": will_skip,
        "max_days": max_days,
        "operator_id": operator_id,
        "historical_execution_policy": "current_frozen_plan_only_v1",
    }


def create_resume_tasks_from_plan(db: Session, plan: dict[str, Any], *,
                                  payload_builder: Callable | None = None,
                                  task_type: str = "portfolio_resume",
                                  ) -> list[dict[str, Any]]:
    """Create or reattach the original days, then execute them in date order."""
    if task_type != "portfolio_resume":
        raise ValueError("恢复执行仅支持组合逐日恢复任务")
    from app.services.portfolio_execution_task import (
        _DISPATCH_LOCK, dispatch_execution_tasks, snapshot_for_trade_date, portfolio_execution_in_progress,
    )
    created = []
    portfolio_id = int(plan["portfolio_id"])
    cid = plan["resumption_correlation_id"]
    with _DISPATCH_LOCK:
        if portfolio_execution_in_progress(db, portfolio_id):
            return []
        for item in plan["pending_days"]:
            if item.get("decision_done"):
                continue
            trade_date = date.fromisoformat(item["date"])
            review = review_resume_execution_date(db, portfolio_id, trade_date)
            if not review["enabled"]:
                continue
            idem = item["idempotency_key"]
            task = async_task_already_exists_for_resume(db, portfolio_id, trade_date, idem)
            is_new = task is None
            if task is not None:
                if task.status != "queued":
                    continue
            else:
                previous = db.scalar(select(AsyncTaskRecord).where(
                    AsyncTaskRecord.idempotency_key == idem).limit(1))
                previous_payload = json.loads(previous.payload_json or "{}") if previous else {}
                payload = {
                    "resume_mode": True, "portfolio_id": portfolio_id,
                    "trade_date": trade_date.isoformat(), "resumption_correlation_id": cid,
                    "idempotency_key": idem, "operator_id": plan.get("operator_id", "system:resume"),
                    "phase": "daily_fill_gap",
                    "strategy_snapshot_id": previous_payload.get("strategy_snapshot_id") or
                        (review["source_refs"][0]["strategy_snapshot_id"] if review["source_refs"] else snapshot_for_trade_date(db, portfolio_id, trade_date)),
                }
                if payload_builder is not None:
                    payload.update(payload_builder(portfolio_id, trade_date, idem, cid))
                task = AsyncTaskRecord(id=uuid4().hex, task_type=task_type, status="queued", stage="queued",
                    message="等待按交易日恢复", payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    idempotency_key=idem if previous is None else idem + "|retry:" + uuid4().hex[:8],
                    correlation_id=cid, is_terminal_locked=0)
                db.add(task)
                db.flush()
            created.append({"task_id": task.id, "trade_date": trade_date.isoformat(),
                "idempotency_key": task.idempotency_key, "correlation_id": task.correlation_id,
                "status": task.status, "created": is_new})
        db.commit()
        dispatch_execution_tasks([item["task_id"] for item in created])
    return created


# ---------------------------------------------------------------------------
# 便捷：独立 Session 做一次 plan（供 REST 调用）
# ---------------------------------------------------------------------------
def run_resume_plan_api(portfolio_id: int, *, today: date | None = None, max_days: int = 20,
                        operator_id: str = "system:resume") -> dict[str, Any]:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        return plan_portfolio_resume(db, portfolio_id, today=today, max_days=max_days, operator_id=operator_id)
    finally:
        try:
            db.close()
        except Exception:
            pass


def run_resume_execute_api(portfolio_id: int, *, today: date | None = None, max_days: int = 20,
                           operator_id: str = "system:resume",
                           task_type: str = "portfolio_resume",
                           payload_builder=None,
                           ) -> dict[str, Any]:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        plan = plan_portfolio_resume(db, portfolio_id, today=today, max_days=max_days, operator_id=operator_id)
        created = create_resume_tasks_from_plan(db, plan, payload_builder=payload_builder, task_type=task_type)
        plan["created_tasks"] = created
        return plan
    finally:
        try:
            db.close()
        except Exception:
            pass
