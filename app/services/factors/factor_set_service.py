"""WP7-01: FactorSet 服务层（创建/成员管理/content_hash/冻结）。

对齐 docs/专业因子库开发计划.md §WP7-01 和 docs/因子设置与专业因子库改造方案.md §10。

核心能力：
- 创建 FactorSet（draft 状态）
- 添加成员（固定 factor_version_id，写入 factor_code/factor_version 冗余字段）
- 计算 content_hash（成员版本聚合哈希，用于唯一性校验）
- 冻结 FactorSet（draft → frozen，不可再修改成员）
- 查询 FactorSet（含成员列表）

安全约束：
- FactorSet 发布后（frozen）不可修改成员（add/remove/update 均拒绝）
- 成员的 factor_version_id 必须指向已发布（active/shadow/testing）的版本
- content_hash 唯一：同成员组合的 FactorSet 不允许重复冻结
- deprecated 状态终态，不可恢复为 draft/frozen
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorVersion
from app.schemas.factor_library import (
    FactorSetCreate,
    FactorSetMemberCreate,
)


# ── 常量 ──────────────────────────────────────────────────

# 允许加入 FactorSet 的因子版本状态（必须已通过校验）
ALLOWED_VERSION_STATUSES = frozenset({"valid", "testing", "shadow", "active"})

# FactorSet 状态迁移
ALLOWED_SET_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"frozen", "deprecated"},
    "frozen": {"deprecated"},  # frozen 后只能废弃，不可回 draft
    "deprecated": set(),  # 终态
}


# ── 错误 ──────────────────────────────────────────────────


class FactorSetError(Exception):
    """FactorSet 服务错误。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


# ── 工具函数 ──────────────────────────────────────────────


