"""向导草稿服务（SD-v2.0 §6.15；任务 T16）。

职责
====
- 草稿 CRUD：5 步**部分/完整**配置 + 当前步骤 + 状态
  （`save_draft` / `get_draft` / `list_drafts`）
- 草稿 → `config_hash`（复用 `config_hash.py`，**唯一实现**）
- 字段校验运行记录（`factor_data_validation_runs`）：
  **同一 draft_id + config_hash 的活动任务唯一**（幂等复用）
- 「去修复数据」回流：把草稿标记为 `waiting_data_recheck`（向导 §5.1）

零迁移
======
`factor_mining_drafts` 与 `factor_data_validation_runs` 两张表由 T01 迁移
（0053）建好、模型在 `app/models/factor_mining.py` —— T16 不需要新迁移。

状态取值（来自模型注释，**不得自造**）
- 草稿：`draft` / `waiting_data_recheck` / `invalidated`
- 校验运行：`queued` / `running` / `passed` / `blocked` / `warning` / `failed`
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.factor_mining import FactorDataValidationRun, FactorMiningDraft
from app.services.factors.mining import config_hash as CH

logger = logging.getLogger(__name__)

# ── 草稿状态 ──
DRAFT_STATUS_DRAFT = "draft"
DRAFT_STATUS_WAITING_RECHECK = "waiting_data_recheck"
DRAFT_STATUS_INVALIDATED = "invalidated"
VALID_DRAFT_STATUSES: frozenset[str] = frozenset({
    DRAFT_STATUS_DRAFT, DRAFT_STATUS_WAITING_RECHECK, DRAFT_STATUS_INVALIDATED,
})

# ── 校验运行状态 ──
RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_PASSED = "passed"
RUN_BLOCKED = "blocked"
RUN_WARNING = "warning"
RUN_FAILED = "failed"
VALID_RUN_STATUSES: frozenset[str] = frozenset({
    RUN_QUEUED, RUN_RUNNING, RUN_PASSED, RUN_BLOCKED, RUN_WARNING, RUN_FAILED,
})

#: 「活动」状态 = 正在占用幂等位的状态（同 draft+hash 只允许一条）
ACTIVE_RUN_STATUSES: tuple[str, ...] = (RUN_QUEUED, RUN_RUNNING)

#: 校验报告有效期（与 T14 `VALIDATION_TTL_HOURS` 一致：需求 §3.5 默认 24h）
VALIDATION_TTL_HOURS = 24

STEP_MIN = 1
STEP_MAX = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dumps(payload: Any) -> str | None:
    """落库序列化：**保留 None**（与 `config_hash.strip_none` 相反）。

    理由同 T11：落库要能原样还原「当时配置里这个字段是空的」，
    而算哈希时 None 与缺失等价。两者用途不同。
    """
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def _loads(raw: str | None, fallback: Any = None) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("draft json 解析失败，按空处理: %s", raw[:80])
        return fallback


# ══════════════════════════════════════════════════════════
# 草稿 CRUD
# ══════════════════════════════════════════════════════════


@dataclass
class DraftView:
    """草稿的只读视图（`to_dict` 直接给前端）。"""

    id: str
    name: str | None
    status: str
    current_step: int
    steps: dict[str, Any]
    candidate_pool_snapshot_id: str | None
    owner: str
    config_hash: str
    created_at: datetime | None
    updated_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.id,
            "name": self.name,
            "status": self.status,
            "current_step": self.current_step,
            "steps": self.steps,
            "candidate_pool_snapshot_id": self.candidate_pool_snapshot_id,
            "owner": self.owner,
            "config_hash": self.config_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _to_view(row: FactorMiningDraft) -> DraftView:
    steps = {
        "step1": _loads(row.step1_json),
        "step2": _loads(row.step2_json),
        "step3": _loads(row.step3_json),
        "step4": _loads(row.step4_json),
    }
    return DraftView(
        id=row.id, name=row.name, status=row.status,
        current_step=int(row.current_step or 1), steps=steps,
        candidate_pool_snapshot_id=row.candidate_pool_snapshot_id,
        owner=row.owner,
        config_hash=CH.compute_config_hash(CH.steps_to_config(steps)),
        created_at=row.created_at, updated_at=row.updated_at,
    )


def get_draft(db: Session, *, draft_id: str) -> DraftView | None:
    row = db.get(FactorMiningDraft, draft_id)
    return _to_view(row) if row is not None else None


def list_drafts(db: Session, *, owner: str | None = None, limit: int = 20,
                status: str | None = None) -> list[DraftView]:
    stmt = select(FactorMiningDraft)
    if owner:
        stmt = stmt.where(FactorMiningDraft.owner == owner)
    if status:
        stmt = stmt.where(FactorMiningDraft.status == status)
    stmt = stmt.order_by(FactorMiningDraft.updated_at.desc()).limit(int(limit))
    return [_to_view(r) for r in db.execute(stmt).scalars().all()]


def save_draft(db: Session, *, payload: Mapping[str, Any]) -> DraftView:
    """保存草稿（无 `draft_id` 则新建；有则更新**部分**字段）。

    - 只更新 payload 里出现的步骤 → 支持「暂存编辑」（校验进行中也能改配置，
      需求 §3.5）；未出现的步骤原样保留。
    - `current_step` 越界 → 钳到 [1, 5]。
    - 状态变更走 `set_draft_status`（本函数不隐式改状态，除新建时置 draft）。
    """
    data = dict(payload or {})
    draft_id = data.get("draft_id") or data.get("id")
    steps = dict(data.get("steps") or {})
    # 也接受扁平写法 {"step1": {...}}
    for key in CH.DRAFT_STEP_KEYS:
        if key in data and data[key] is not None:
            steps[key] = data[key]

    row = db.get(FactorMiningDraft, draft_id) if draft_id else None
    if row is None:
        row = FactorMiningDraft(
            id=draft_id or uuid.uuid4().hex,
            name=data.get("name"),
            status=DRAFT_STATUS_DRAFT,
            current_step=1,
            owner=str(data.get("owner") or "local_user"),
        )
        db.add(row)

    if data.get("name") is not None:
        row.name = str(data["name"])
    if data.get("candidate_pool_snapshot_id") is not None:
        row.candidate_pool_snapshot_id = str(data["candidate_pool_snapshot_id"])
    if data.get("owner") is not None:
        row.owner = str(data["owner"])
    if data.get("current_step") is not None:
        row.current_step = max(STEP_MIN, min(STEP_MAX, int(data["current_step"])))

    for key in CH.DRAFT_STEP_KEYS:
        if key in steps:
            setattr(row, f"{key}_json", _dumps(steps[key]))

    status = data.get("status")
    if status is not None:
        if status not in VALID_DRAFT_STATUSES:
            raise ValueError(
                f"非法草稿状态 {status!r}；允许 {sorted(VALID_DRAFT_STATUSES)}。")
        row.status = status

    db.flush()
    db.commit()
    db.refresh(row)
    return _to_view(row)


def set_draft_status(db: Session, *, draft_id: str, status: str) -> DraftView:
    """显式改状态（校验用，避免 save_draft 隐式改）。"""
    if status not in VALID_DRAFT_STATUSES:
        raise ValueError(
            f"非法草稿状态 {status!r}；允许 {sorted(VALID_DRAFT_STATUSES)}。")
    row = db.get(FactorMiningDraft, draft_id)
    if row is None:
        raise ValueError(f"草稿不存在: {draft_id}")
    row.status = status
    db.commit()
    db.refresh(row)
    return _to_view(row)


def mark_waiting_data_recheck(db: Session, *, draft_id: str) -> DraftView:
    """「去修复数据」（向导 §5.1）：把草稿标记为待修复后重校验。

    数据修复完成**不由系统猜测**；用户从修复页返回后由前端再次触发校验，
    届时 `config_hash` 会重新生成（向导 §5.1 明说）。
    """
    return set_draft_status(db, draft_id=draft_id,
                            status=DRAFT_STATUS_WAITING_RECHECK)


def draft_config_hash(db: Session, *, draft_id: str) -> str:
    """草稿当前配置的哈希（Step1~Step3）。"""
    view = get_draft(db, draft_id=draft_id)
    if view is None:
        raise ValueError(f"草稿不存在: {draft_id}")
    return view.config_hash


# ══════════════════════════════════════════════════════════
# 校验运行：幂等复用
# ══════════════════════════════════════════════════════════


@dataclass
class ValidationRunView:
    id: str
    draft_id: str
    config_hash: str
    task_id: str | None
    status: str
    total_shards: int
    done_shards: int
    expires_at: datetime | None
    report: dict[str, Any] | None
    blockers: list[Any] | None
    warnings: list[Any] | None
    shard_state: dict[str, Any] | None
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "draft_id": self.draft_id,
            "config_hash": self.config_hash,
            "task_id": self.task_id,
            "status": self.status,
            "total_shards": self.total_shards,
            "done_shards": self.done_shards,
            "expires_at": self.expires_at,
            "report": self.report,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "shard_state": self.shard_state,
            "reused": self.reused,
        }


def _run_to_view(row: FactorDataValidationRun, *, reused: bool = False
                 ) -> ValidationRunView:
    return ValidationRunView(
        id=row.id, draft_id=row.draft_id, config_hash=row.config_hash,
        task_id=row.task_id, status=row.status,
        total_shards=int(row.total_shards or 0),
        done_shards=int(row.done_shards or 0),
        expires_at=row.expires_at,
        report=_loads(row.report_json), blockers=_loads(row.blockers_json),
        warnings=_loads(row.warnings_json), shard_state=_loads(row.shard_state_json),
        reused=reused,
    )


def find_active_validation_run(
    db: Session, *, draft_id: str, config_hash: str,
) -> FactorDataValidationRun | None:
    """查同 draft + hash 的**活动**校验运行（queued/running）。"""
    return db.execute(
        select(FactorDataValidationRun).where(
            FactorDataValidationRun.draft_id == draft_id,
            FactorDataValidationRun.config_hash == config_hash,
            FactorDataValidationRun.status.in_(list(ACTIVE_RUN_STATUSES)),
        ).order_by(FactorDataValidationRun.created_at.asc()).limit(1)
    ).scalar_one_or_none()


def create_or_reuse_validation_run(
    db: Session, *, draft_id: str, config_hash: str | None = None,
    fields: Sequence[Any] | None = None, task_id: str | None = None,
    total_shards: int = 0, status: str = RUN_QUEUED,
    ttl_hours: int = VALIDATION_TTL_HOURS,
) -> ValidationRunView:
    """创建校验运行；**同 draft_id + config_hash 的活动运行已存在则复用**。

    这是需求 §3.5「配置哈希去重」与模型注释「同一 draft_id + config_hash 的
    活动任务唯一」的落地点。终态运行**不复用**（修复数据后重新校验是合法诉求）。
    """
    if status not in VALID_RUN_STATUSES:
        raise ValueError(
            f"非法校验状态 {status!r}；允许 {sorted(VALID_RUN_STATUSES)}。")
    if config_hash is None:
        config_hash = draft_config_hash(db, draft_id=draft_id)

    existing = find_active_validation_run(db, draft_id=draft_id,
                                          config_hash=config_hash)
    if existing is not None:
        return _run_to_view(existing, reused=True)

    row = FactorDataValidationRun(
        id=uuid.uuid4().hex,
        draft_id=draft_id,
        config_hash=config_hash,
        task_id=task_id,
        status=status,
        fields_json=_dumps(list(fields) if fields is not None else None),
        total_shards=int(total_shards or 0),
        done_shards=0,
        expires_at=_utcnow() + timedelta(hours=int(ttl_hours)),
    )
    db.add(row)
    db.flush()
    db.commit()
    db.refresh(row)
    return _run_to_view(row)


def get_validation_run(db: Session, *, run_id: str) -> ValidationRunView | None:
    row = db.get(FactorDataValidationRun, run_id)
    return _run_to_view(row) if row is not None else None


def latest_validation_run(db: Session, *, draft_id: str
                          ) -> ValidationRunView | None:
    row = db.execute(
        select(FactorDataValidationRun)
        .where(FactorDataValidationRun.draft_id == draft_id)
        .order_by(FactorDataValidationRun.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    return _run_to_view(row) if row is not None else None


def update_validation_run(
    db: Session, *, run_id: str, status: str | None = None,
    task_id: str | None = None, done_shards: int | None = None,
    total_shards: int | None = None, shard_state: Any = None,
    report: Any = None, blockers: Any = None, warnings: Any = None,
) -> ValidationRunView:
    """推进校验运行（worker 每完成一片调用一次；`shard_state` 即断点续跑依据）。"""
    if status is not None and status not in VALID_RUN_STATUSES:
        raise ValueError(
            f"非法校验状态 {status!r}；允许 {sorted(VALID_RUN_STATUSES)}。")
    row = db.get(FactorDataValidationRun, run_id)
    if row is None:
        raise ValueError(f"校验运行不存在: {run_id}")
    if status is not None:
        row.status = status
    if task_id is not None:
        row.task_id = task_id
    if done_shards is not None:
        row.done_shards = int(done_shards)
    if total_shards is not None:
        row.total_shards = int(total_shards)
    if shard_state is not None:
        row.shard_state_json = _dumps(shard_state)
    if report is not None:
        row.report_json = _dumps(report)
    if blockers is not None:
        row.blockers_json = _dumps(blockers)
    if warnings is not None:
        row.warnings_json = _dumps(warnings)
    db.commit()
    db.refresh(row)
    return _run_to_view(row)


def is_validation_run_valid(db: Session, *, run_id: str,
                            now: datetime | None = None) -> bool:
    """报告是否在有效期内（过期后 Step4 提交被阻断的依据）。

    已过期或非通过类终态 → False。
    """
    row = db.get(FactorDataValidationRun, run_id)
    if row is None:
        return False
    if row.status not in (RUN_PASSED, RUN_WARNING):
        return False
    if row.expires_at is None:
        return False
    return (now or _utcnow()) <= row.expires_at


__all__ = [
    "DRAFT_STATUS_DRAFT",
    "DRAFT_STATUS_WAITING_RECHECK",
    "DRAFT_STATUS_INVALIDATED",
    "VALID_DRAFT_STATUSES",
    "RUN_QUEUED",
    "RUN_RUNNING",
    "RUN_PASSED",
    "RUN_BLOCKED",
    "RUN_WARNING",
    "RUN_FAILED",
    "VALID_RUN_STATUSES",
    "ACTIVE_RUN_STATUSES",
    "VALIDATION_TTL_HOURS",
    "STEP_MIN",
    "STEP_MAX",
    "DraftView",
    "ValidationRunView",
    "get_draft",
    "list_drafts",
    "save_draft",
    "set_draft_status",
    "mark_waiting_data_recheck",
    "draft_config_hash",
    "find_active_validation_run",
    "create_or_reuse_validation_run",
    "get_validation_run",
    "latest_validation_run",
    "update_validation_run",
    "is_validation_run_valid",
]
