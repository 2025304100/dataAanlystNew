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
from app.services.portfolio_state_machine import _get_status


@dataclass(frozen=True)
class G6Readiness:
    portfolio_id: int
    eligible: bool
    current_state: str
    g5_report_id: int | None = None
    blockers: list[str] = field(default_factory=list)

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


def evaluate_g6_readiness(db: Session, *, portfolio_id: int) -> G6Readiness:
    """Check the non-negotiable prerequisites before a G6 rollout.

    G6 does not turn on a feature or submit an order. It only provides a
    deterministic, auditable decision that the operator can review before
    starting a single-portfolio rebalance cycle.
    """
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    blockers: list[str] = []
    status = _get_status(db, portfolio)
    if status != "READY":
        blockers.append(f"组合状态为 {status}，G6 仅允许 READY")
    if portfolio.last_decision_trade_date and (
        portfolio.last_reconciled_trade_date is None
        or portfolio.last_reconciled_trade_date < portfolio.last_decision_trade_date
    ):
        blockers.append("存在未完成对账的决策日，必须先完成每日守恒对账")

    summary = latest_g5_summary(db, portfolio_id=portfolio.id)
    report_id = int(summary["audit_report_id"]) if summary and summary.get("audit_report_id") else None
    if summary is None:
        blockers.append("缺少已归档 G5 双跑报告")
    elif not bool(summary.get("g5_eligible_for_g6")):
        blockers.append("最新 G5 双跑报告未达到 G6 准入条件")

    return G6Readiness(
        portfolio_id=portfolio.id,
        eligible=not blockers,
        current_state=status,
        g5_report_id=report_id,
        blockers=blockers,
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
) -> G6RolloutResult:
    """Atomically enable the new member source for one portfolio.

    The function only flushes.  The API/worker owns commit/rollback, so the
    portfolio update and its audit event share one transaction on SQLite and
    MySQL alike.
    """
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError("G6 rollout only supports simulated portfolios")
    readiness = evaluate_g6_readiness(db, portfolio_id=portfolio.id)
    if not readiness.eligible:
        raise ValueError("G6 rollout blocked: " + "; ".join(readiness.blockers))

    current = str(getattr(portfolio, "auto_trade_source_mode", "portfolio") or "portfolio")
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
        attributes={"g5_report_id": readiness.g5_report_id},
        note="单组合 G6 灰度启动",
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
