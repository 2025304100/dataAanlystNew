"""字段异步校验服务（SD-v2.0 §6.15 / 需求 §3.5 / 向导 §5；任务 T14）。

核心契约
========
1. **幂等复用**：同一 `draft_id + config_hash` 的**活动**任务（queued/running）
   唯一 —— 已存在则复用返回，不重复创建（需求 §3.5「配置哈希去重」）。
2. **分片续跑**：校验按**字段**分片（需求 §3.5「按字段/时间段分片」，M1a 先做
   字段维，时间段维预留 `time_buckets` 参数）；每完成一片立即落盘进度，
   续跑时跳过已完成分片，**绝不重复**。
3. **报告必须保存**（向导 §5.1「不允许只在前端临时展示」）：全量报告写
   `async_task_records.result_json`，含 `valid_until`（默认 24h，需求硬规则）。
4. **不占 `mining_domain`**（任务卡「明确不做」）：校验只**读** DuckDB，
   连 `duckdb_write` 都不需要（§7.1：该锁是「所有**写** DuckDB 的任务」）。

零迁移的持久化方案
==================
T14 没有模型/迁移写权限，校验任务全部复用 `async_task_records`：
- 任务记录：`task_type="factor_mining_validation"`，
  payload 带 `draft_id` / `config_hash` / 分片清单
- 分片进度：`batch_recovery_json`（既有列，语义就是「批次恢复」）——
  每完成一片原子更新一次
- 报告：`result_json`（Text，报告全文 + `valid_until`）

> 为什么不用 `service._freeze_json`（T11 的教训）：这里存的字段里同样有大量
> None（未执行分片的 finished_at 等），**必须保留 key** —— 用 `_freeze_json`
> 同款实现（本地复制一份，避免跨域 import candidate_pool）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.services import task_state_machine as TSM
from app.services import async_tasks as AT
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
)

logger = logging.getLogger(__name__)

#: 异步任务类型
TASK_TYPE = "factor_mining_validation"

#: 字段校验有效期（需求 §3.5 硬规则：默认 24 小时）
VALIDATION_TTL_HOURS = 24

#: 分片状态
SHARD_PENDING = "pending"
SHARD_DONE = "done"
SHARD_FAILED = "failed"

#: 总体结论
VERDICT_PASS = "pass"
VERDICT_WARN = "warn"
VERDICT_BLOCK = "block"

_SHARD_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _freeze_json(payload: Any) -> str:
    """保留 None 的紧凑序列化（与 `candidate_pool.service._freeze_json` 同款；
    本地复制以避免跨域 import —— 见 T11 对 canonical_json 丢 None 的教训）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def compute_config_hash(config: Mapping[str, Any] | None) -> str:
    """配置哈希（幂等复用的键；None 与缺失等价 —— 哈希语义用 canonical 风格）。"""
    def _strip(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _strip(v) for k, v in o.items() if v is not None}
        if isinstance(o, list):
            return [_strip(v) for v in o]
        return o

    payload = json.dumps(_strip(dict(config or {})), ensure_ascii=False,
                         separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════
# 分片
# ══════════════════════════════════════════════════════════


@dataclass
class ValidationShard:
    """一个分片 = 一个字段的一次校验（时间段维 `bucket` 预留，M1a 恒为 "all"）。"""

    field: str
    bucket: str = "all"
    status: str = SHARD_PENDING
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def key(self) -> str:
        return f"{self.field}::{self.bucket}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field, "bucket": self.bucket, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "result": self.result, "error": self.error,
        }


def build_shards(fields: Sequence[Mapping[str, Any] | str],
                 *, time_buckets: Sequence[str] = ("all",),
                 ) -> list[ValidationShard]:
    """字段 × 时间段 展开为分片清单（顺序稳定，续跑按 key 匹配）。"""
    shards: list[ValidationShard] = []
    names = [f if isinstance(f, str) else str(f.get("field")) for f in fields]
    for name in names:
        for bucket in time_buckets:
            shards.append(ValidationShard(field=name, bucket=bucket))
    return shards


def _shards_from_payload(payload: Mapping[str, Any]) -> list[ValidationShard]:
    out: list[ValidationShard] = []
    for raw in payload.get("shards") or []:
        out.append(ValidationShard(
            field=str(raw.get("field")), bucket=str(raw.get("bucket") or "all"),
            status=str(raw.get("status") or SHARD_PENDING),
            started_at=raw.get("started_at"), finished_at=raw.get("finished_at"),
            result=raw.get("result") or {}, error=raw.get("error"),
        ))
    return out


