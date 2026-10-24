"""Fail-closed admission checks for a single-portfolio G6 rollout."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import (
    AUTO_TRADE_SOURCE_LEGACY_SCAN,
    AUTO_TRADE_SOURCE_MEMBERS_ONLY,
    Portfolio,
)
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event
from app.services.g5_dual_run_audit import latest_g5_summary
from app.services.g5_admission_governance import get_g5_admission_state, assert_g5_admission_open_for_update
from app.services.portfolio_state_machine import _get_status


@dataclass(frozen=True)
class G6Readiness:
    portfolio_id: int
    eligible: bool
    current_state: str
    g5_report_id: int | None = None
    blockers: list[str] = field(default_factory=list)
    admission_control: dict = field(default_factory=dict)
    g5_report_hash: str | None = None
    current_source_mode: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class G6RolloutResult:
    portfolio_id: int
    status: str
    from_source_mode: str
    to_source_mode: str
    operator_id: str
    correlation_id: str | None
    g5_report_id: int | None = None
    audit_event_id: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_g6_readiness(db: Session, *, portfolio_id: int, current_read: bool = False) -> G6Readiness:
    """Check the non-negotiable prerequisites before a G6 rollout.

    G6 does not turn on a feature or submit an order. It only provides a
    deterministic, auditable decision that the operator can review before
    starting a single-portfolio rebalance cycle.
    """
    portfolio = db.get(Portfolio, int(portfolio_id), with_for_update=current_read, populate_existing=current_read)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    blockers: list[str] = []
    control = get_g5_admission_state(db, portfolio_id, current_read=current_read)
    if control["frozen"]:
        blockers.append("G5_NEW_ADMISSION_FROZEN：" + "；".join(control["reasons"]))
    status = _get_status(db, portfolio)
    if status != "READY":
        blockers.append(f"组合状态为 {status}，G6 仅允许 READY")
    from app.services.portfolio_reconciliation import _active_coverage
    try:
        active = _active_coverage(db, portfolio.id, current_read=current_read)
        if active:
            from app.services.portfolio_reconciliation_activation import get_initial_coverage_activation
            if not get_initial_coverage_activation(db, portfolio.id, active=active, current_read=current_read):
                blockers.append("当前账户覆盖范围尚未完成本人首次执行审核")
            target = portfolio.last_decision_trade_date
            if target and target >= active["coverage_start_trade_date"] and (
                active["verified_through_trade_date"] is None or active["verified_through_trade_date"] < target):
                blockers.append("当前代仍有未完成对账的决策日，必须先完成连续核对")
        elif portfolio.last_decision_trade_date and (
            portfolio.last_reconciled_trade_date is None or portfolio.last_reconciled_trade_date < portfolio.last_decision_trade_date):
            blockers.append("存在未完成对账的决策日，必须先完成每日守恒对账")
    except ValueError:
        blockers.append("当前账户基线或连续覆盖来源不可核验，不能新增准入")

    summary = (latest_g5_summary(db, portfolio_id=portfolio.id, current_read=True) if current_read else
               latest_g5_summary(db, portfolio_id=portfolio.id))
    report_id = int(summary["audit_report_id"]) if summary and summary.get("audit_report_id") else None
    if summary is None:
        blockers.append("缺少已归档 G5 双跑报告")
    elif not summary.get("evidence_verified"):
        blockers.append("对比报告缺少可核验的真实执行证据：" + "；".join(summary.get("g5_validation_errors") or []))
    elif not bool(summary.get("g5_eligible_for_g6")):
        reasons = summary.get("acceptance_blockers") or []
        blockers.append("最新 G5 双跑报告未达到 G6 准入条件" + ("：" + "；".join(reasons) if reasons else ""))
    if summary and summary.get("evidence_verified") and portfolio.auto_trade_source_mode != AUTO_TRADE_SOURCE_MEMBERS_ONLY:
        scope = summary.get("applicable_scope") or {}
        if scope.get("from_source_mode") != portfolio.auto_trade_source_mode:
            blockers.append("G5_SOURCE_SCOPE_CHANGED：报告起始来源与当前组合来源不同，需重新验收")

    return G6Readiness(
        portfolio_id=portfolio.id,
        eligible=not blockers,
        current_state=status,
        g5_report_id=report_id,
        blockers=blockers,
        admission_control=control,
        g5_report_hash=summary.get("audit_report_hash") if summary else None,
        current_source_mode=portfolio.auto_trade_source_mode,
    )


def _latest_rollout_audit(db: Session, *, portfolio_id: int, action: str):
    return db.execute(
        select(DataGovernanceAuditEvent)
        .where(
            DataGovernanceAuditEvent.portfolio_id == int(portfolio_id),
            DataGovernanceAuditEvent.action == action,
        )
        .order_by(DataGovernanceAuditEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def start_g6_rollout(
    db: Session,
    *,
    portfolio_id: int,
    operator_id: str = "system",
    correlation_id: str | None = None,
    expected_g5_report_id: int | None = None,
    expected_g5_report_hash: str | None = None,
    expected_source_mode: str | None = None,
    review_note: str | None = None,
) -> G6RolloutResult:
    """Atomically enable the new member source for one portfolio.

    The function only flushes.  The API/worker owns commit/rollback, so the
    portfolio update and its audit event share one transaction on SQLite and
    MySQL alike.
    """
    assert_g5_admission_open_for_update(db, portfolio_id)
    portfolio = db.get(Portfolio, int(portfolio_id), with_for_update=True, populate_existing=True)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError("G6 rollout only supports simulated portfolios")
    readiness = evaluate_g6_readiness(db, portfolio_id=portfolio.id, current_read=True)
    if not readiness.eligible:
        raise ValueError("G6 rollout blocked: " + "; ".join(readiness.blockers))

    current = str(getattr(portfolio, "auto_trade_source_mode", "portfolio") or "portfolio")
    if (expected_g5_report_id != readiness.g5_report_id or expected_g5_report_hash != readiness.g5_report_hash
        or expected_source_mode != current):
        raise ValueError("G5_ADMISSION_SNAPSHOT_CHANGED：报告或当前来源已变化，请重新查看切换影响后审核")
    if (not review_note or len(review_note.strip()) < 10 or not operator_id
        or operator_id in {"system", "cron_worker"} or operator_id.startswith("system:")):
        raise ValueError("G5_PERSONAL_SWITCH_REVIEW_REQUIRED：切换需本人填写至少10个字的独立审核理由")
    if current == AUTO_TRADE_SOURCE_MEMBERS_ONLY:
        return G6RolloutResult(
            portfolio_id=portfolio.id,
            status="NOOP",
            from_source_mode=current,
            to_source_mode=current,
            operator_id=str(operator_id or "system"),
            correlation_id=correlation_id,
            g5_report_id=readiness.g5_report_id,
            reason="G6 已启动，重复请求幂等返回",
        )

    before = {"auto_trade_source_mode": current, "g5_report_id": readiness.g5_report_id}
    portfolio.auto_trade_source_mode = AUTO_TRADE_SOURCE_MEMBERS_ONLY
    after = {"auto_trade_source_mode": AUTO_TRADE_SOURCE_MEMBERS_ONLY, "g5_report_id": readiness.g5_report_id}
    audit = write_audit_event(
        db,
        "G6_ROLLOUT_STARTED",
        portfolio_id=portfolio.id,
        business_key=f"g6:{portfolio.id}:start",
        operator_id=operator_id,
        correlation_id=correlation_id,
        before=before,
        after=after,
        attributes={"g5_report_id": readiness.g5_report_id, "g5_report_hash": readiness.g5_report_hash,
                    "review_note": review_note.strip(), "ack": True, "from_source_mode": current,
                    "to_source_mode": AUTO_TRADE_SOURCE_MEMBERS_ONLY},
        note=review_note.strip(),
    )
    return G6RolloutResult(
        portfolio_id=portfolio.id,
        status="STARTED",
        from_source_mode=current,
        to_source_mode=AUTO_TRADE_SOURCE_MEMBERS_ONLY,
        operator_id=str(operator_id or "system"),
        correlation_id=correlation_id,
        g5_report_id=readiness.g5_report_id,
        audit_event_id=audit.event_id,
    )


def rollback_g6_rollout(
    db: Session,
    *,
    portfolio_id: int,
    operator_id: str = "system",
    correlation_id: str | None = None,
    reason: str | None = None,
) -> G6RolloutResult:
    """Atomically return one portfolio to the legacy scan source."""
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError("G6 rollout only supports simulated portfolios")

    current = str(getattr(portfolio, "auto_trade_source_mode", "portfolio") or "portfolio")
    if current == AUTO_TRADE_SOURCE_LEGACY_SCAN:
        return G6RolloutResult(
            portfolio_id=portfolio.id,
            status="NOOP",
            from_source_mode=current,
            to_source_mode=current,
            operator_id=str(operator_id or "system"),
            correlation_id=correlation_id,
            reason="已处于旧来源，重复回滚幂等返回",
        )

    before = {"auto_trade_source_mode": current}
    portfolio.auto_trade_source_mode = AUTO_TRADE_SOURCE_LEGACY_SCAN
    after = {"auto_trade_source_mode": AUTO_TRADE_SOURCE_LEGACY_SCAN}
    audit = write_audit_event(
        db,
        "G6_ROLLOUT_ROLLED_BACK",
        portfolio_id=portfolio.id,
        business_key=f"g6:{portfolio.id}:rollback",
        operator_id=operator_id,
        correlation_id=correlation_id,
        before=before,
        after=after,
        attributes={"reason": reason or "operator_requested"},
        note=reason or "单组合 G6 灰度回滚",
    )
    # Preserve the existing emergency env kill switch for compatibility.  The
    # persisted portfolio mode remains the source of truth for future calls.
    from app.services.auto_trade_dual_run import rollback_to_old_source
    rollback_to_old_source(portfolio.id)
    return G6RolloutResult(
        portfolio_id=portfolio.id,
        status="ROLLED_BACK",
        from_source_mode=current,
        to_source_mode=AUTO_TRADE_SOURCE_LEGACY_SCAN,
        operator_id=str(operator_id or "system"),
        correlation_id=correlation_id,
        audit_event_id=audit.event_id,
        reason=reason or "operator_requested",
    )
