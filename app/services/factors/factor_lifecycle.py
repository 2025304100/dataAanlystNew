"""WP1-05: 因子生命周期状态机服务。

实现合法迁移、硬门禁、actor/reason、409 冲突。
对齐 docs/专业因子库开发计划.md §WP1-05 和 docs/因子设置与专业因子库改造方案.md §5。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor import Factor
from app.models.factor_evaluation import TransitionAudit
from app.schemas.factor_library import FactorTransitionRequest


# --- State machine definition ---

# WP1 第一版支持的迁移 + WP6 扩展（shadow/active/quarantine）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"candidate", "rejected", "deprecated"},
    "candidate": {"testing", "rejected", "deprecated", "draft"},
    "testing": {"shadow", "rejected", "deprecated", "candidate"},  # WP6: testing → shadow
    "rejected": {"draft"},  # only via creating new version
    "shadow": {"active", "quarantined", "deprecated", "testing", "rejected"},  # WP6: shadow → active/reject
    "active": {"quarantined", "deprecated"},  # WP6: active → quarantine/deprecate
    "quarantined": {"shadow", "deprecated"},  # WP6: quarantined → shadow (recovery) or deprecate
    "deprecated": set(),  # terminal
}

# Action to target status mapping
ACTION_TO_STATUS: dict[str, str] = {
    "submit_candidate": "candidate",
    "start_testing": "testing",
    "start_shadow": "shadow",  # WP6: testing → shadow
    "activate": "active",  # WP6: shadow → active (人工审批)
    "quarantine": "quarantined",  # WP6: 数据硬错误自动隔离
    "recover_shadow": "shadow",  # WP6: quarantined → shadow (恢复观察)
    "reject": "rejected",
    "revoke_to_draft": "draft",
    "deprecate": "deprecated",
}

# Action prerequisites (current status must be in this set)
ACTION_PREREQUISITES: dict[str, set[str]] = {
    "submit_candidate": {"draft"},
    "start_testing": {"candidate"},
    "start_shadow": {"testing"},  # WP6: 只有 testing 可进入 shadow
    "activate": {"shadow"},  # WP6: 只有 shadow 可激活（需人工审批 + 20 有效交易日）
    "quarantine": {"shadow", "active"},  # WP6: shadow 或 active 可隔离
    "recover_shadow": {"quarantined"},  # WP6: 隔离后可恢复观察
    "reject": {"draft", "candidate", "testing", "shadow"},
    "revoke_to_draft": {"rejected"},
    "deprecate": {
        "draft", "candidate", "testing", "rejected",
        "active", "shadow", "quarantined",
    },
}


@dataclass(frozen=True)
class TransitionResult:
    """状态迁移结果。"""

    factor_id: int
    from_status: str | None
    to_status: str
    action: str
    actor: str
    reason: str | None
    audit_id: int
    success: bool
    error: str | None = None


# --- 内部工具 ---


def _infer_current_status(factor: Factor) -> str:
    """推断因子的当前生命周期状态。

    旧因子可能 lifecycle_status 为 None：
    - origin == "system" → 视为 active
    - 其他 → 视为 draft
    """
    if factor.lifecycle_status is not None:
        return factor.lifecycle_status
    if factor.origin == "system":
        return "active"
    return "draft"


# --- 公共 API ---


def validate_transition(
    *,
    current_status: str | None,
    action: str,
) -> tuple[bool, str | None]:
    """验证迁移是否合法（不执行）。

    返回 (is_valid, error_message)。
    error_message 为 None 表示合法；否则为错误码字符串：
    - "invalid_action": action 不在 ACTION_TO_STATUS
    - "current_status_unknown": current_status 为 None（需要 origin 推断）
    - "transition_forbidden": 前置状态或目标迁移不合法
    """
    if action not in ACTION_TO_STATUS:
        return False, "invalid_action"

    if current_status is None:
        # validate_transition 不接触 DB，无法基于 origin 推断
        return False, "current_status_unknown"

    target_status = ACTION_TO_STATUS[action]

    prereqs = ACTION_PREREQUISITES.get(action, set())
    if current_status not in prereqs:
        return False, "transition_forbidden"

    allowed = ALLOWED_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        return False, "transition_forbidden"

    return True, None


def execute_transition(
    db: Session,
    *,
    factor_id: int,
    request: FactorTransitionRequest,
) -> TransitionResult:
    """执行因子状态迁移。

    流程：
    1. 获取因子（with_for_update 行锁）
    2. 验证 action 合法
    3. 验证当前状态满足 action 前置条件
    4. 验证目标状态在允许的迁移集合中
    5. 幂等检查（request_id）
    6. 更新 lifecycle_status
    7. 单向同步 status（legacy compat）
    8. 追加 TransitionAudit
    9. commit
    """
    # 1. 获取因子（行锁）
    factor = db.execute(
        select(Factor).where(Factor.id == factor_id).with_for_update()
    ).scalar_one_or_none()

    if factor is None:
        raise ValueError("factor_not_found")

    action = request.action

    # 2. 验证 action 合法
    if action not in ACTION_TO_STATUS:
        raise ValueError("invalid_action")

    target_status = ACTION_TO_STATUS[action]

    # 3. 推断当前状态（处理 legacy None）
    raw_status = factor.lifecycle_status
    current_status = _infer_current_status(factor)

    # 4. 前置状态检查
    prereqs = ACTION_PREREQUISITES.get(action, set())
    if current_status not in prereqs:
        raise ValueError("transition_forbidden")

    # 5. 允许的目标迁移检查
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise ValueError("transition_forbidden")

    # 6. 幂等检查（request_id）
    if request.request_id:
        existing_audit = db.execute(
            select(TransitionAudit)
            .where(TransitionAudit.request_id == request.request_id)
            .limit(1)
        ).scalar_one_or_none()
        if existing_audit is not None:
            return TransitionResult(
                factor_id=factor_id,
                from_status=existing_audit.from_status,
                to_status=existing_audit.to_status,
                action=action,
                actor=existing_audit.actor,
                reason=existing_audit.reason,
                audit_id=existing_audit.id,
                success=True,
                error=None,
            )

    # 7. 更新 lifecycle_status
    factor.lifecycle_status = target_status

    # 8. 单向同步 legacy 字段
    if target_status == "active":
        factor.status = "active"
        factor.is_active = 1
    elif target_status == "deprecated":
        factor.is_active = 0

    # 9. 追加 TransitionAudit
    audit = TransitionAudit(
        factor_id=factor_id,
        factor_version_id=None,
        from_status=raw_status,
        to_status=target_status,
        actor=request.actor,
        reason=request.reason,
        evidence_run_id=request.evidence_run_id,
        request_id=request.request_id,
        migration_note=None,
    )
    db.add(audit)
    db.flush()  # 填充 audit.id

    audit_id = audit.id

    # 10. commit
    db.commit()

    return TransitionResult(
        factor_id=factor_id,
        from_status=raw_status,
        to_status=target_status,
        action=action,
        actor=request.actor,
        reason=request.reason,
        audit_id=audit_id,
        success=True,
        error=None,
    )


def get_transition_history(
    db: Session,
    *,
    factor_id: int,
    limit: int = 50,
) -> list[TransitionAudit]:
    """获取因子状态迁移历史（按时间倒序）。"""
    return list(
        db.execute(
            select(TransitionAudit)
            .where(TransitionAudit.factor_id == factor_id)
            .order_by(TransitionAudit.created_at.desc())
            .limit(limit)
        ).scalars().all()
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ACTION_TO_STATUS",
    "ACTION_PREREQUISITES",
    "TransitionResult",
    "validate_transition",
    "execute_transition",
    "get_transition_history",
]
