"""候选池服务（SD-v2.0 §6.3 · M3 / 任务 T08）。

本文件的职责边界
================
✅ 池 CRUD（create / get / list / update）
✅ 成员批量添加、批量软删除
✅ 审计（写 `FactorAuditLog`，与业务写在**同一事务**内一起提交）
✅ 成员列表（只读，join `symbols` 取展示字段）

❌ 不做（属其它任务）
  - 条件规则编译 / 预览 / 区间预设 → T09
  - CSV / Excel 导入与导出 → T10
  - 看板分析 / 冻结快照 / 锁定与解锁 → T11

隔离模型（开发文档 §3.0.1）——**本文件最重要的不变式**
=====================================================
1. 候选池**不写回** `symbols` / 行情 / 财报主表。成员只存 `symbol_id`；
   名称/行业/板块一律 **join 读**，不复制、不回写。
2. **批量删除只做关联软删除**（`is_deleted=1` + `deleted_at`），主数据一行不动。
3. `source_type`（`filter` / `import`）**创建后不可切换**；尝试切换一律拒绝。
4. `(pool_id, symbol_id)` 唯一由 DB 约束保证；本服务对「重复添加」做**幂等**处理
   （已在池中且未删除 → 计入 `skipped_existing`，不报错）；
   对「曾软删除又加回」做**复活**（重置 `is_deleted=0` / `deleted_at=NULL`，计入 `reactivated`）。

审计落点为什么是 `FactorAuditLog`（而不是 `data_governance_audit_events`）
=========================================================================
- `data_governance_audit_events.action` 上有 **DB 级 CHECK 约束**（`ck_dg_audit_action_values`），
  新增动作值必须**同时**改代码白名单 + **加一次迁移**改 CHECK —— 超出 T08 的写权限，
  且该表语义属于 portfolio / 数据治理域。
- `FactorAuditLog`（`factor_audit_logs`，AC-18「全写操作审计」）的 `action` 是**自由字符串、无 CHECK**
  → 无需迁移；它就是因子域的写操作审计表（`pipeline_task` 已在用），
  而挖掘与因子中心/因子模型同属一体。

⚠️ 已上报的口径分歧：`FactorAuditLog` 的 `factor_set_id` 语义是「FactorSet 的稳定关联键」。
   候选池不是 FactorSet，故该列留 `NULL`，把 `pool_id` 放进 `attributes_json`。
   若后续要求「候选池审计独立成表/独立列」，需一次迁移与 schema 决策（见 PROGRESS observations）。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.hash_utils import canonical_json
from app.models.factor_runtime import FactorAuditLog
from app.models.mining_candidate_pool import (
    TrainingCandidatePool,
    TrainingCandidatePoolMember,
    TrainingCandidatePoolSnapshot,
)
from app.models.symbol import Symbol
from app.schemas.errors import FactorSevenError

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

#: `source_type` 取值。创建后**不可切换**。
SOURCE_TYPE_FILTER = "filter"
SOURCE_TYPE_IMPORT = "import"
VALID_SOURCE_TYPES: frozenset[str] = frozenset({SOURCE_TYPE_FILTER, SOURCE_TYPE_IMPORT})

#: 候选池有效标的**硬下限**（需求 §3.6 / 向导 §3.5：50 为系统硬下限，前端不可降低）。
#: **单一事实源在这里**：`rules.py` / `presets.py` 从这里导入，
#: 因为 `rules` 已经 import 了 `service`，反向会造成循环导入。
MIN_POOL_SIZE = 50

#: 池状态机（`factor_models` 之外的自有状态，M3 只用 draft/frozen）
POOL_STATUS_DRAFT = "draft"
POOL_STATUS_FROZEN = "frozen"
POOL_STATUS_NEEDS_RECHECK = "needs_recheck"
POOL_STATUS_INVALIDATED = "invalidated"
VALID_POOL_STATUSES: frozenset[str] = frozenset(
    {POOL_STATUS_DRAFT, POOL_STATUS_FROZEN, POOL_STATUS_NEEDS_RECHECK, POOL_STATUS_INVALIDATED}
)

#: 审计动作（写入 `factor_audit_logs.action`，自由字符串）
AUDIT_POOL_CREATED = "pool_created"
AUDIT_POOL_UPDATED = "pool_updated"
AUDIT_POOL_MEMBERS_ADDED = "pool_members_added"
AUDIT_POOL_MEMBERS_SOFT_DELETED = "pool_members_soft_deleted"

MAX_NAME_LEN = 128
MAX_BATCH = 5000  # 单次批量操作的标的数上限（防御性；导入走 T10）


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════════
# 结果类型
# ══════════════════════════════════════════════════════════════


@dataclass
class MemberChangeResult:
    """成员批量增删的结果统计（前端「全选/批量操作」需要逐项计数）。"""

    pool_id: str
    requested: int
    member_count: int
    added: int = 0
    reactivated: int = 0
    skipped_existing: int = 0
    removed: int = 0
    not_in_pool: int = 0
    audit_event_id: int | None = None
    changed_symbol_ids: list[int] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.added + self.reactivated + self.removed

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "requested": self.requested,
            "member_count": self.member_count,
            "added": self.added,
            "reactivated": self.reactivated,
            "skipped_existing": self.skipped_existing,
            "removed": self.removed,
            "not_in_pool": self.not_in_pool,
            "changed": self.changed,
            "audit_event_id": self.audit_event_id,
        }


# ══════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════


def _pool_not_found(pool_id: str) -> FactorSevenError:
    return FactorSevenError(
        "NOT_FOUND",
        title_zh="候选池不存在",
        detail_zh=f"未找到候选池 {pool_id}，可能已被删除或 ID 有误。",
        impact="本次操作未执行",
        fix_link="/settings/factor-mining?step=1",
        retryable=False,
        extras={"pool_id": pool_id},
    )


def _validation_error(detail: str, **extras: Any) -> FactorSevenError:
    return FactorSevenError(
        "VALIDATION_ERROR",
        detail_zh=detail,
        impact="本次操作未执行",
        fix_link="/settings/factor-mining?step=1",
        retryable=False,
        extras=extras or None,
    )


def _business_blocked(detail: str, *, reason: str, **extras: Any) -> FactorSevenError:
    return FactorSevenError(
        "BUSINESS_BLOCKED",
        title_zh="当前状态不允许该操作",
        detail_zh=detail,
        impact="本次操作被拒绝，数据未发生变更",
        fix_link="/settings/factor-mining?step=1",
        retryable=False,
        extras={"reason": reason, **extras},
    )


def get_pool(db: Session, pool_id: str) -> TrainingCandidatePool:
    """取池，不存在则抛 `NOT_FOUND`（7 要素错误）。"""
    pool = db.get(TrainingCandidatePool, pool_id)
    if pool is None:
        raise _pool_not_found(pool_id)
    return pool


def _locked_snapshot(db: Session, pool_id: str) -> TrainingCandidatePoolSnapshot | None:
    """该池是否存在**已锁定**的快照。

    `is_locked` 在快照表上（不在池表上，见 `mining_candidate_pool.py`）。
    向导 §3.7.3：分析完成后锁定**筛选 / 导入 / 批量删除**。
    """
    return db.execute(
        select(TrainingCandidatePoolSnapshot)
        .where(
            TrainingCandidatePoolSnapshot.pool_id == pool_id,
            TrainingCandidatePoolSnapshot.is_locked == 1,
        )
        .order_by(TrainingCandidatePoolSnapshot.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _assert_not_locked(db: Session, pool_id: str, action_zh: str) -> None:
    locked = _locked_snapshot(db, pool_id)
    if locked is not None:
        raise _business_blocked(
            f"该候选池已完成看板分析并被锁定（快照 {locked.id}），"
            f"{action_zh}已禁用。请先在设计稿中执行「清空分析并解锁」后再操作。",
            reason="POOL_LOCKED",
            pool_id=pool_id,
            snapshot_id=locked.id,
        )


def _active_member_count(db: Session, pool_id: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(TrainingCandidatePoolMember)
            .where(
                TrainingCandidatePoolMember.pool_id == pool_id,
                TrainingCandidatePoolMember.is_deleted == 0,
            )
        ).scalar()
        or 0
    )


def _write_audit(
    db: Session,
    *,
    action: str,
    actor: str,
    pool_id: str | None = None,
    before: Any = None,
    after: Any = None,
    attributes: dict[str, Any] | None = None,
) -> int | None:
    """写一条挖掘域审计（AC-18 全写操作审计）。

    与业务写在**同一事务**内（只 `flush`，由调用方 `commit`）——
    业务失败则审计一起回滚，不会留下「有审计无业务」的假记录。

    两个口径（2026-09-17 需求方裁决）：
    1. **`pool_id` 落独立列**（`factor_audit_logs.pool_id`，迁移 0055）——
       原先塞在 `attributes_json`，按池查审计要走 JSON 过滤且无索引
    2. **审计 JSON 保留 None**（`_audit_json`）——`canonical_json` 会静默丢弃
       None，导致「当时该字段是 None」与「该字段未记录」不可区分；
       审计的价值恰在能**还原当时状态**。历史行不动，只影响新写入。
    """
    row = FactorAuditLog(
        action=action,
        factor_set_id=None,          # 候选池不是 FactorSet
        model_run_id=None,
        pool_id=pool_id,             # 独立列（迁移 0055 新增，可空）
        actor=str(actor or "system"),
        before_json=_audit_json(before),
        after_json=_audit_json(after),
        attributes_json=_audit_json(attributes),
    )
    db.add(row)
    db.flush()
    return row.id


def _audit_json(payload: Any) -> str:
    """审计 JSON 序列化：**保留 None**（`_freeze_json` 语义）。

    刻意不用 `canonical_json`：它按设计丢弃值为 None 的键（哈希场景合理，
    见 `app/core/hash_utils.py` 模块 docstring 第 2 条），而审计要保真。
    两者用途不同 —— **算哈希归一、落库保真**（T16 定，手册 §1.17）。
    """
    return _freeze_json({} if payload is None else payload)


def _dedupe_preserving_order(values: Iterable[int]) -> list[int]:
    """去重且保持顺序（不排序：加入顺序会影响列表默认排序的稳定性）。"""
    seen: set[int] = set()
    out: list[int] = []
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _coerce_symbol_ids(symbol_ids: Sequence[int] | None) -> list[int]:
    if symbol_ids is None:
        raise _validation_error("symbol_ids 不能为空。")
    cleaned: list[int] = []
    for raw in symbol_ids:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise _validation_error(
                f"symbol_ids 只接受整数 symbol_id，收到 {raw!r}。",
                invalid_value=repr(raw),
            )
        cleaned.append(raw)
    deduped = _dedupe_preserving_order(cleaned)
    if not deduped:
        raise _validation_error("symbol_ids 不能为空。")
    if len(deduped) > MAX_BATCH:
        raise _validation_error(
            f"单次批量操作上限 {MAX_BATCH} 个标的，本次 {len(deduped)} 个。请分批提交。",
            max_batch=MAX_BATCH,
        )
    return deduped


# ══════════════════════════════════════════════════════════════
# 池 CRUD
# ══════════════════════════════════════════════════════════════


def create_pool(
    db: Session,
    *,
    name: str,
    source_type: str,
    description: str | None = None,
    filter_config: dict[str, Any] | None = None,
    rule_hash: str | None = None,
    import_batch_id: str | None = None,
    created_by: str = "local_user",
    pool_id: str | None = None,
) -> TrainingCandidatePool:
    """创建候选池。

    `source_type` 决定后续互斥入口（`filter` = 条件筛选 / `import` = 外部导入），
    **创建后不可切换**（需求 §3.1）。因此这里对两类入口的必备字段做前置校验，
    避免建出一个「既无规则也无批次」的空壳池。
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise _validation_error("候选池名称不能为空。")
    if len(clean_name) > MAX_NAME_LEN:
        raise _validation_error(f"候选池名称最长 {MAX_NAME_LEN} 字符，本次 {len(clean_name)} 字符。")
    if source_type not in VALID_SOURCE_TYPES:
        raise _validation_error(
            f"source_type 必须是 {sorted(VALID_SOURCE_TYPES)} 之一，收到 {source_type!r}。",
            allowed=sorted(VALID_SOURCE_TYPES),
        )

    if source_type == SOURCE_TYPE_FILTER and not filter_config:
        raise _validation_error(
            "source_type=filter 时必须提供非空的 filter_config（条件筛选规则）。"
            "空规则含义不明确（等价于「全选」还是「不选」？），且会让该池的成员来源不可复现，"
            "故拒绝创建。",
            source_type=source_type,
        )
    if source_type == SOURCE_TYPE_IMPORT and not import_batch_id:
        raise _validation_error(
            "source_type=import 时必须提供 import_batch_id，用于逐行匹配结果落库与溯源。",
            source_type=source_type,
        )

    pid = pool_id or uuid.uuid4().hex
    if db.get(TrainingCandidatePool, pid) is not None:
        raise _validation_error(f"候选池 ID {pid} 已存在。", pool_id=pid)

    pool = TrainingCandidatePool(
        id=pid,
        name=clean_name,
        description=description,
        source_type=source_type,
        filter_config_json=canonical_json(filter_config) if filter_config else None,
        rule_hash=rule_hash,
        import_batch_id=import_batch_id,
        status=POOL_STATUS_DRAFT,
        version=1,
        member_count=0,
        created_by=str(created_by or "system"),
    )
    db.add(pool)
    db.flush()

    _write_audit(
        db,
        action=AUDIT_POOL_CREATED,
        actor=created_by,
        pool_id=pid,
        before={},
        after={
            "pool_id": pid,
            "name": clean_name,
            "source_type": source_type,
            "status": POOL_STATUS_DRAFT,
            "version": 1,
        },
        attributes={"pool_id": pid, "source_type": source_type},
    )
    db.commit()
    db.refresh(pool)
    return pool


