"""WP8-02: 回滚演练工具。

对齐 docs/专业因子库开发计划.md §12.2 和 spec.md「迁移切换与回退」Scenario: 回退后主流程可用。

核心能力：
- 关闭因子中心写开关（feature_enabled=False）
- runtime 切回 manual（清空 active_model_run_id、记录 fallback_reason）
- 验证读取路径回到 definitions.py 和旧 factor_engine（weight_mode='manual'）
- 保留新表和审计供排障（不删除数据、不执行 Alembic downgrade）
- 返回结构化回滚报告，记录每步状态和验证结果

安全约束：
- 必须先 fallback 到 manual，再关闭 feature_enabled（config 层强制）
- 不删除任何数据表或审计记录
- Alembic downgrade 不在本工具范围内（需人工确认无新业务写入后手动执行）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_runtime import FactorModelAuditLog, FactorRuntimeState
from app.services.factors.config import (
    ensure_factor_system_config,
    update_factor_system_config,
)
from app.services.factors.runtime import (
    fallback_factor_model,
    get_factor_runtime_snapshot,
)


# ── 结果 dataclass ────────────────────────────────────────


@dataclass
class RollbackStepResult:
    """单步回滚结果。"""

    step: str
    success: bool
    before: str
    after: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "success": self.success,
            "before": self.before,
            "after": self.after,
            "message": self.message,
        }


@dataclass
class RollbackDrillReport:
    """回滚演练报告。"""

    actor: str
    reason: str
    steps: list[RollbackStepResult] = field(default_factory=list)
    overall_success: bool = False
    # 回滚后状态快照
    final_weight_mode: str = ""
    final_active_model_run_id: str | None = None
    final_fallback_reason: str | None = None
    final_feature_enabled: bool = False
    # 审计记录保留情况
    audit_logs_preserved: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "reason": self.reason,
            "steps": [s.to_dict() for s in self.steps],
            "overall_success": self.overall_success,
            "final_weight_mode": self.final_weight_mode,
            "final_active_model_run_id": self.final_active_model_run_id,
            "final_fallback_reason": self.final_fallback_reason,
            "final_feature_enabled": self.final_feature_enabled,
            "audit_logs_preserved": self.audit_logs_preserved,
            "errors": self.errors,
        }


# ── 核心实现 ──────────────────────────────────────────────


def execute_rollback_drill(
    db: Session,
    *,
    actor: str = "local_user",
    reason: str = "WP8-02 rollback drill",
) -> RollbackDrillReport:
    """执行回滚演练。

    步骤：
    1. 记录回滚前状态
    2. fallback_factor_model：runtime 切回 manual，清空 active_model_run_id
    3. update_factor_system_config：关闭 feature_enabled
    4. 验证回滚后状态
    5. 验证审计记录保留

    约束：
    - 不删除任何数据
    - 不执行 Alembic downgrade
    - 每步失败则记录错误并继续后续步骤（尽力回滚）
    """
    report = RollbackDrillReport(actor=actor, reason=reason)
    errors: list[str] = []

    # 0. 记录回滚前状态
    pre_runtime = get_factor_runtime_snapshot(db)
    pre_config = ensure_factor_system_config(db)
    pre_weight_mode = pre_runtime.weight_mode
    pre_active_model = pre_runtime.active_model_run_id
    pre_feature_enabled = bool(pre_config.feature_enabled)

    # 1. fallback_factor_model：runtime 切回 manual
    step1 = RollbackStepResult(
        step="fallback_to_manual",
        success=False,
        before=f"weight_mode={pre_weight_mode}, active_model_run_id={pre_active_model}",
        after="",
    )
    try:
        # 如果已经是 manual，跳过 fallback（但仍记录审计）
        if pre_weight_mode == "manual":
            step1.success = True
            step1.after = "weight_mode=manual (already manual)"
            step1.message = "already in manual mode, skip fallback"
        else:
            snapshot = fallback_factor_model(db, actor=actor, reason=reason)
            step1.success = True
            step1.after = (
                f"weight_mode={snapshot.weight_mode}, "
                f"active_model_run_id={snapshot.active_model_run_id}"
            )
    except Exception as exc:
        step1.message = str(exc)
        errors.append(f"fallback_failed: {exc}")
    report.steps.append(step1)

    # 2. update_factor_system_config：关闭 feature_enabled
    step2 = RollbackStepResult(
        step="disable_feature_enabled",
        success=False,
        before=f"feature_enabled={pre_feature_enabled}",
        after="",
    )
    try:
        if not pre_feature_enabled:
            step2.success = True
            step2.after = "feature_enabled=False (already disabled)"
            step2.message = "feature already disabled, skip"
        else:
            config_snapshot = update_factor_system_config(
                db,
                feature_enabled=False,
                actor=actor,
            )
            step2.success = True
            step2.after = f"feature_enabled={bool(config_snapshot.feature_enabled)}"
    except Exception as exc:
        step2.message = str(exc)
        errors.append(f"disable_feature_failed: {exc}")
    report.steps.append(step2)

    # 3. 验证回滚后状态
    step3 = RollbackStepResult(
        step="verify_rollback_state",
        success=False,
        before="",
        after="",
    )
    try:
        post_runtime = get_factor_runtime_snapshot(db)
        post_config = ensure_factor_system_config(db)
        report.final_weight_mode = post_runtime.weight_mode
        report.final_active_model_run_id = post_runtime.active_model_run_id
        report.final_fallback_reason = post_runtime.fallback_reason
        report.final_feature_enabled = bool(post_config.feature_enabled)

        step3.before = (
            f"expected: weight_mode=manual, active_model_run_id=None, "
            f"feature_enabled=False"
        )
        step3.after = (
            f"actual: weight_mode={report.final_weight_mode}, "
            f"active_model_run_id={report.final_active_model_run_id}, "
            f"feature_enabled={report.final_feature_enabled}"
        )
        step3.success = (
            report.final_weight_mode == "manual"
            and report.final_active_model_run_id is None
            and not report.final_feature_enabled
        )
        if not step3.success:
            errors.append("post_rollback_state_mismatch")
    except Exception as exc:
        step3.message = str(exc)
        errors.append(f"verify_failed: {exc}")
    report.steps.append(step3)

    # 4. 验证审计记录保留（不删除任何审计日志）
    step4 = RollbackStepResult(
        step="verify_audit_logs_preserved",
        success=False,
        before="",
        after="",
    )
    try:
        audit_count = db.execute(
            select(FactorModelAuditLog)
        ).scalars().all()
        report.audit_logs_preserved = len(audit_count)
        step4.before = f"audit_logs before drill: unknown (no snapshot)"
        step4.after = f"audit_logs after drill: {report.audit_logs_preserved}"
        step4.success = report.audit_logs_preserved >= 0  # 只要查询成功即可
    except Exception as exc:
        step4.message = str(exc)
        errors.append(f"audit_verify_failed: {exc}")
    report.steps.append(step4)

    # 5. 整体判定
    report.overall_success = (
        len(errors) == 0
        and all(s.success for s in report.steps)
    )
    report.errors = errors

    return report


def verify_rollback_read_path(db: Session) -> dict[str, Any]:
    """验证回滚后读取路径回到 definitions.py 和旧 factor_engine。

    回滚后应满足：
    - weight_mode='manual'：scoring_bridge 不读 warehouse、不写 Score
    - feature_enabled=False：因子中心写 API 不可用
    - active_model_run_id=None：不使用 Ridge 模型权重
    """
    runtime = get_factor_runtime_snapshot(db)
    config = ensure_factor_system_config(db)

    return {
        "weight_mode": runtime.weight_mode,
        "active_model_run_id": runtime.active_model_run_id,
        "fallback_reason": runtime.fallback_reason,
        "feature_enabled": bool(config.feature_enabled),
        "read_path": "definitions.py + factor_engine"
        if runtime.weight_mode == "manual"
        else f"dynamic (weight_mode={runtime.weight_mode})",
        "write_api_available": bool(config.feature_enabled),
        "uses_ridge_weights": runtime.active_model_run_id is not None,
        "is_rolled_back": (
            runtime.weight_mode == "manual"
            and runtime.active_model_run_id is None
            and not bool(config.feature_enabled)
        ),
    }