def _utcnow() -> datetime:
    """UTC 当前时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_set_id(name: str) -> str:
    """根据名称生成 FactorSet ID（fs-<name-slug>-<hash8>）。"""
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    slug = slug[:32] or "set"
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"fs-{slug}-{h}"


def compute_set_content_hash(members: list[FactorSetMember]) -> str:
    """计算 FactorSet 成员版本聚合哈希。

    哈希内容：按 (factor_code, factor_version, role, weight_constraint, missing_policy, display_order) 排序后拼接。
    用于唯一性校验：同成员组合的 FactorSet 不允许重复冻结。
    """
    items = sorted(
        (
            {
                "factor_code": m.factor_code,
                "factor_version": m.factor_version,
                "factor_version_id": m.factor_version_id,
                "role": m.role,
                "weight_constraint": m.weight_constraint,
                "display_order": m.display_order,
                "missing_policy": m.missing_policy,
            }
            for m in members
        ),
        key=lambda x: (x["factor_code"], x["factor_version"], x["role"]),
    )
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ── 结果 dataclass ────────────────────────────────────────


@dataclass
class FactorSetResult:
    """FactorSet 操作结果。"""

    factor_set: FactorSet
    is_new: bool = True
    content_hash: str | None = None


# ── 创建 ──────────────────────────────────────────────────


def create_factor_set(
    db: Session,
    *,
    payload: FactorSetCreate,
) -> FactorSetResult:
    """创建 FactorSet（draft 状态）。

    幂等：同 ID 已存在且仍为 draft 时返回既有记录。
    """
    set_id = payload.id or _build_set_id(payload.name)

    existing = db.get(FactorSet, set_id)
    if existing is not None:
        return FactorSetResult(factor_set=existing, is_new=False)

    factor_set = FactorSet(
        id=set_id,
        name=payload.name,
        description=payload.description,
        status="draft",
        content_hash=None,
        frozen_at=None,
        created_by=payload.created_by,
    )
    db.add(factor_set)
    db.flush()
    return FactorSetResult(factor_set=factor_set, is_new=True)


# ── 成员管理 ──────────────────────────────────────────────


def add_member(
    db: Session,
    *,
    factor_set_id: str,
    payload: FactorSetMemberCreate,
) -> FactorSetMember:
    """向 FactorSet 添加成员。

    约束：
    - FactorSet 必须为 draft 状态（frozen/deprecated 拒绝修改）
    - factor_version_id 必须存在且状态为 testing/shadow/active
    - 同 factor_id 不允许重复加入（唯一约束）
    """
    factor_set = _get_set_or_raise(db, factor_set_id)

    if factor_set.status != "draft":
        raise FactorSetError(
            "set_not_mutable",
            f"FactorSet {factor_set_id} 状态为 {factor_set.status}，不允许修改成员",
        )

    # 校验 factor_version
    version = db.get(FactorVersion, payload.factor_version_id)
    if version is None:
        raise FactorSetError(
            "version_not_found",
            f"FactorVersion {payload.factor_version_id} 不存在",
        )

    factor = db.get(Factor, payload.factor_id)
    if factor is None:
        raise FactorSetError(
            "factor_not_found",
            f"Factor {payload.factor_id} 不存在",
        )

    # 版本状态门禁
    version_status = getattr(version, "validation_status", None) or "pending"
    if version_status not in ALLOWED_VERSION_STATUSES:
        raise FactorSetError(
            "version_status_not_allowed",
            f"FactorVersion {payload.factor_version_id} 状态为 {version_status}，"
            f"只允许 {sorted(ALLOWED_VERSION_STATUSES)}",
        )

    # factor_id 与 version 的 factor_id 一致性
    if version.factor_id != payload.factor_id:
        raise FactorSetError(
            "version_factor_mismatch",
            f"FactorVersion {payload.factor_version_id} 属于 factor_id={version.factor_id}，"
            f"与传入 factor_id={payload.factor_id} 不一致",
        )

    # 唯一性检查
    existing = db.execute(
        select(FactorSetMember)
        .where(
            FactorSetMember.factor_set_id == factor_set_id,
            FactorSetMember.factor_id == payload.factor_id,
        )
        .limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        raise FactorSetError(
            "member_already_exists",
            f"Factor {payload.factor_id} 已在 FactorSet {factor_set_id} 中",
        )

    member = FactorSetMember(
        factor_set_id=factor_set_id,
        factor_id=payload.factor_id,
        factor_version_id=payload.factor_version_id,
        factor_code=factor.code,
        factor_version=version.version,
        role=payload.role,
        weight_constraint=payload.weight_constraint,
        display_order=payload.display_order,
        missing_policy=payload.missing_policy,
    )
    db.add(member)
    db.flush()
    return member


def remove_member(
    db: Session,
    *,
    factor_set_id: str,
    factor_id: int,
) -> None:
    """从 FactorSet 移除成员（仅 draft 状态）。"""
    factor_set = _get_set_or_raise(db, factor_set_id)

    if factor_set.status != "draft":
        raise FactorSetError(
            "set_not_mutable",
            f"FactorSet {factor_set_id} 状态为 {factor_set.status}，不允许修改成员",
        )

    member = db.execute(
        select(FactorSetMember)
        .where(
            FactorSetMember.factor_set_id == factor_set_id,
            FactorSetMember.factor_id == factor_id,
        )
        .limit(1)
    ).scalar_one_or_none()

    if member is None:
        raise FactorSetError(
            "member_not_found",
            f"Factor {factor_id} 不在 FactorSet {factor_set_id} 中",
        )

    db.delete(member)
    db.flush()


# ── 冻结 ──────────────────────────────────────────────────


def freeze_factor_set(
    db: Session,
    *,
    factor_set_id: str,
    actor: str = "local_user",
    reason: str | None = None,
) -> FactorSet:
    """冻结 FactorSet（draft → frozen）。

    约束：
    - 当前状态必须为 draft
    - 至少有 1 个成员
    - 计算 content_hash 并检查唯一性（同哈希的 frozen 集合已存在则拒绝）
    """
    factor_set = _get_set_or_raise(db, factor_set_id)

    if factor_set.status != "draft":
        raise FactorSetError(
            "cannot_freeze",
            f"FactorSet {factor_set_id} 状态为 {factor_set.status}，只能冻结 draft",
        )

    members = list(factor_set.members)
    if not members:
        raise FactorSetError(
            "empty_set",
            f"FactorSet {factor_set_id} 无成员，不允许冻结",
        )

    content_hash = compute_set_content_hash(members)

    # 唯一性检查：同 content_hash 的 frozen 集合已存在则拒绝
    duplicate = db.execute(
        select(FactorSet)
        .where(
            FactorSet.content_hash == content_hash,
            FactorSet.status == "frozen",
            FactorSet.id != factor_set_id,
        )
        .limit(1)
    ).scalar_one_or_none()

    if duplicate is not None:
        raise FactorSetError(
            "duplicate_frozen_set",
            f"已存在相同成员的冻结 FactorSet: {duplicate.id} ({duplicate.name})",
        )

    factor_set.status = "frozen"
    factor_set.frozen_at = _utcnow()
    factor_set.content_hash = content_hash
    factor_set.updated_at = _utcnow()
    db.flush()
    return factor_set


def deprecate_factor_set(
    db: Session,
    *,
    factor_set_id: str,
    reason: str | None = None,
) -> FactorSet:
    """废弃 FactorSet（draft/frozen → deprecated，终态）。"""
    factor_set = _get_set_or_raise(db, factor_set_id)

    if factor_set.status == "deprecated":
        return factor_set  # 幂等

    if factor_set.status not in ALLOWED_SET_TRANSITIONS:
        raise FactorSetError(
            "cannot_deprecate",
            f"FactorSet {factor_set_id} 状态异常: {factor_set.status}",
        )

    factor_set.status = "deprecated"
    factor_set.updated_at = _utcnow()
    db.flush()
    return factor_set


# ── 查询 ──────────────────────────────────────────────────


def _get_set_or_raise(db: Session, factor_set_id: str) -> FactorSet:
    """获取 FactorSet，不存在则抛错。"""
    factor_set = db.get(FactorSet, factor_set_id)
    if factor_set is None:
        raise FactorSetError(
            "set_not_found",
            f"FactorSet {factor_set_id} 不存在",
        )
    return factor_set


def get_factor_set(db: Session, factor_set_id: str) -> FactorSet | None:
    """获取 FactorSet（含成员）。"""
    return db.get(FactorSet, factor_set_id)


def list_factor_sets(
    db: Session,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[FactorSet]:
    """列出 FactorSet。"""
    stmt = select(FactorSet).order_by(FactorSet.created_at.desc())
    if status:
        stmt = stmt.where(FactorSet.status == status)
    stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


def get_factor_set_members(
    db: Session,
    factor_set_id: str,
) -> list[FactorSetMember]:
    """获取 FactorSet 成员列表（按 display_order 排序）。"""
    stmt = (
        select(FactorSetMember)
        .where(FactorSetMember.factor_set_id == factor_set_id)
        .order_by(FactorSetMember.display_order.asc(), FactorSetMember.factor_code.asc())
    )
    return list(db.execute(stmt).scalars().all())


def to_read_dict(factor_set: FactorSet) -> dict[str, Any]:
    """转换为字典（含成员）。"""
    members = sorted(
        factor_set.members,
        key=lambda m: (m.display_order, m.factor_code),
    )
    return {
        "id": factor_set.id,
        "name": factor_set.name,
        "description": factor_set.description,
        "content_hash": factor_set.content_hash,
        "status": factor_set.status,
        "frozen_at": factor_set.frozen_at.isoformat() if factor_set.frozen_at else None,
        "created_by": factor_set.created_by,
        "created_at": factor_set.created_at.isoformat() if factor_set.created_at else None,
        "updated_at": factor_set.updated_at.isoformat() if factor_set.updated_at else None,
        "n_members": len(members),
        "members": [
            {
                "id": m.id,
                "factor_set_id": m.factor_set_id,
                "factor_id": m.factor_id,
                "factor_version_id": m.factor_version_id,
                "factor_code": m.factor_code,
                "factor_version": m.factor_version,
                "role": m.role,
                "weight_constraint": m.weight_constraint,
                "display_order": m.display_order,
                "missing_policy": m.missing_policy,
                "excluded_reason": m.excluded_reason,
            }
            for m in members
        ],
    }