# ══════════════════════════════════════════════════════════
# 默认分片检查器（真实 DuckDB 元数据检查；测试可注入替换）
# ══════════════════════════════════════════════════════════

#: 判定为「覆盖率偏低（警告）」的阈值
WARN_COVERAGE = 0.80


def _default_field_checker(shard: ValidationShard, ctx: Mapping[str, Any]) -> dict[str, Any]:
    """对单个字段做 DuckDB 元数据检查（只读，不占任何锁）。

    检查项（向导 §5.1 第 2/3 项的字段级部分）：
    物理表存在性、行数、首末日期、非空覆盖率、最新日期距数据截止日的 staleness。
    """
    from app.services.factors.candidate_pool import rules as pool_rules

    binding = pool_rules.FIELD_BINDINGS.get(shard.field)
    if binding is None:
        return {"verdict": VERDICT_BLOCK, "reason_zh": f"字段 {shard.field} 未注册。"}
    if binding.blocked:
        return {"verdict": VERDICT_BLOCK, "reason_zh": binding.blocked_reason_zh or "",
                "physical_table": binding.physical_table}
    if not binding.physical_table or not binding.physical_column:
        return {"verdict": VERDICT_BLOCK, "reason_zh": "字段缺少物理绑定。"}

    warehouse = ctx.get("warehouse") or pool_rules._default_warehouse(ctx.get("db"))
    with warehouse.connection(read_only=True) as conn:
        table = binding.physical_table
        col = binding.physical_column
        date_col = "trade_date" if table != "raw_financial_reports" else "announcement_date"
        row = conn.execute(
            f"SELECT COUNT(*), COUNT({col}), MIN({date_col}), MAX({date_col}) "
            f"FROM {table}"
        ).fetchone()
    total, nonnull, min_d, max_d = row
    coverage = (nonnull / total) if total else 0.0
    verdict = VERDICT_PASS if coverage >= WARN_COVERAGE else VERDICT_WARN
    return {
        "verdict": verdict,
        "physical_table": table,
        "physical_column": col,
        "date_column": date_col,
        "total_rows": int(total or 0),
        "non_null_rows": int(nonnull or 0),
        "coverage": round(coverage, 6),
        "min_date": str(min_d) if min_d else None,
        "max_date": str(max_d) if max_d else None,
    }


# ══════════════════════════════════════════════════════════
# 创建 / 复用
# ══════════════════════════════════════════════════════════


def _find_active(db: Any, *, draft_id: str, config_hash: str) -> AsyncTaskRecord | None:
    rows = db.execute(
        select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_type == TASK_TYPE,
            AsyncTaskRecord.status.in_(["queued", "running"]),
        ).order_by(AsyncTaskRecord.created_at.asc())
    ).scalars().all()
    for row in rows:
        try:
            payload = json.loads(row.payload_json) if row.payload_json else {}
        except ValueError:
            continue
        if payload.get("draft_id") == draft_id and \
                payload.get("config_hash") == config_hash:
            return row
    return None


def create_or_reuse_validation(
    *, draft_id: str, config: Mapping[str, Any] | None,
    fields: Sequence[Mapping[str, Any] | str],
    operator_id: str = "system",
    field_checker: Callable[[ValidationShard, Mapping[str, Any]], dict[str, Any]]
    | None = None,
    warehouse: Any = None,
) -> dict[str, Any]:
    """创建（或幂等复用）一次字段校验任务。

    Returns:
        `{task_id, reused, config_hash, total_shards, status}`。
        `reused=True` 表示复用了活动任务（**不重复创建**）。
    """
    if not draft_id:
        raise ValueError("draft_id 不能为空。")
    if not fields:
        raise ValueError("fields 不能为空（至少勾选一个字段）。")

    config_hash = compute_config_hash(config)
    shards = build_shards(fields)

    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        existing = _find_active(db, draft_id=draft_id, config_hash=config_hash)
        if existing is not None:
            return {
                "task_id": existing.id, "reused": True,
                "config_hash": config_hash,
                "total_shards": len(_shards_from_payload(
                    json.loads(existing.payload_json or "{}"))),
                "status": existing.status,
            }
    finally:
        db.close()

    payload = {
        "draft_id": draft_id,
        "config_hash": config_hash,
        "config": dict(config or {}),
        "fields": [f if isinstance(f, str) else dict(f) for f in fields],
        "shards": [s.to_dict() for s in shards],
        "operator_id": operator_id,
    }
    task = create_async_task(TASK_TYPE, payload, use_control_plane=True,
                             force_new=True)
    _start_worker(task.id, _make_worker(field_checker, warehouse))
    return {
        "task_id": task.id, "reused": False, "config_hash": config_hash,
        "total_shards": len(shards), "status": task.status,
    }


