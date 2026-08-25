"""Q24 / WP0-7a, WP0-7b: FactorSet 服务。

契约：
1. FactorSet.status in {draft, frozen, deprecated} (Q24.1)
2. draft → frozen 后 **成员不可原地修改**；任何变更必须复制生成新 FactorSet (Q24.3)
   通过新 ID + content_hash 区分版本。
3. 训练/模型运行必须显式传递 factor_set_id；禁止空/None (Q24.2)
4. WP0-7b readiness: 组合绑定的 active FactorSet 必须 status=frozen 且成员数>=1、
   对应 active 模型存在 Score 覆盖 >=95%，否则阻断。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor import Factor
from app.models.factor_model import FactorVersion
from app.core.hash_utils import canonical_json

# ── 私有工具 ────────────────────────────────────────────────────────

_VALID_STATUSES = {"draft", "frozen", "deprecated"}
_FROZEN_IMMUTABLE_MSG = "FactorSet(id={}) 已 frozen，禁止原地修改 (Q24.3). 复制生成新 FactorSet."


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _compute_content_hash(members: list[FactorSetMember]) -> str:
    """按 (factor_code, factor_version, role, weight_constraint) 排序生成 canonical JSON → sha256。"""
    sig = []
    for m in sorted(members, key=lambda m_: (m_.factor_code, m_.factor_version, m_.role or "", m_.weight_constraint or "")):
        sig.append({
            "factor_code": m.factor_code,
            "factor_version": int(m.factor_version),
            "role": m.role,
            "weight_constraint": m.weight_constraint,
        })
    raw = canonical_json(sig)
    return "fs_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _assert_not_frozen(fs: FactorSet) -> None:
    if fs.status == "frozen":
        raise RuntimeError(_FROZEN_IMMUTABLE_MSG.format(fs.id))
    if fs.status == "deprecated":
        raise RuntimeError(f"FactorSet(id={fs.id}) 已 deprecated，禁止修改 (Q24.3)")


# ── 公共 API ────────────────────────────────────────────────────────


def create_factor_set(
    db: "Session",
    *,
    factor_set_id: str,
    name: str,
    description: str | None = None,
    created_by: str = "local_user",
) -> FactorSet:
    """新建 draft 状态的 FactorSet。"""
    if not factor_set_id:
        raise ValueError("factor_set_id 不能为空")
    if factor_set_id in ("", None):
        raise ValueError("factor_set_id 必填")
    if len(name) == 0:
        raise ValueError("name 必填")
    exists = db.get(FactorSet, factor_set_id)
    if exists is not None:
        raise ValueError(f"FactorSet(id={factor_set_id}) 已存在")
    fs = FactorSet(
        id=factor_set_id,
        name=name,
        description=description,
        status="draft",
        frozen_at=None,
        created_by=created_by,
    )
    db.add(fs)
    db.commit()
    db.refresh(fs)
    return fs


def add_member(
    db: "Session",
    factor_set_id: str,
    *,
    factor_id: int,
    factor_version_id: int,
    factor_code: str,
    factor_version: int,
    role: str = "feature",
    weight_constraint: str | None = None,
) -> FactorSetMember:
    """向 draft FactorSet 增加成员。冻结后禁止。"""
    fs = db.get(FactorSet, factor_set_id)
    if fs is None:
        raise KeyError(f"FactorSet(id={factor_set_id}) 不存在")
    _assert_not_frozen(fs)
    mem = FactorSetMember(
        factor_set_id=factor_set_id,
        factor_id=int(factor_id),
        factor_version_id=int(factor_version_id),
        factor_code=str(factor_code),
        factor_version=int(factor_version),
        role=str(role),
        weight_constraint=weight_constraint,
    )
    db.add(mem)
    fs.updated_at = _utcnow_naive()
    db.commit()
    db.refresh(mem)
    return mem


def remove_member(db: "Session", factor_set_id: str, member_id: int) -> None:
    """从 draft FactorSet 删除成员。冻结后禁止。"""
    fs = db.get(FactorSet, factor_set_id)
    if fs is None:
        raise KeyError(f"FactorSet(id={factor_set_id}) 不存在")
    _assert_not_frozen(fs)
    mem = db.get(FactorSetMember, member_id)
    if mem is None or mem.factor_set_id != factor_set_id:
        raise KeyError(f"FactorSetMember(id={member_id}, fs={factor_set_id}) 不存在")
    db.delete(mem)
    fs.updated_at = _utcnow_naive()
    db.commit()


def freeze_factor_set(db: "Session", factor_set_id: str) -> FactorSet:
    """draft → frozen。写入 content_hash 与 frozen_at；之后成员不可变更。"""
    fs = db.get(FactorSet, factor_set_id)
    if fs is None:
        raise KeyError(f"FactorSet(id={factor_set_id}) 不存在")
    if fs.status == "frozen":
        return fs  # 幂等
    if fs.status == "deprecated":
        raise RuntimeError(f"FactorSet(id={factor_set_id}) 已 deprecated，无法冻结")
    # 读取 members（必须有 >=1 才能冻结）
    members = list(db.execute(
        select(FactorSetMember).where(FactorSetMember.factor_set_id == factor_set_id)
    ).scalars().all())
    if not members:
        raise RuntimeError(f"FactorSet(id={factor_set_id}) 空成员，禁止冻结 (Q24.3)")
    fs.status = "frozen"
    fs.content_hash = _compute_content_hash(members)
    fs.frozen_at = _utcnow_naive()
    fs.updated_at = fs.frozen_at
    db.commit()
    db.refresh(fs)
    return fs


def deprecate_factor_set(db: "Session", factor_set_id: str) -> FactorSet:
    """frozen → deprecated（仅软标记）。"""
    fs = db.get(FactorSet, factor_set_id)
    if fs is None:
        raise KeyError(f"FactorSet(id={factor_set_id}) 不存在")
    if fs.status == "deprecated":
        return fs
    fs.status = "deprecated"
    fs.updated_at = _utcnow_naive()
    db.commit()
    db.refresh(fs)
    return fs


def copy_factor_set_to_new_id(
    db: "Session",
    source_factor_set_id: str,
    *,
    new_factor_set_id: str,
    new_name: str | None = None,
    created_by: str = "local_user",
) -> FactorSet:
    """Q24.3：frozen FactorSet 变更必须复制生成新 ID (status=draft, 成员复制)。
    若源为 draft，则等价于 dup + 回到 draft 状态。"""
    src = db.get(FactorSet, source_factor_set_id)
    if src is None:
        raise KeyError(f"FactorSet(id={source_factor_set_id}) 不存在")
    if new_factor_set_id in ("", None):
        raise ValueError("new_factor_set_id 必填")
    exists = db.get(FactorSet, new_factor_set_id)
    if exists is not None:
        raise ValueError(f"FactorSet(id={new_factor_set_id}) 已存在")

    new_fs = FactorSet(
        id=new_factor_set_id,
        name=new_name or (src.name + " (copy)"),
        description=src.description,
        status="draft",
        frozen_at=None,
        content_hash=None,
        created_by=created_by,
    )
    db.add(new_fs)
    db.flush()
    src_members = list(db.execute(
        select(FactorSetMember).where(
            FactorSetMember.factor_set_id == source_factor_set_id
        )
    ).scalars().all())
    for m in src_members:
        db.add(FactorSetMember(
            factor_set_id=new_fs.id,
            factor_id=m.factor_id,
            factor_version_id=m.factor_version_id,
            factor_code=m.factor_code,
            factor_version=m.factor_version,
            role=m.role,
            weight_constraint=m.weight_constraint,
        ))
    db.commit()
    db.refresh(new_fs)
    return new_fs


def get_factor_set(db: "Session", factor_set_id: str) -> FactorSet | None:
    return db.get(FactorSet, factor_set_id)


def list_members(db: "Session", factor_set_id: str) -> list[FactorSetMember]:
    return list(db.execute(
        select(FactorSetMember).where(FactorSetMember.factor_set_id == factor_set_id)
        .order_by(FactorSetMember.id.asc())
    ).scalars().all())


# ── WP0-7b: FactorSet readiness 门禁 ────────────────────────────────


def _make_readiness(
    ready: bool, *, factor_set_id: str, code: str, message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ready": bool(ready),
        "factor_set_id": factor_set_id,
        "code": code,
        "message": message,
        "details": details or {},
    }


def _member_lineage_issue(
    member: FactorSetMember,
    *,
    factors_by_id: dict[int, Factor],
    versions_by_id: dict[int, FactorVersion],
) -> tuple[str, str, dict[str, Any]] | None:
    """Return the first immutable lineage inconsistency for a set member."""
    factor = factors_by_id.get(member.factor_id)
    if factor is None:
        return (
            "MEMBER_FACTOR_NOT_FOUND",
            "FactorSet member references a missing Factor.",
            {
                "member_id": member.id,
                "factor_id": member.factor_id,
                "factor_version_id": member.factor_version_id,
            },
        )

    version = versions_by_id.get(member.factor_version_id)
    if version is None:
        return (
            "MEMBER_FACTOR_VERSION_NOT_FOUND",
            "FactorSet member references a missing FactorVersion.",
            {
                "member_id": member.id,
                "factor_id": member.factor_id,
                "factor_version_id": member.factor_version_id,
            },
        )
    if version.factor_id != member.factor_id:
        return (
            "MEMBER_FACTOR_VERSION_OWNER_MISMATCH",
            "FactorSet member's FactorVersion belongs to a different Factor.",
            {
                "member_id": member.id,
                "factor_id": member.factor_id,
                "factor_version_id": member.factor_version_id,
                "version_factor_id": version.factor_id,
            },
        )
    if factor.code != member.factor_code:
        return (
            "MEMBER_FACTOR_CODE_MISMATCH",
            "FactorSet member's factor_code does not match its Factor.",
            {
                "member_id": member.id,
                "factor_id": member.factor_id,
                "factor_code": member.factor_code,
                "actual_factor_code": factor.code,
            },
        )
    if version.version != member.factor_version:
        return (
            "MEMBER_FACTOR_VERSION_MISMATCH",
            "FactorSet member's factor_version does not match its FactorVersion.",
            {
                "member_id": member.id,
                "factor_version_id": member.factor_version_id,
                "factor_version": member.factor_version,
                "actual_factor_version": version.version,
            },
        )
    return None


def factor_set_readiness(
    db: "Session",
    factor_set_id: str,
    *,
    require_frozen: bool = True,
) -> dict[str, Any]:
    """WP0-7b: FactorSet readiness。

    Checks (Q24 / Q5 / Q15):
      1. FactorSet 存在
      2. status == frozen (require_frozen=True 时，默认生产)
      3. members count >= 1
      4. content_hash 非空 (已冻结且签名)
      5. 每个成员的 Factor/FactorVersion/代码/版本引用一致
    """
    fs = get_factor_set(db, factor_set_id)
    if fs is None:
        return _make_readiness(False, factor_set_id=factor_set_id,
                              code="NOT_FOUND", message=f"FactorSet {factor_set_id} 不存在")
    if require_frozen and fs.status != "frozen":
        return _make_readiness(
            False, factor_set_id=factor_set_id,
            code="NOT_FROZEN",
            message=f"FactorSet status={fs.status}，必须为 frozen 才能用于训练/生产 (Q24.3)",
            details={"status": fs.status},
        )
    members = list_members(db, factor_set_id)
    n_members = len(members)
    if n_members < 1:
        return _make_readiness(
            False, factor_set_id=factor_set_id,
            code="EMPTY_SET",
            message="FactorSet 空成员 (>=1 个因子)",
            details={"member_count": n_members},
        )
    if require_frozen and (not fs.content_hash or len(fs.content_hash) < 16):
        return _make_readiness(
            False, factor_set_id=factor_set_id,
            code="NO_CONTENT_HASH",
            message="冻结 FactorSet 缺少 content_hash 签名，非法；请重新 freeze",
        )
    factors_by_id = {
        factor.id: factor
        for factor in db.execute(
            select(Factor).where(Factor.id.in_({member.factor_id for member in members}))
        ).scalars()
    }
    versions_by_id = {
        version.id: version
        for version in db.execute(
            select(FactorVersion).where(
                FactorVersion.id.in_({member.factor_version_id for member in members})
            )
        ).scalars()
    }
    for member in members:
        issue = _member_lineage_issue(
            member,
            factors_by_id=factors_by_id,
            versions_by_id=versions_by_id,
        )
        if issue is not None:
            code, message, details = issue
            return _make_readiness(
                False,
                factor_set_id=factor_set_id,
                code=code,
                message=message,
                details=details,
            )
    return _make_readiness(
        True, factor_set_id=factor_set_id,
        code="READY",
        message=f"FactorSet {factor_set_id} 就绪：status={fs.status}, members={n_members}",
        details={
            "status": fs.status,
            "member_count": n_members,
            "content_hash": fs.content_hash,
            "frozen_at": fs.frozen_at.isoformat() + "Z" if fs.frozen_at else None,
        },
    )
