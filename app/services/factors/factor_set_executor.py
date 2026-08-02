"""WP7-02: 动态因子计算（按 FactorSet 执行 FactorExecutor）。

对齐 docs/专业因子库开发计划.md §WP7-02 和 docs/因子设置与专业因子库改造方案.md §10.2。

核心能力：
- 按 FactorSet 成员列表，使用各成员的 FactorVersion.formula_ast_json + postprocess_json
  重新构建 ExecutionPlan 并调用 FactorExecutor.execute 写入 factor_values
- 不再依赖硬编码的 FACTOR_DEFINITIONS（降级为系统种子+灾备）
- 支持 trade_date 范围、calc_batch_id 生成
- 失败成员跳过并记录错误（不阻断其他成员）

设计原则：
- 复用现有 FactorExecutor（DuckDB 兼容执行 + 单写锁 + 失败不覆盖旧批次）
- 复用 factor_compiler.compile_formula 重新构建 ExecutionPlan（确保执行计划哈希一致）
- target/regime 角色成员跳过（不作为特征计算）
- frozen FactorSet 才允许执行（保证成员版本不可变）
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorVersion
from app.services.factors.factor_compiler import CompilationResult, compile_formula
from app.services.factors.factor_executor import ExecutionOutcome, FactorExecutor
from app.services.factors.factor_set_service import get_factor_set
from app.services.factors.store import FactorWarehouse


# ── 结果 ──────────────────────────────────────────────────


@dataclass
class MemberExecutionResult:
    """单个成员的执行结果。"""

    factor_code: str
    factor_version: int
    factor_version_id: int
    success: bool
    outcome: ExecutionOutcome | None = None
    error: str | None = None


@dataclass
class FactorSetExecutionResult:
    """FactorSet 执行结果。"""

    factor_set_id: str
    calc_batch_id: str
    trade_date: date | None
    total_members: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0  # target/regime 角色
    results: list[MemberExecutionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_set_id": self.factor_set_id,
            "calc_batch_id": self.calc_batch_id,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "total_members": self.total_members,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "results": [
                {
                    "factor_code": r.factor_code,
                    "factor_version": r.factor_version,
                    "factor_version_id": r.factor_version_id,
                    "success": r.success,
                    "error": r.error,
                    "outcome": r.outcome.to_dict() if r.outcome else None,
                }
                for r in self.results
            ],
            "errors": self.errors,
        }


# ── 核心实现 ──────────────────────────────────────────────


def _load_version_plan(version: FactorVersion) -> CompilationResult:
    """从 FactorVersion 重新构建 ExecutionPlan。

    使用 formula_expr + params_json + postprocess_json + direction 调用编译器，
    确保执行计划哈希与创建版本时一致。
    """
    params = json.loads(version.params_json) if version.params_json else {}
    postprocess = (
        json.loads(version.postprocess_json) if version.postprocess_json else None
    )

    return compile_formula(
        formula=version.formula_expr,
        params=params,
        direction=version.direction or "higher_better",
        postprocess=postprocess,
        strict_fields=True,
    )


def execute_factor_set(
    db: Session,
    warehouse: FactorWarehouse,
    *,
    factor_set_id: str,
    trade_date: date | None = None,
    calc_batch_id: str | None = None,
    executor: FactorExecutor | None = None,
) -> FactorSetExecutionResult:
    """按 FactorSet 执行所有 feature 成员的因子计算。

    约束：
    - FactorSet 必须为 frozen 状态（保证成员版本不可变）
    - target/regime 角色成员跳过
    - 失败成员记录错误但不阻断其他成员
    - calc_batch_id 不传则自动生成（fs-<set_id>-<uuid8>）

    返回：FactorSetExecutionResult
    """
    factor_set = get_factor_set(db, factor_set_id)
    if factor_set is None:
        raise ValueError(f"factor_set_not_found:{factor_set_id}")

    if factor_set.status != "frozen":
        raise ValueError(
            f"factor_set_not_frozen:{factor_set_id} status={factor_set.status}"
        )

    batch_id = calc_batch_id or f"fs-{factor_set_id}-{uuid.uuid4().hex[:8]}"
    result = FactorSetExecutionResult(
        factor_set_id=factor_set_id,
        calc_batch_id=batch_id,
        trade_date=trade_date,
    )

    # 按顺序执行成员
    members = sorted(
        factor_set.members,
        key=lambda m: (m.display_order, m.factor_code),
    )
    result.total_members = len(members)

    # 复用 executor 实例（避免重复创建连接）
    exec_instance = executor or FactorExecutor(warehouse)

    for member in members:
        # target/regime 角色跳过
        if member.role in ("target", "regime"):
            result.skipped_count += 1
            result.results.append(
                MemberExecutionResult(
                    factor_code=member.factor_code,
                    factor_version=member.factor_version,
                    factor_version_id=member.factor_version_id,
                    success=True,
                    error=f"skipped_role:{member.role}",
                )
            )
            continue

        try:
            # 加载版本
            version = db.get(FactorVersion, member.factor_version_id)
            if version is None:
                raise ValueError(
                    f"version_not_found:{member.factor_version_id}"
                )

            # 重新编译执行计划
            compilation = _load_version_plan(version)
            if not compilation.is_valid or compilation.execution_plan is None:
                errors_str = "; ".join(
                    e.message if hasattr(e, "message") else str(e)
                    for e in compilation.errors
                )
                raise ValueError(f"compilation_failed:{errors_str}")

            # 执行写入
            outcome = exec_instance.execute(
                compilation.execution_plan,
                factor_code=member.factor_code,
                factor_version=member.factor_version,
                trade_date=trade_date,
                calc_batch_id=batch_id,
            )

            result.results.append(
                MemberExecutionResult(
                    factor_code=member.factor_code,
                    factor_version=member.factor_version,
                    factor_version_id=member.factor_version_id,
                    success=True,
                    outcome=outcome,
                )
            )
            result.success_count += 1

        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)
            result.errors.append(
                f"{member.factor_code}@v{member.factor_version}: {err_msg}"
            )
            result.results.append(
                MemberExecutionResult(
                    factor_code=member.factor_code,
                    factor_version=member.factor_version,
                    factor_version_id=member.factor_version_id,
                    success=False,
                    error=err_msg,
                )
            )
            result.failed_count += 1

    return result


def preview_factor_set_members(
    db: Session,
    *,
    factor_set_id: str,
) -> list[dict[str, Any]]:
    """预览 FactorSet 成员的执行计划信息（不实际执行）。

    用于 UI 展示：每个成员的公式、参数、后处理、执行计划哈希。
    """
    factor_set = get_factor_set(db, factor_set_id)
    if factor_set is None:
        raise ValueError(f"factor_set_not_found:{factor_set_id}")

    members = sorted(
        factor_set.members,
        key=lambda m: (m.display_order, m.factor_code),
    )

    previews: list[dict[str, Any]] = []
    for member in members:
        version = db.get(FactorVersion, member.factor_version_id)
        if version is None:
            previews.append(
                {
                    "factor_code": member.factor_code,
                    "factor_version": member.factor_version,
                    "factor_version_id": member.factor_version_id,
                    "role": member.role,
                    "error": f"version_not_found:{member.factor_version_id}",
                }
            )
            continue

        try:
            compilation = _load_version_plan(version)
            previews.append(
                {
                    "factor_code": member.factor_code,
                    "factor_version": member.factor_version,
                    "factor_version_id": member.factor_version_id,
                    "role": member.role,
                    "formula_expr": version.formula_expr,
                    "direction": version.direction,
                    "params": json.loads(version.params_json) if version.params_json else {},
                    "postprocess": json.loads(version.postprocess_json)
                    if version.postprocess_json
                    else None,
                    "compilation_success": compilation.is_valid,
                    "compilation_errors": [
                        e.message if hasattr(e, "message") else str(e)
                        for e in compilation.errors
                    ],
                    "execution_plan_hash": compilation.execution_plan.content_hash()
                    if compilation.execution_plan
                    else None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            previews.append(
                {
                    "factor_code": member.factor_code,
                    "factor_version": member.factor_version,
                    "factor_version_id": member.factor_version_id,
                    "role": member.role,
                    "error": str(exc),
                }
            )

    return previews