def _make_worker(field_checker, warehouse) -> Callable[[str], None]:
    def _worker(task_id: str) -> None:
        run_validation_worker(task_id, field_checker=field_checker,
                              warehouse=warehouse)
    return _worker


# ══════════════════════════════════════════════════════════
# worker：分片执行 + 断点续跑
# ══════════════════════════════════════════════════════════


def run_validation_worker(
    task_id: str, *,
    field_checker: Callable[[ValidationShard, Mapping[str, Any]], dict[str, Any]]
    | None = None,
    warehouse: Any = None,
) -> None:
    """校验 worker：逐分片执行，**每片落盘一次进度**（断点续跑的依据）。

    分片失败不中断整体（该分片标 failed 继续下一片）——
    报告要如实反映全部字段的状态，不能因一个字段崩掉整场校验。
    """
    checker = field_checker or _default_field_checker
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status in ("cancelled", "done"):
            return
        payload = json.loads(task.payload_json) if task.payload_json else {}
        shards = _shards_from_payload(payload)
        draft_id = str(payload.get("draft_id") or "")

        TSM.transition(task_id, "running", db=db)
        _set_task(db, task_id, status="running", stage="validating",
                  message=f"0/{len(shards)} shards", started_at=_now())

        # 续跑：跳过已完成分片（batch_recovery_json 是唯一进度事实源）
        recovery = json.loads(task.batch_recovery_json) if task.batch_recovery_json else {}
        done_keys = {k for k, v in (recovery.get("shards") or {}).items()
                     if v.get("status") == SHARD_DONE}

        ctx = {"db": db, "warehouse": warehouse}
        failures: list[str] = []
        for idx, shard in enumerate(shards):
            db.expire_all()
            current = db.get(AsyncTaskRecord, task_id)
            if current is None or current.status == "cancelled":
                return
            # 🚨 cancel_async_task 对 running 任务只置 cancel_requested=1，
            #    由 worker 自检自止（async_tasks 既有语义，不改）。T14 第一版
            #    只查了全局 stop 事件 —— 用户点「暂停」会永远停不下来。
            if int(getattr(current, "cancel_requested", 0) or 0) == 1:
                _set_task(db, task_id, status="cancelled", stage="cancelled",
                          message=f"paused by user at {idx}/{len(shards)}",
                          finished_at=_now())
                return
            if AT.is_worker_stop_requested(task_id=task_id):
                _set_task(db, task_id, status="cancelled", stage="cancelled",
                          message=f"paused at {idx}/{len(shards)}",
                          finished_at=_now())
                return
            if shard.key() in done_keys:
                continue                      # 断点续跑：不重复已完成分片

            shard.started_at = _utcnow().isoformat()
            try:
                with _SHARD_LOCK:
                    shard.result = checker(shard, ctx) or {}
                shard.status = SHARD_DONE
            except Exception as exc:  # noqa: BLE001 - 单分片失败不拖垮全场
                shard.status = SHARD_FAILED
                shard.error = f"{type(exc).__name__}: {exc}"[:500]
                failures.append(shard.key())

            _persist_progress(db, task_id, shards, idx=idx + 1, total=len(shards))

        verdict, summary = _summarize(shards, failures)
        now = _utcnow()
        report = {
            "draft_id": draft_id,
            "config_hash": payload.get("config_hash"),
            "verdict": verdict,
            "verdict_label_zh": {VERDICT_PASS: "通过", VERDICT_WARN: "警告",
                                 VERDICT_BLOCK: "阻断"}[verdict],
            "summary_zh": summary,
            "total_shards": len(shards),
            "failed_shards": failures,
            "shards": [s.to_dict() for s in shards],
            "valid_until": (now + timedelta(hours=VALIDATION_TTL_HOURS)).isoformat(),
            "ttl_hours": VALIDATION_TTL_HOURS,
            "generated_at": now.isoformat(),
        }
        _set_task(
            db, task_id,
            status="done", stage="validated", percent=100,
            message=f"{verdict}: {summary}",
            result_json=_freeze_json(report),
            finished_at=_now(),
        )
    finally:
        db.close()


def _persist_progress(db: Any, task_id: str, shards: Sequence[ValidationShard],
                      *, idx: int, total: int) -> None:
    """每完成一片立即落盘（独立 commit —— 续跑的进度事实源）。"""
    with _SHARD_LOCK:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        recovery = json.loads(task.batch_recovery_json) if task.batch_recovery_json else {}
        by_key = {f"{s['field']}::{s.get('bucket') or 'all'}": s
                  for s in (recovery.get("shards") or {}).values()}
        for s in shards:
            if s.status != SHARD_PENDING:
                by_key[s.key()] = s.to_dict()
        task.batch_recovery_json = _freeze_json(
            {"shards": by_key, "completed": len(by_key), "total": total})
        task.processed = idx
        task.percent = int(idx * 100 / total) if total else 100
        task.message = f"{idx}/{total} shards"
        db.commit()


