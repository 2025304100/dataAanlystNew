"""Read-only review of the exact applied binding a portfolio will accept.

Acceptance history lives in the existing governance audit table. A saved
binding is not evidence that the user previously accepted it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio, PortfolioRule
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.factors.__facade__ import review_score_model_binding
from app.services.portfolio_state_machine import _get_status


BINDING_CONFIRMATION_TYPE = "MODEL_BINDING_CONFIRMED"


def _object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _last_accepted_binding(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict[str, Any] | None:
    statement = (
        select(DataGovernanceAuditEvent)
        .where(
            DataGovernanceAuditEvent.portfolio_id == portfolio_id,
            DataGovernanceAuditEvent.action == "FACTOR_USAGE_APPLIED",
            DataGovernanceAuditEvent.attributes_json.contains(
                '"confirmation_type":"MODEL_BINDING_CONFIRMED"', autoescape=True,
            ),
        )
        .order_by(DataGovernanceAuditEvent.occurred_at.desc(), DataGovernanceAuditEvent.id.desc())
        .limit(1)
    )
    if current_read:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    events = db.execute(statement).scalars()
    for event in events:
        if _object(event.attributes_json).get("confirmation_type") == BINDING_CONFIRMATION_TYPE:
            accepted = _object(event.after_json).get("accepted_binding")
            if isinstance(accepted, dict) and accepted.get("snapshot_id"):
                return accepted
    return None


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def build_binding_review(db: Session, portfolio: Portfolio, *, current_read: bool = False) -> dict[str, Any]:
    """Describe the currently applied binding without flushing or mutating DB.

    Preflight and task-locked snapshots never count as user-applied strategy.
    The latest applied snapshot must still reference the active, effective
    usage and the validated global active model's frozen FactorSet.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    blockers: list[str] = []
    with db.no_autoflush:
        from app.services.auto_trade_dual_run import resolve_applied_snapshot_id
        if current_read:
            # Match the execution resolver's order, with a current read so a
            # prior RR snapshot cannot hide a concurrent save/apply commit.
            snapshot_id = db.execute(
                select(StrategyExecutionSnapshot.id).where(
                    StrategyExecutionSnapshot.portfolio_id == portfolio.id,
                    StrategyExecutionSnapshot.snapshot_type == "save_and_apply",
                ).order_by(StrategyExecutionSnapshot.snapshot_no.desc(), StrategyExecutionSnapshot.created_at.desc())
                .limit(1).with_for_update()
            ).scalar_one_or_none()
        else:
            snapshot_id = resolve_applied_snapshot_id(db, portfolio_id=portfolio.id)
        snapshot = db.get(StrategyExecutionSnapshot, snapshot_id, with_for_update=current_read, populate_existing=current_read) if snapshot_id else None
        before = _last_accepted_binding(db, portfolio.id, current_read=current_read)
        after: dict[str, Any] | None = None
        if snapshot is None:
            blockers.append("策略尚未保存应用，请先生成可执行快照")
        else:
            usage = db.get(PortfolioFactorUsage, snapshot.portfolio_factor_usage_id, with_for_update=current_read, populate_existing=current_read) if snapshot.portfolio_factor_usage_id else None
            after = {
                "snapshot_id": snapshot.id,
                "model_run_id": snapshot.factor_model_run_id,
                "model_name": None,
                "factor_set_id": snapshot.factor_set_id,
                "rule_id": snapshot.rule_id,
                "rule_version": snapshot.rule_version,
                "snapshot_no": snapshot.snapshot_no,
                "run_mode": usage.run_mode if usage else None,
                "pit_mode": usage.pit_mode if usage else None,
            }
            if snapshot.effective_from is None or _utc_naive(snapshot.effective_from) > now:
                blockers.append("策略快照尚未生效")
            if not usage or usage.portfolio_id != portfolio.id:
                blockers.append("策略快照缺少本组合的有效模型绑定")
            elif (
                usage.status != "active"
                or usage.effective_from is None
                or _utc_naive(usage.effective_from) > now
                or (usage.effective_to is not None and _utc_naive(usage.effective_to) <= now)
            ):
                blockers.append("策略快照关联的模型绑定已过期或尚未生效，请重新保存应用")
            else:
                if (
                    usage.factor_model_run_id != snapshot.factor_model_run_id
                    or usage.factor_set_id != snapshot.factor_set_id
                    or usage.rule_id != snapshot.rule_id
                    or usage.rule_version != snapshot.rule_version
                ):
                    blockers.append("策略快照与当前模型绑定不一致，请重新保存应用")
                if usage.run_mode not in {"production_pit", "production_sim"}:
                    blockers.append("当前策略为研究模式，请保存应用正式执行策略")
            if not snapshot.factor_model_run_id or not snapshot.factor_set_id:
                blockers.append("策略快照缺少模型或因子集绑定")
            else:
                readiness = review_score_model_binding(db, snapshot.factor_model_run_id, snapshot.factor_set_id, current_read=current_read)
                after["model_name"] = readiness["model_name"]
                if not readiness["ready"]:
                    messages = {
                        "MODEL_NOT_FOUND": "绑定模型已不存在，请重新选择模型并保存应用",
                        "MODEL_RETIRED": "绑定模型已退役，请选择有效模型并保存应用",
                        "MODEL_NOT_VALIDATED": "绑定模型尚未通过验证",
                        "MODEL_NOT_GLOBAL_ACTIVE": "绑定模型已不是当前活动模型，请重新保存应用",
                        "FACTOR_SET_NOT_READY": "绑定的因子集尚未就绪",
                    }
                    blockers.append(messages.get(readiness["code"], "模型与冻结因子集不一致，请重新保存应用"))
            if snapshot.rule_id is not None:
                rule = db.get(PortfolioRule, snapshot.rule_id, with_for_update=current_read, populate_existing=current_read)
                if not rule or rule.portfolio_id != portfolio.id or not rule.is_active:
                    blockers.append("快照关联的策略已失效，请重新保存应用")

        requires_confirmation = (
            getattr(portfolio.status_model, "value", portfolio.status_model) == "MODEL_INACTIVE"
            or _get_status(db, portfolio, current_read=current_read) == "MODEL_INACTIVE"
            or bool(before and after and before.get("snapshot_id") != after["snapshot_id"])
        )
        if before is None:
            change_summary = "首次确认此组合的模型与策略快照；没有历史已接受绑定记录"
        elif after is None:
            change_summary = "当前没有可接受的策略快照"
        else:
            changed: list[str] = []
            for key, label in (
                ("model_run_id", "模型"), ("factor_set_id", "因子集"),
                ("rule_id", "策略"), ("rule_version", "策略版本"),
                ("run_mode", "执行模式"), ("pit_mode", "数据时点模式"),
                ("snapshot_id", "策略快照"),
            ):
                if before.get(key) != after.get(key):
                    changed.append(label)
            change_summary = "请确认接受新绑定：" + "、".join(changed) if changed else "绑定内容未变化，请确认恢复本组合的模型接管"

    return {
        "portfolio_id": portfolio.id,
        "snapshot_id": after["snapshot_id"] if after else None,
        "model_run_id": after["model_run_id"] if after else None,
        "model_name": after["model_name"] if after else None,
        "change_summary": change_summary,
        "before": before,
        "after": after,
        "requires_confirmation": requires_confirmation,
        "first_confirmation": before is None,
        "can_confirm": requires_confirmation and not blockers,
        "blockers": list(dict.fromkeys(blockers)),
    }
