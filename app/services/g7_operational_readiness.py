"""Read-only G7 operational status for controlled rollout operations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.alert import AlertEvent
from app.models.portfolio import Portfolio
from app.models.sim_account import SimOrder
from app.services.g6_graduated_rollout import evaluate_g6_readiness
from app.services.portfolio_state_machine import _get_status


PENDING_ORDER_STATUSES = frozenset({"partial", "pending_confirmation"})
PENDING_REVIEW_STATUSES = frozenset({"PENDING_REVIEW"})
NEW_BUY_STOPPED_STATES = frozenset({
    "ADMIN_PAUSED",
    "RECONCILIATION_BLOCKED",
    "DATA_INCOMPLETE_PAUSED",
})


@dataclass(frozen=True)
class G7OperationalStatus:
    """Facts an operator needs before expanding, pausing, or resuming a rollout."""

    portfolio_id: int
    current_state: str
    g6_eligible: bool
    g5_report_id: int | None
    can_stop_new_buys: bool
    new_buys_stopped: bool
    pending_order_count: int
    pending_orders_by_status: dict[str, int]
    active_alert_count: int
    ready_for_expansion: bool
    can_resume: bool
    operational_blockers: list[str] = field(default_factory=list)
    resume_blockers: list[str] = field(default_factory=list)
    # P1：结构化阻断项（与行动项契约对齐），供待办总览/修复抽屉消费；
    # 原 operational_blockers 文本保留，供旧消费方与 G7 面板兼容。
    structured_blockers: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _pending_order_counts(db: Session, *, portfolio_id: int) -> dict[str, int]:
    rows = db.execute(
        select(SimOrder.status, SimOrder.review_status, func.count(SimOrder.id))
        .where(
            SimOrder.portfolio_id == int(portfolio_id),
            or_(
                SimOrder.status.in_(PENDING_ORDER_STATUSES),
                SimOrder.review_status.in_(PENDING_REVIEW_STATUSES),
            ),
        )
        .group_by(SimOrder.status, SimOrder.review_status)
    ).all()
    counts: dict[str, int] = {}
    for order_status, review_status, count in rows:
        label = (
            str(order_status)
            if order_status in PENDING_ORDER_STATUSES
            else str(review_status).lower()
        )
        counts[label] = counts.get(label, 0) + int(count)
    return counts


def evaluate_g7_operational_status(
    db: Session,
    *,
    portfolio_id: int,
) -> G7OperationalStatus:
    """Aggregate durable rollout facts without changing portfolio state.

    ``new_buys_stopped`` reports the states enforced by the order-entry path.
    It deliberately does not claim that a missing G5 report stopped an already
    enabled legacy source. That report instead makes ``ready_for_expansion``
    false, which is the safer and auditable G7 admission signal.
    """
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    g6 = evaluate_g6_readiness(db, portfolio_id=portfolio.id)
    current_state = _get_status(db, portfolio)
    pending_by_status = _pending_order_counts(db, portfolio_id=portfolio.id)
    pending_count = sum(pending_by_status.values())
    active_alert_count = int(db.execute(
        select(func.count(AlertEvent.id)).where(
            AlertEvent.portfolio_id == portfolio.id,
            AlertEvent.acknowledged == 0,
            AlertEvent.status == "ACTIVE",
        )
    ).scalar_one())

    operational_blockers = list(g6.blockers)
    if pending_count:
        operational_blockers.append(f"存在 {pending_count} 笔待处理模拟订单，必须先完成处置")
    if active_alert_count:
        operational_blockers.append(f"存在 {active_alert_count} 条未确认告警，必须先完成处置")

    # Use the same composite order-entry gate as auto-simulation. The legacy
    # ``portfolio_status`` column alone does not represent SCORE_STALE or
    # MODEL_INACTIVE dimension blocks.
    try:
        from app.services.portfolio_status import order_entry_gate_check

        buy_gate = order_entry_gate_check(db, portfolio.id, "BUY")
        new_buys_stopped = (
            current_state in NEW_BUY_STOPPED_STATES or not buy_gate.allowed
        )
    except Exception:
        # A status dashboard must remain readable on an old pre-migration DB;
        # fall back to the durable state-machine stop list in that case.
        new_buys_stopped = current_state in NEW_BUY_STOPPED_STATES
    ready_for_expansion = (
        g6.eligible
        and current_state == "READY"
        and not new_buys_stopped
        and pending_count == 0
        and active_alert_count == 0
    )
    # Resuming a manually paused portfolio is a separate control from G6
    # expansion. Missing G5 evidence blocks expansion, but must not prevent an
    # operator from releasing ADMIN_PAUSED after pending work and alerts are
    # cleared.
    resume_blockers: list[str] = []
    if pending_count:
        resume_blockers.append(f"存在 {pending_count} 笔待处理模拟订单")
    if active_alert_count:
        resume_blockers.append(f"存在 {active_alert_count} 条未确认告警")
    if current_state != "ADMIN_PAUSED":
        resume_blockers.append("当前组合不处于 ADMIN_PAUSED，无需或不能通过人工暂停恢复")
    can_resume = not resume_blockers

    # P1：把运维阻断结构化为行动项。G7 是上线前灰度/运维视角，统一为 info 级，
    # 不与交易门禁（HG1/readiness 的 blocker）混淆；当前前端无稳定直达入口，动作暂为 none。
    structured_blockers: list[dict] = []
    if g6.blockers:
        structured_blockers.append(
            {
                "code": "G6_GATE_NOT_READY",
                "severity": "info",
                "title": "灰度准入条件未满足",
                "impact": "灰度准入尚有未清条件，日常交易是否可执行以交易门禁为准",
                "detail": "；".join(str(b) for b in g6.blockers),
                "fix_link": {"action_type": "none"},
            }
        )
    if pending_count:
        structured_blockers.append(
            {
                "code": "PENDING_SIM_ORDERS",
                "severity": "info",
                "title": "有待处理模拟订单",
                "impact": f"存在 {pending_count} 笔待处理模拟订单，扩大灰度前需先处置",
                "detail": f"pending_order_count={pending_count}",
                "fix_link": {"action_type": "none"},
            }
        )
    if active_alert_count:
        structured_blockers.append(
            {
                "code": "ACTIVE_ALERTS",
                "severity": "info",
                "title": "有未确认告警",
                "impact": f"存在 {active_alert_count} 条未确认告警，扩大灰度前需先确认",
                "detail": f"active_alert_count={active_alert_count}",
                "fix_link": {"action_type": "none"},
            }
        )

    return G7OperationalStatus(
        portfolio_id=portfolio.id,
        current_state=current_state,
        g6_eligible=g6.eligible,
        g5_report_id=g6.g5_report_id,
        can_stop_new_buys=not new_buys_stopped,
        new_buys_stopped=new_buys_stopped,
        pending_order_count=pending_count,
        pending_orders_by_status=pending_by_status,
        active_alert_count=active_alert_count,
        ready_for_expansion=ready_for_expansion,
        can_resume=can_resume,
        operational_blockers=operational_blockers,
        resume_blockers=resume_blockers,
        structured_blockers=structured_blockers,
    )