def list_pools(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    source_type: str | None = None,
    keyword: str | None = None,
) -> tuple[list[TrainingCandidatePool], int]:
    """分页查询候选池（返回 `(items, total)`）。"""
    if page < 1:
        raise _validation_error("page 必须 >= 1。")
    if page_size < 1 or page_size > 200:
        raise _validation_error("page_size 必须在 1~200 之间。")
    if status is not None and status not in VALID_POOL_STATUSES:
        raise _validation_error(
            f"status 必须是 {sorted(VALID_POOL_STATUSES)} 之一，收到 {status!r}。"
        )
    if source_type is not None and source_type not in VALID_SOURCE_TYPES:
        raise _validation_error(
            f"source_type 必须是 {sorted(VALID_SOURCE_TYPES)} 之一，收到 {source_type!r}。"
        )

    conditions = []
    if status:
        conditions.append(TrainingCandidatePool.status == status)
    if source_type:
        conditions.append(TrainingCandidatePool.source_type == source_type)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        conditions.append(
            or_(TrainingCandidatePool.name.like(like), TrainingCandidatePool.description.like(like))
        )

    total = int(
        db.execute(
            select(func.count()).select_from(TrainingCandidatePool).where(*conditions)
        ).scalar()
        or 0
    )
    items = list(
        db.execute(
            select(TrainingCandidatePool)
            .where(*conditions)
            .order_by(TrainingCandidatePool.created_at.desc(), TrainingCandidatePool.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
    )
    return items, total


def update_pool(
    db: Session,
    pool_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    operator_id: str = "system",
) -> TrainingCandidatePool:
    """更新池的可变字段。

    **`source_type` 不可切换**：传了与现值不同的值 → `BUSINESS_BLOCKED`。
    传了与现值**相同**的值视为幂等（前端表单常把整个对象回传），不报错。
    """
    pool = get_pool(db, pool_id)

    if source_type is not None and source_type != pool.source_type:
        raise _business_blocked(
            f"候选池的 source_type 创建后不可切换（当前 {pool.source_type}，"
            f"请求 {source_type}）。如需另一种入口，请新建候选池。",
            reason="SOURCE_TYPE_IMMUTABLE",
            pool_id=pool_id,
            current_source_type=pool.source_type,
            requested_source_type=source_type,
        )

    if status is not None and status not in VALID_POOL_STATUSES:
        raise _validation_error(
            f"status 必须是 {sorted(VALID_POOL_STATUSES)} 之一，收到 {status!r}。"
        )

    before = {"name": pool.name, "description": pool.description, "status": pool.status}
    changed = False

    if name is not None:
        clean = name.strip()
        if not clean:
            raise _validation_error("候选池名称不能为空。")
        if len(clean) > MAX_NAME_LEN:
            raise _validation_error(f"候选池名称最长 {MAX_NAME_LEN} 字符。")
        if clean != pool.name:
            pool.name = clean
            changed = True
    if description is not None and description != pool.description:
        pool.description = description
        changed = True
    if status is not None and status != pool.status:
        pool.status = status
        changed = True

    if not changed:
        return pool

    pool.version = int(pool.version or 1) + 1
    pool.updated_at = _utcnow()
    db.flush()

    _write_audit(
        db,
        action=AUDIT_POOL_UPDATED,
        actor=operator_id,
        pool_id=pool_id,
        before=before,
        after={"name": pool.name, "description": pool.description, "status": pool.status},
        attributes={"pool_id": pool_id, "version": pool.version},
    )
    db.commit()
    db.refresh(pool)
    return pool


# ══════════════════════════════════════════════════════════════
# 成员：批量添加 / 批量软删除
# ══════════════════════════════════════════════════════════════


def add_members(
    db: Session,
    pool_id: str,
    *,
    symbol_ids: Sequence[int],
    operator_id: str = "system",
    source: str = "manual",
) -> MemberChangeResult:
    """批量加入成员。

    行为口径：
    - **未在池中** → 新增（`added`）
    - **已软删除** → 复活（`reactivated`，重置 `is_deleted`/`deleted_at`）
    - **已在池中且未删除** → 幂等跳过（`skipped_existing`），**不报错**
    - **symbol_id 在 `symbols` 中不存在** → **整批拒绝**（`VALIDATION_ERROR`）

    为什么未知 symbol 整批拒绝而不是部分接受：直接加成员时，ID 来自客户端的候选列表；
    出现未知 ID 说明是客户端 bug 或数据过期，静默丢弃会掩盖问题。
    「逐行匹配结果 / 部分成功」是**导入**流程（T10）的语义，不适用于本入口。
    """
    pool = get_pool(db, pool_id)
    ids = _coerce_symbol_ids(symbol_ids)
    _assert_not_locked(db, pool_id, "批量加入成员")

    known = set(
        db.execute(select(Symbol.id).where(Symbol.id.in_(ids))).scalars()
    )
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise _validation_error(
            f"有 {len(unknown)} 个 symbol_id 在 symbols 主表中不存在：{unknown[:20]}"
            + ("（仅显示前 20 个）" if len(unknown) > 20 else "")
            + "。候选池只做关联，不会为主数据补行。",
            unknown_symbol_ids=unknown[:200],
            unknown_count=len(unknown),
        )

    # 一次性取出现有成员，避免 N 次查询
    existing_rows = list(
        db.execute(
            select(TrainingCandidatePoolMember).where(
                TrainingCandidatePoolMember.pool_id == pool_id,
                TrainingCandidatePoolMember.symbol_id.in_(ids),
            )
        ).scalars()
    )
    by_symbol = {m.symbol_id: m for m in existing_rows}

    # 纳入校验摘要的来源信息（symbol 主数据只读，不回写）
    symbol_meta = {
        s.id: s
        for s in db.execute(select(Symbol).where(Symbol.id.in_(ids))).scalars()
    }

    result = MemberChangeResult(pool_id=pool_id, requested=len(ids), member_count=0)
    now = _utcnow()

    for sid in ids:
        meta = symbol_meta.get(sid)
        summary = canonical_json(
            {
                "source": source,
                "matched": True,
                "symbol": getattr(meta, "symbol", None),
                "asset_type": getattr(meta, "asset_type", None),
                "market": getattr(meta, "market", None),
                "is_st": getattr(meta, "is_st", None),
                "is_active": getattr(meta, "is_active", None),
                "checked_at": now.isoformat(),
            }
        )
        row = by_symbol.get(sid)
        if row is None:
            db.add(
                TrainingCandidatePoolMember(
                    pool_id=pool_id,
                    symbol_id=sid,
                    inclusion_summary_json=summary,
                    included_at=now,
                    is_deleted=0,
                    deleted_at=None,
                )
            )
            result.added += 1
            result.changed_symbol_ids.append(sid)
        elif row.is_deleted:
            row.is_deleted = 0
            row.deleted_at = None
            row.included_at = now
            row.inclusion_summary_json = summary
            result.reactivated += 1
            result.changed_symbol_ids.append(sid)
        else:
            result.skipped_existing += 1

    db.flush()

    if result.changed == 0:
        # 幂等：没有任何变更 → 不写审计、不 bump 版本
        result.member_count = _active_member_count(db, pool_id)
        db.commit()
        return result

    count = _active_member_count(db, pool_id)
    pool.member_count = count
    pool.version = int(pool.version or 1) + 1
    pool.updated_at = now
    db.flush()

    result.member_count = count
    result.audit_event_id = _write_audit(
        db,
        action=AUDIT_POOL_MEMBERS_ADDED,
        actor=operator_id,
        pool_id=pool_id,
        before={"member_count": count - result.added - result.reactivated},
        after={"member_count": count},
        attributes={
            "pool_id": pool_id,
            "source": source,
            "requested": len(ids),
            "added": result.added,
            "reactivated": result.reactivated,
            "skipped_existing": result.skipped_existing,
            "symbol_ids": result.changed_symbol_ids[:2000],
            "version": pool.version,
        },
    )
    db.commit()
    return result


def remove_members(
    db: Session,
    pool_id: str,
    *,
    symbol_ids: Sequence[int],
    operator_id: str = "system",
    reason: str | None = None,
) -> MemberChangeResult:
    """批量**软删除**成员。

    ⚠️ 不变式：**只断关联，不删主数据**。`symbols` / 行情 / 财报一行都不动
    （需求 §3.8、向导 §3.6：删除操作必须二次确认并写审计 —— 二次确认在前端；
    服务端负责写入可追溯的审计记录，含操作者、标的清单、原因）。

    不在池中 / 已删除的 ID 计入 `not_in_pool`（幂等，不报错）。
    池被锁定（存在 `is_locked=1` 的快照）时拒绝。
    """
    pool = get_pool(db, pool_id)
    ids = _coerce_symbol_ids(symbol_ids)
    _assert_not_locked(db, pool_id, "批量删除成员")

    rows = list(
        db.execute(
            select(TrainingCandidatePoolMember).where(
                TrainingCandidatePoolMember.pool_id == pool_id,
                TrainingCandidatePoolMember.symbol_id.in_(ids),
                TrainingCandidatePoolMember.is_deleted == 0,
            )
        ).scalars()
    )

    result = MemberChangeResult(pool_id=pool_id, requested=len(ids), member_count=0)
    now = _utcnow()
    for row in rows:
        row.is_deleted = 1
        row.deleted_at = now
        result.removed += 1
        result.changed_symbol_ids.append(row.symbol_id)

    result.not_in_pool = len(ids) - result.removed
    db.flush()

    if result.removed == 0:
        result.member_count = _active_member_count(db, pool_id)
        db.commit()
        return result

    count = _active_member_count(db, pool_id)
    pool.member_count = count
    pool.version = int(pool.version or 1) + 1
    pool.updated_at = now
    db.flush()

    result.member_count = count
    result.audit_event_id = _write_audit(
        db,
        action=AUDIT_POOL_MEMBERS_SOFT_DELETED,
        actor=operator_id,
        pool_id=pool_id,
        before={"member_count": count + result.removed},
        after={"member_count": count},
        attributes={
            "pool_id": pool_id,
            "requested": len(ids),
            "removed": result.removed,
            "not_in_pool": result.not_in_pool,
            "symbol_ids": result.changed_symbol_ids[:2000],
            "reason": reason,
            # 明确写进审计：主数据未受影响，便于事后核对「只断关联」这一不变式
            "master_data_untouched": True,
            "version": pool.version,
        },
    )
    db.commit()
    return result


def list_members(
    db: Session,
    pool_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    keyword: str | None = None,
    include_deleted: bool = False,
    order_by: str = "included_at",
    descending: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """成员列表（join `symbols` 取展示字段；**只读主数据，不回写**）。"""
    get_pool(db, pool_id)  # 池不存在 → NOT_FOUND
    if page < 1:
        raise _validation_error("page 必须 >= 1。")
    if page_size < 1 or page_size > 500:
        raise _validation_error("page_size 必须在 1~500 之间。")

    conditions = [TrainingCandidatePoolMember.pool_id == pool_id]
    if not include_deleted:
        conditions.append(TrainingCandidatePoolMember.is_deleted == 0)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        conditions.append(or_(Symbol.symbol.like(like), Symbol.name.like(like)))

    order_map = {
        "included_at": TrainingCandidatePoolMember.included_at,
        "symbol": Symbol.symbol,
        "symbol_id": TrainingCandidatePoolMember.symbol_id,
        "name": Symbol.name,
        "industry": Symbol.industry,
    }
    if order_by not in order_map:
        raise _validation_error(
            f"order_by 必须是 {sorted(order_map)} 之一，收到 {order_by!r}。"
        )
    order_col = order_map[order_by]
    order_expr = order_col.desc() if descending else order_col.asc()

    total = int(
        db.execute(
            select(func.count())
            .select_from(TrainingCandidatePoolMember)
            .join(Symbol, Symbol.id == TrainingCandidatePoolMember.symbol_id, isouter=True)
            .where(*conditions)
        ).scalar()
        or 0
    )

    rows = db.execute(
        select(TrainingCandidatePoolMember, Symbol)
        .join(Symbol, Symbol.id == TrainingCandidatePoolMember.symbol_id, isouter=True)
        .where(*conditions)
        .order_by(order_expr, TrainingCandidatePoolMember.symbol_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items: list[dict[str, Any]] = []
    for member, symbol in rows:
        items.append(
            {
                "member_id": member.id,
                "pool_id": member.pool_id,
                "symbol_id": member.symbol_id,
                "symbol": getattr(symbol, "symbol", None),
                "name": getattr(symbol, "name", None),
                "asset_type": getattr(symbol, "asset_type", None),
                "market": getattr(symbol, "market", None),
                "board": getattr(symbol, "board", None),
                "industry": getattr(symbol, "industry", None),
                "is_st": getattr(symbol, "is_st", None),
                "is_active": getattr(symbol, "is_active", None),
                "included_at": member.included_at,
                "is_deleted": member.is_deleted,
                "deleted_at": member.deleted_at,
            }
        )
    return items, total


def get_pool_detail(db: Session, pool_id: str) -> dict[str, Any]:
    """池详情 + 成员统计（`GET /candidate-pools/{id}` 的载荷）。"""
    pool = get_pool(db, pool_id)
    active = _active_member_count(db, pool_id)
    soft_deleted = int(
        db.execute(
            select(func.count())
            .select_from(TrainingCandidatePoolMember)
            .where(
                TrainingCandidatePoolMember.pool_id == pool_id,
                TrainingCandidatePoolMember.is_deleted == 1,
            )
        ).scalar()
        or 0
    )
    locked = _locked_snapshot(db, pool_id)
    return {
        "id": pool.id,
        "name": pool.name,
        "description": pool.description,
        "source_type": pool.source_type,
        "status": pool.status,
        "version": pool.version,
        "member_count": active,
        "member_count_cached": pool.member_count,
        "soft_deleted_count": soft_deleted,
        "created_by": pool.created_by,
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
        "is_locked": bool(locked),
        "locked_snapshot_id": locked.id if locked else None,
    }


# ══════════════════════════════════════════════════════════════
# 快照冻结 / 重置（T11）
# ══════════════════════════════════════════════════════════════

#: 快照分析状态机（`analysis_status` 列）
SNAPSHOT_NOT_ANALYZED = "not_analyzed"
SNAPSHOT_ANALYZING = "analyzing"
SNAPSHOT_ANALYZED = "analyzed"
SNAPSHOT_RESET = "reset"
VALID_SNAPSHOT_STATUSES: frozenset[str] = frozenset(
    {SNAPSHOT_NOT_ANALYZED, SNAPSHOT_ANALYZING, SNAPSHOT_ANALYZED, SNAPSHOT_RESET}
)

AUDIT_SNAPSHOT_FROZEN = "pool_snapshot_frozen"
AUDIT_SNAPSHOT_RESET = "pool_snapshot_reset"


def list_snapshots(db: Session, pool_id: str) -> list[TrainingCandidatePoolSnapshot]:
    """该池全部快照，新→旧。池不存在则抛 `NOT_FOUND`。"""
    get_pool(db, pool_id)
    return list(
        db.execute(
            select(TrainingCandidatePoolSnapshot)
            .where(TrainingCandidatePoolSnapshot.pool_id == pool_id)
            .order_by(TrainingCandidatePoolSnapshot.created_at.desc())
        ).scalars()
    )


def get_latest_snapshot(
    db: Session, pool_id: str
) -> TrainingCandidatePoolSnapshot | None:
    """该池最新一份快照（含已 reset 的 —— 历史必须可追溯）。"""
    return next(iter(list_snapshots(db, pool_id)), None)


def _freeze_member_rows(db: Session, pool_id: str) -> list[dict[str, Any]]:
    """把当前**有效成员**连同其主数据展示字段一次性读出，用于冻结。

    只读主数据（join `symbols`），不做任何回写 —— 隔离模型不变式。
    """
    rows = db.execute(
        select(TrainingCandidatePoolMember, Symbol)
        .join(Symbol, Symbol.id == TrainingCandidatePoolMember.symbol_id)
        .where(
            TrainingCandidatePoolMember.pool_id == pool_id,
            TrainingCandidatePoolMember.is_deleted == 0,
        )
        .order_by(TrainingCandidatePoolMember.id)
    ).all()
    now = _utcnow()
    frozen: list[dict[str, Any]] = []
    for member, sym in rows:
        frozen.append(
            {
                "symbol_id": int(member.symbol_id),
                "symbol": str(sym.symbol),
                "name": None if sym.name is None else str(sym.name),
                "market": None if sym.market is None else str(sym.market),
                "board": None if sym.board is None else str(sym.board),
                "asset_type": None if sym.asset_type is None else str(sym.asset_type),
                #: 行业**当前标签**（实测全空）；快照记录 observed_at，
                #: 严禁用于历史 PIT / 中性化（见 `mining_candidate_pool.py` 模块 docstring）
                "industry": None if sym.industry is None else str(sym.industry),
                "industry_observed_at": now.isoformat(),
                "included_at": member.included_at.isoformat()
                if member.included_at
                else None,
            }
        )
    return frozen


def _freeze_json(payload: Any) -> str:
    """冻结数据的序列化：**保留 None**、键排序、紧凑分隔。

    ⚠️ 为什么不用 `canonical_json`：实测它会**静默丢弃所有 None 值**
    （`{"industry": None}` → `{}`）。这对**哈希**是合理设计（None 与缺失等价，
    避免两种写法产生不同 hash），但对**冻结落库**是数据丢失 —— 快照要能
    原样还原当时看到了什么。行业列实测全空（None），用 canonical_json 会把
    `industry` 这个 key 整个吃掉，下游拿不到「字段存在但无值」的信号。

    这条已写进 MEMORY / 手册跑偏表：**落库用 `_freeze_json`，算哈希才用 `canonical_json`**。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def freeze_snapshot(
    db: Session,
    *,
    pool_id: str,
    data_cutoff_at: datetime | None = None,
    operator_id: str = "system",
) -> TrainingCandidatePoolSnapshot:
    """冻结候选池：把**当前有效成员**写入一份不可变快照。

    - `data_cutoff_at` 缺省取 `_utcnow()`；语义是「数据截止时刻」，分析据此取数
    - 快照生成后**不会**自动锁定 —— 锁定由「分析完成」触发（`mark_analyzed`），
      与向导 §3.7.1 的「分析完成 → 锁定」一致
    - 同一池可多次冻结（每次挖掘独立快照，需求 §3.8），**不**删除旧快照
    """
    pool = get_pool(db, pool_id)
    _assert_not_locked(db, pool_id, "重新冻结快照")

    members = _freeze_member_rows(db, pool_id)
    if len(members) < MIN_POOL_SIZE:
        raise _business_blocked(
            f"候选池有效标的 {len(members)} 只，低于硬下限 {MIN_POOL_SIZE} 只，不能冻结快照。"
            "50 是后端硬校验，不能通过前端参数降低。",
            reason="POOL_TOO_SMALL",
            pool_id=pool_id,
            hits=len(members),
            min_pool_size=MIN_POOL_SIZE,
        )

    cutoff = data_cutoff_at or _utcnow()
    snapshot = TrainingCandidatePoolSnapshot(
        id=uuid.uuid4().hex,
        pool_id=pool_id,
        members_json=_freeze_json(members),
        rule_hash=pool.rule_hash or "",
        data_cutoff_at=cutoff,
        stats_json=_freeze_json({"member_count": len(members)}),
        analysis_status=SNAPSHOT_NOT_ANALYZED,
        member_count=len(members),
        is_locked=0,
        #: 行业快照：全部成员的当前行业标签（实测全空 → None），
        #: 仅供展示；严禁用于历史 PIT / 中性化
        industry_value=None,
        industry_source="symbols.industry(current)",
        industry_observed_at=_utcnow(),
    )
    db.add(snapshot)
    db.flush()

    _write_audit(
        db,
        action=AUDIT_SNAPSHOT_FROZEN,
        actor=operator_id,
        pool_id=pool_id,
        before={},
        after={
            "snapshot_id": snapshot.id,
            "pool_id": pool_id,
            "member_count": len(members),
            "data_cutoff_at": cutoff.isoformat(),
        },
        attributes={"pool_id": pool_id, "snapshot_id": snapshot.id},
    )
    db.commit()
    db.refresh(snapshot)
    return snapshot


def mark_analyzed(
    db: Session, *, snapshot_id: str, analysis: dict[str, Any],
    operator_id: str = "system",
) -> TrainingCandidatePoolSnapshot:
    """把分析结果写入快照并把状态置为 `analyzed` + 锁定。

    锁定在**分析完成**时发生（向导 §3.7.1 / §3.7.3），不是冻结时。
    """
    snapshot = db.get(TrainingCandidatePoolSnapshot, snapshot_id)
    if snapshot is None:
        raise _pool_not_found(snapshot_id)
    snapshot.analysis_json = _freeze_json(analysis)
    snapshot.analysis_status = SNAPSHOT_ANALYZED
    snapshot.analyzed_at = _utcnow()
    snapshot.is_locked = 1
    db.flush()

    _write_audit(
        db,
        action="pool_snapshot_analyzed",
        actor=operator_id,
        pool_id=snapshot.pool_id,
        before={"analysis_status": SNAPSHOT_NOT_ANALYZED, "is_locked": 0},
        after={"analysis_status": SNAPSHOT_ANALYZED, "is_locked": 1},
        attributes={"pool_id": snapshot.pool_id, "snapshot_id": snapshot_id},
    )
    db.commit()
    db.refresh(snapshot)
    return snapshot


def reset_analysis(
    db: Session, *, pool_id: str, operator_id: str = "system"
) -> dict[str, Any]:
    """「重新选择」：删除该池**最新**快照（连同分析结果与锁定）。

    向导 §3.7.3：确认后「删除分析数据，释放筛选/导入功能，回到初始状态」。
    锁定在快照上，删快照即自动解锁 —— 不需要单独的 unlock 动作。

    只删**最新**一份：历史快照是已挖掘任务的引用物，删了会破坏溯源
    （`factor_mining_runs.snapshot_id` 外键）。
    """
    pool = get_pool(db, pool_id)
    snapshots = list_snapshots(db, pool_id)
    if not snapshots:
        raise _business_blocked(
            f"候选池 {pool_id} 没有可重置的快照（尚未生成挖掘物料）。",
            reason="NO_SNAPSHOT",
            pool_id=pool_id,
        )
    latest = snapshots[0]
    before = {
        "snapshot_id": latest.id,
        "analysis_status": latest.analysis_status,
        "member_count": latest.member_count,
        "is_locked": int(latest.is_locked or 0),
    }
    db.delete(latest)
    db.flush()

    # 池状态回 draft（若曾因分析被置为 frozen）
    if pool.status == POOL_STATUS_FROZEN:
        pool.status = POOL_STATUS_DRAFT
        pool.version = (pool.version or 1) + 1

    _write_audit(
        db,
        action=AUDIT_SNAPSHOT_RESET,
        actor=operator_id,
        pool_id=pool_id,
        before=before,
        after={"remaining_snapshots": len(snapshots) - 1},
        attributes={"pool_id": pool_id, "snapshot_id": before["snapshot_id"]},
    )
    db.commit()
    return {
        "pool_id": pool_id,
        "removed_snapshot_id": before["snapshot_id"],
        "remaining_snapshots": len(snapshots) - 1,
        "is_locked": _locked_snapshot(db, pool_id) is not None,
    }


def snapshot_to_dict(
    db: Session, snapshot: TrainingCandidatePoolSnapshot
) -> dict[str, Any]:
    """快照 → 展示 dict（`analysis_json` 已反序列化，前端可直接渲染看板）。"""
    members = json.loads(snapshot.members_json) if snapshot.members_json else []

    def _load(raw: str | None) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 - 脏数据不应让看板 500
            return None

    return {
        "snapshot_id": snapshot.id,
        "pool_id": snapshot.pool_id,
        "member_count": snapshot.member_count,
        "member_count_in_json": len(members),
        "data_cutoff_at": snapshot.data_cutoff_at,
        "analysis_status": snapshot.analysis_status,
        "analyzed_at": snapshot.analyzed_at,
        "is_locked": bool(snapshot.is_locked),
        "rule_hash": snapshot.rule_hash,
        "stats": _load(snapshot.stats_json),
        "analysis": _load(snapshot.analysis_json),
        "industry": {
            "value": snapshot.industry_value,
            "source": snapshot.industry_source,
            "observed_at": snapshot.industry_observed_at,
            "note_zh": (
                "行业为 `symbols.industry` 当前标签（实测全空）；"
                "严禁用于历史 PIT / 行业中性化。"
            ),
        },
        "members_sample": members[:20],
        "created_at": snapshot.created_at,
    }


__all__ = [
    "AUDIT_POOL_CREATED",
    "AUDIT_POOL_MEMBERS_ADDED",
    "AUDIT_POOL_MEMBERS_SOFT_DELETED",
    "AUDIT_POOL_UPDATED",
    "MAX_BATCH",
    "MIN_POOL_SIZE",
    "MemberChangeResult",
    "POOL_STATUS_DRAFT",
    "POOL_STATUS_FROZEN",
    "POOL_STATUS_INVALIDATED",
    "POOL_STATUS_NEEDS_RECHECK",
    "SOURCE_TYPE_FILTER",
    "SOURCE_TYPE_IMPORT",
    "VALID_POOL_STATUSES",
    "VALID_SOURCE_TYPES",
    "add_members",
    "create_pool",
    "get_pool",
    "get_pool_detail",
    "list_members",
    "list_pools",
    "remove_members",
    "update_pool",
    # T11 快照
    "SNAPSHOT_NOT_ANALYZED",
    "SNAPSHOT_ANALYZING",
    "SNAPSHOT_ANALYZED",
    "SNAPSHOT_RESET",
    "VALID_SNAPSHOT_STATUSES",
    "AUDIT_SNAPSHOT_FROZEN",
    "AUDIT_SNAPSHOT_RESET",
    "list_snapshots",
    "get_latest_snapshot",
    "freeze_snapshot",
    "mark_analyzed",
    "reset_analysis",
    "snapshot_to_dict",
]