def _summarize(shards: Sequence[ValidationShard],
               failures: Sequence[str]) -> tuple[str, str]:
    verdicts = [str(s.result.get("verdict") or VERDICT_PASS)
                for s in shards if s.status == SHARD_DONE]
    if failures or VERDICT_BLOCK in verdicts:
        return VERDICT_BLOCK, (
            f"{len(failures)} 个分片执行失败，"
            f"{verdicts.count(VERDICT_BLOCK)} 个字段阻断" if failures
            else f"{verdicts.count(VERDICT_BLOCK)} 个字段阻断")
    if VERDICT_WARN in verdicts:
        return VERDICT_WARN, f"{verdicts.count(VERDICT_WARN)} 个字段覆盖率偏低（警告）"
    return VERDICT_PASS, f"全部 {len(shards)} 个字段通过"


# ══════════════════════════════════════════════════════════
# 查询 / 有效期 / 续跑入口
# ══════════════════════════════════════════════════════════


def get_validation_progress(db: Any, task_id: str) -> dict[str, Any]:
    """进度 + 报告 + 有效期（报告**从库里读**，不允许只在前端）。"""
    task = db.get(AsyncTaskRecord, task_id)
    if task is None or task.task_type != TASK_TYPE:
        return {"found": False}
    payload = json.loads(task.payload_json) if task.payload_json else {}
    recovery = json.loads(task.batch_recovery_json) if task.batch_recovery_json else {}
    report = json.loads(task.result_json) if task.result_json else None
    return {
        "found": True,
        "task_id": task.id,
        "draft_id": payload.get("draft_id"),
        "config_hash": payload.get("config_hash"),
        "status": task.status,
        "completed_shards": int(recovery.get("completed") or 0),
        "total_shards": len(payload.get("shards") or []),
        "valid_until": (report or {}).get("valid_until"),
        "report": report,
    }


def is_validation_valid(db: Any, task_id: str, *, now: datetime | None = None) -> bool:
    """校验报告是否在 24h 有效期内（过期后 Step4 提交被阻断的依据）。"""
    task = db.get(AsyncTaskRecord, task_id)
    if task is None or task.status != "done" or not task.result_json:
        return False
    try:
        report = json.loads(task.result_json)
    except ValueError:
        return False
    raw = report.get("valid_until")
    if not raw:
        return False
    try:
        valid_until = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    return (now or _utcnow()) <= valid_until


def resume_validation(
    *, task_id: str, field_checker=None, warehouse: Any = None,
    operator_id: str = "system",
) -> dict[str, Any]:
    """断点续跑入口：对 failed/cancelled 任务**以相同 payload 新建任务**，
    已完成分片由 batch_recovery_json 天然跳过（不重复执行）。

    为什么新建而不是复活旧任务：`async_tasks` 终态受 `is_terminal_locked` 保护
    （既有语义，不改）；新任务继承同一 payload → 同一 config_hash →
    报告可追溯到同一配置。
    """
    from app.db.session import get_session_local

    db = get_session_local()()
    try:
        old = db.get(AsyncTaskRecord, task_id)
        if old is None or old.task_type != TASK_TYPE:
            raise ValueError(f"校验任务不存在: {task_id}")
        payload = json.loads(old.payload_json) if old.payload_json else {}
    finally:
        db.close()

    task = create_async_task(TASK_TYPE, payload, use_control_plane=True,
                             force_new=True)
    _start_worker(task.id, _make_worker(field_checker, warehouse))
    return {"task_id": task.id, "resumed_from": task_id,
            "config_hash": payload.get("config_hash"),
            "total_shards": len(payload.get("shards") or [])}


__all__ = [
    "TASK_TYPE",
    "VALIDATION_TTL_HOURS",
    "SHARD_PENDING",
    "SHARD_DONE",
    "SHARD_FAILED",
    "VERDICT_PASS",
    "VERDICT_WARN",
    "VERDICT_BLOCK",
    "WARN_COVERAGE",
    "ValidationShard",
    "compute_config_hash",
    "build_shards",
    "create_or_reuse_validation",
    "run_validation_worker",
    "get_validation_progress",
    "is_validation_valid",
    "resume_validation",
    "_persist_progress",
]
