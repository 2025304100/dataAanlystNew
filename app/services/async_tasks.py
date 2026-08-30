"""通用异步任务服务层。

提供异步任务的创建、查询、取消等生命周期管理，
以及后台线程执行器的启动。任务状态持久化到 async_tasks 表。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.db.session import get_session_local

logger = logging.getLogger(__name__)

# 超过 30 分钟无更新的 running/queued 任务自动标记为 failed
STALE_RUNNING_DEADLINE = timedelta(minutes=30)

# 8 位 hex correlation_id 正则（用于验证已有 corr_id 格式）
_CORR_ID_RE = re.compile(r"^[0-9a-fA-F]{8}$")

# 同一进程内串行化带幂等键的创建，避免 SQLite/非唯一索引场景下的
# "先查询后插入"竞争；跨进程竞争仍由数据库唯一约束 + IntegrityError 回读兜底。
_IDEMPOTENT_CREATE_LOCK = threading.RLock()

# A3. Graceful worker shutdown signals.
WORKER_STOP_EVENT = threading.Event()
_WORKER_THREADS = dict()
_WORKER_THREADS_GUARD = threading.Lock()


def request_all_workers_stop():
    WORKER_STOP_EVENT.set()


def wait_workers_stopped(timeout_seconds):
    deadline = _now() + timedelta(seconds=timeout_seconds)
    remaining = float(timeout_seconds)
    with _WORKER_THREADS_GUARD:
        threads = list(_WORKER_THREADS.values())
    for t in threads:
        if remaining <= 0: break
        t.join(timeout=remaining)
        used = max(0.01, float(timeout_seconds) - max(0.0, (deadline - _now()).total_seconds()))
        remaining = max(0.0, float(timeout_seconds) - used)
    with _WORKER_THREADS_GUARD:
        alive = any(t.is_alive() for t in _WORKER_THREADS.values())
    return not alive


def is_worker_stop_requested(task_id=None):
    return bool(WORKER_STOP_EVENT.is_set())



def _gen_correlation_id() -> str:
    return uuid4().hex[:8]


def _classify_exception_code(exc: BaseException) -> tuple[str, str, str]:
    """从异常类型/信息分类出结构化错误 code + category + title_zh（中文标题），
    重点识别 MySQL 2013(连接断开) / 1452(外键失败)。"""
    msg = str(exc)
    if isinstance(exc, OperationalError):
        if "2013" in msg or "Lost connection" in msg:
            return (
                "eval.db.mysql_lost_connection_2013",
                "infra",
                "数据库连接断开（MySQL 2013 Lost connection）",
            )
        if "2006" in msg or "MySQL server has gone away" in msg:
            return (
                "eval.db.mysql_gone_away_2006",
                "infra",
                "数据库连接已回收（MySQL 2006 Gone away）",
            )
        if "1205" in msg or "Lock wait timeout" in msg:
            return (
                "eval.db.lock_wait_timeout_1205",
                "infra",
                "数据库锁等待超时（MySQL 1205）",
            )
        return (
            "eval.db.operational_error",
            "infra",
            "数据库运行时错误",
        )
    if isinstance(exc, IntegrityError):
        if "1452" in msg or "foreign key constraint" in msg.lower():
            return (
                "eval.db.foreign_key_violation_1452",
                "data",
                "外键约束失败（MySQL 1452）：缺少主记录或版本未创建",
            )
        if "1062" in msg or "Duplicate entry" in msg:
            return (
                "eval.db.duplicate_entry_1062",
                "config",
                "唯一键冲突（MySQL 1062 Duplicate）",
            )
        return (
            "eval.db.integrity_error",
            "data",
            "数据完整性错误",
        )
    if isinstance(exc, TimeoutError):
        return ("eval.infra.timeout", "infra", "任务执行超时")
    if isinstance(exc, MemoryError):
        return ("eval.infra.oom", "infra", "内存不足（Worker OOM）")
    if isinstance(exc, ModuleNotFoundError) or isinstance(exc, ImportError):
        return ("eval.infra.import_error", "infra", "依赖缺失/导入失败")
    return ("eval.worker.crashed", "infra", "Worker 进程意外退出（未捕获异常）")


def normalize_error(err_in: dict | None | list | str, *, fallback_cid: str | None = None) -> dict:
    """兜底归一化：确保错误对象必含 6 字段（code/severity/category/title_zh/detail_zh/correlation_id）。
    - correlation_id 必须是 8 位 hex（已有有效则保留，否则生成新的）
    - 无论输入是 dict/list/str/None，都返回符合前端契约的 dict
    """
    cid = fallback_cid or _gen_correlation_id()

    raw: dict = {}
    if isinstance(err_in, dict):
        raw = dict(err_in)
    elif isinstance(err_in, (list, tuple)):
        # 错误地写成了 list 的，退化成第一个元素或转成 string
        try:
            head = err_in[0] if len(err_in) > 0 else None
            if isinstance(head, dict):
                raw = dict(head)
            else:
                raw = {"detail_zh": json.dumps(err_in, ensure_ascii=False, default=str)}
        except Exception:
            raw = {"detail_zh": str(err_in)}
    elif isinstance(err_in, str):
        # 门禁历史原因常用 ``code:detail`` 字符串保存。保留 code，
        # 否则所有原因都会被错误标成 eval.worker.unknown，前端无法定位。
        code_match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*):(.*)$", err_in, re.DOTALL)
        if code_match:
            raw = {
                "code": code_match.group(1),
                "category": "gate",
                "detail_zh": code_match.group(2) or code_match.group(1),
            }
        else:
            raw = {"detail_zh": err_in}
    elif err_in is None:
        raw = {}

    existing_cid = raw.get("correlation_id") or (
        (raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}).get("correlation_id")
        if isinstance(raw.get("evidence"), dict)
        else None
    )
    if isinstance(existing_cid, str) and _CORR_ID_RE.match(existing_cid):
        cid = existing_cid
    # 强制归一化成 8 hex
    if not _CORR_ID_RE.match(cid or ""):
        cid = _gen_correlation_id()

    evidence: dict = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    evidence = dict(evidence)
    evidence.setdefault("correlation_id", cid)

    code = raw.get("code")
    if not isinstance(code, str) or not code.strip():
        code = "eval.worker.unknown"
    code = code.strip()

    severity = raw.get("severity")
    if severity == "warn":
        severity = "warning"
    if severity not in ("error", "warning", "info", "pass"):
        severity = raw.get("level")  # 兼容一些历史写法
        if severity not in ("error", "warning", "info", "pass"):
            severity = "error"

    category = raw.get("category")
    if not isinstance(category, str) or not category.strip():
        category = "infra"

    title_zh = raw.get("title_zh") or raw.get("title") or raw.get("name")
    if not isinstance(title_zh, str) or not title_zh.strip():
        title_zh = code

    detail_zh = raw.get("detail_zh") or raw.get("detail") or raw.get("message") or raw.get("msg")
    if not isinstance(detail_zh, str) or not detail_zh.strip():
        detail_zh = title_zh

    out: dict = {
        "code": code,
        "severity": severity,
        "category": category,
        "title_zh": title_zh,
        "detail_zh": detail_zh,
        "correlation_id": cid,
        "evidence": evidence,
    }
    for optional_key in ("fix_link", "retryable", "id", "title", "detail", "source"):
        if optional_key in raw and raw[optional_key] is not None:
            out[optional_key] = raw[optional_key]
    return out


def normalize_errors(errors_in, *, fallback_cid: str | None = None) -> list[dict]:
    """归一化一批 errors（rejection_reasons 也用这个）。"""
    cid_pool = fallback_cid or _gen_correlation_id()
    if errors_in is None:
        return []
    if isinstance(errors_in, str):
        try:
            parsed = json.loads(errors_in)
        except Exception:
            parsed = errors_in
        return normalize_errors(parsed, fallback_cid=cid_pool)
    if isinstance(errors_in, dict):
        return [normalize_error(errors_in, fallback_cid=cid_pool)]
    if isinstance(errors_in, (list, tuple)):
        shared_cid = cid_pool
        out: list[dict] = []
        for item in errors_in:
            n = normalize_error(item, fallback_cid=shared_cid)
            out.append(n)
            if shared_cid == cid_pool and n.get("correlation_id"):
                shared_cid = str(n["correlation_id"])
        return out
    # 兜底（str/bool/int/float 等）
    return [normalize_error({"detail_zh": str(errors_in)}, fallback_cid=cid_pool)]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json_loads(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to timestamps stored as legacy naive UTC values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _task_to_dict(task: AsyncTaskRecord) -> dict:
    """将 ORM 对象转为前端可用的字典，用于 AsyncTaskRead 构建。"""
    errors = _json_loads(task.errors_json, [])
    # WPD-05: 从第一条 error 提取 error_code 到顶层，作为前端 i18n 映射的稳定事实来源
    top_error_code = None
    if errors and isinstance(errors[0], dict):
        top_error_code = errors[0].get("error_code")
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "stage": task.stage,
        "percent": round(task.percent, 1),
        "message": task.message or "",
        "total": task.total,
        "processed": task.processed,
        "ok_count": task.ok_count,
        "failed_count": task.failed_count,
        "current_item": task.current_item,
        "result": _json_loads(task.result_json, None),
        "errors": errors[-20:],
        "error_code": top_error_code,
        "created_at": _as_utc(task.created_at),
        "started_at": _as_utc(task.started_at),
        "finished_at": _as_utc(task.finished_at),
        "updated_at": _as_utc(task.updated_at),
        # WP-S.5 任务防卡死状态机扩展字段（追加在末尾，不影响现有契约）
        "heartbeat_at": _as_utc(task.heartbeat_at),
        "stage_budget_seconds": task.stage_budget_seconds,
        "stage_started_at": _as_utc(task.stage_started_at),
        "last_progress_at": _as_utc(task.last_progress_at),
        "last_progress_percent": task.last_progress_percent,
        "current_step_description": task.current_step_description,
        "suggested_action": task.suggested_action,
        "batch_recovery": _json_loads(task.batch_recovery_json, None),
        "last_patrol_at": _as_utc(task.last_patrol_at),
        "cancel_requested": bool(task.cancel_requested or False),
        # WP5: 幂等检查需要原始 payload JSON
        "payload_json": task.payload_json,
        # WP5: fingerprint 留空，由调用方在特定 task_type 下填充
        "fingerprint": None,
        # FR-P1-2 可靠性：cid/幂等/终态锁/取消超时
        "correlation_id": task.correlation_id,
        "idempotency_key": task.idempotency_key,
        "is_terminal_locked": bool(getattr(task, "is_terminal_locked", 0) or False),
        "cancelled_timeout_at": _as_utc(getattr(task, "cancelled_timeout_at", None)),
    }


def _task_to_read(task: AsyncTaskRecord) -> AsyncTaskRead:
    return AsyncTaskRead.model_validate(_task_to_dict(task))


def _expire_stale_tasks(db: Session) -> None:
    """将超时未更新的任务标记为 failed。

    FR-P1-2 强化：
    - 仅处理 is_terminal_locked!= 1 的任务（终态已锁定不得再改 status）。
    - 写入 status 之后立即把 is_terminal_locked 置 1。
    """
    cutoff = _now() - STALE_RUNNING_DEADLINE
    rows = (
        db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.status.in_(("queued", "running")),
                AsyncTaskRecord.updated_at < cutoff,
                (AsyncTaskRecord.is_terminal_locked == 0) | (AsyncTaskRecord.is_terminal_locked.is_(None)),
            )
        )
        .scalars()
        .all()
    )
    for task in rows:
        task.status = "failed"
        task.stage = "failed"
        task.message = "Task expired (no update for 30 minutes)"
        task.finished_at = _now()
        task.is_terminal_locked = 1
    if rows:
        db.commit()


def _run_patrol(db: Session) -> None:
    """best-effort 执行巡检，让 interrupted/stalled 优先于 30 分钟兜底失败被识别。

    巡检逻辑由 `app.services.task_state_machine.patrol_interrupted_and_stalled`
    提供，更精细（heartbeat + worker 线程存活 + 阶段预算 + 进度停滞）。
    异常仅记录日志，不掩盖后续 _expire_stale_tasks。
    """
    try:
        from app.services.task_state_machine import patrol_interrupted_and_stalled
        patrol_interrupted_and_stalled(db)
    except Exception:
        logger.exception("patrol_interrupted_and_stalled failed; continuing with _expire_stale_tasks")



def mark_graceful_shutdown_tasks(db):
    rows = (
        db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.status.in_(("queued", "running")),
                (AsyncTaskRecord.is_terminal_locked == 0) | (AsyncTaskRecord.is_terminal_locked.is_(None)),
            )
        )
        .scalars()
        .all()
    )
    ts = _now()
    for task in rows:
        cid = task.correlation_id or _gen_correlation_id()
        _append_error(task, {
            "code": "BACKEND_GRACEFUL_SHUTDOWN_ACK",
            "stage": task.stage or "running",
            "error": "Backend graceful shutdown stopped this in-process worker before completion.",
            "correlation_id": cid,
        })
        task.status = "failed"
        task.stage = "graceful_interrupted"
        task.message = "Backend graceful shutdown interrupted task before completion"
        task.finished_at = ts
        task.updated_at = ts
        task.correlation_id = cid
        task.is_terminal_locked = 1
    if rows:
        db.commit()
    return [task.id for task in rows]


def interrupt_orphaned_async_tasks(db: Session) -> list[str]:
    """Fail queued/running in-process tasks left behind by a backend restart.

    FR-P1-2 强化：仅处理 is_terminal_locked!= 1 的任务；写入终态立即锁定。
    """
    rows = (
        db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.status.in_(("queued", "running")),
                (AsyncTaskRecord.is_terminal_locked == 0) | (AsyncTaskRecord.is_terminal_locked.is_(None)),
            )
        )
        .scalars()
        .all()
    )
    interrupted_at = _now()
    for task in rows:
        previous_stage = task.stage
        _append_error(
            task,
            {
                "code": "BACKEND_RESTART_INTERRUPTED",
                "stage": previous_stage,
                "error": "Backend restarted before the in-process worker completed",
                "correlation_id": task.correlation_id or _gen_correlation_id(),
            },
        )
        task.status = "failed"
        task.stage = "interrupted"
        task.message = "Backend restarted before task completed"
        task.finished_at = interrupted_at
        task.updated_at = interrupted_at
        task.is_terminal_locked = 1
    if rows:
        db.commit()
    return [task.id for task in rows]


def _set_task(db: Session, task_id: str, **updates) -> AsyncTaskRecord:
    """原子更新任务字段并提交。

    FR-P1-2 终态保护（对齐 project_memory #8）：
    - 若 is_terminal_locked == 1（SQL/ORM 层判定），禁止修改 status/stage/is_terminal_locked。
      （避免 worker 线程/巡检/恢复覆盖终态，导致订单/持仓终态不一致。）
    - 若本次更新把 status 置为 done/failed/cancelled 终态，则同步设置
      is_terminal_locked=1 +（若无 correlation_id）生成 correlation_id 归档。
    """
    try:
        from sqlalchemy.exc import PendingRollbackError
        db.connection()
    except PendingRollbackError:
        try:
            db.rollback()
        except Exception:
            pass
    except Exception:
        pass
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        raise ValueError(f"Async task not found: {task_id}")

    # 终态硬锁：任何线程/巡检/恢复都不得改动 status/stage/终态锁
    if int(getattr(task, "is_terminal_locked", 0) or 0) == 1:
        updates = {
            k: v for k, v in updates.items()
            if k not in ("status", "stage", "is_terminal_locked")
        }
        if not updates:
            return task
    else:
        # 非锁定：如果原有 status 已是终态（虽 is_terminal_locked 未锁），仍按旧逻辑保护 status/stage
        if task.status in ("done", "failed", "cancelled"):
            updates = {k: v for k, v in updates.items() if k not in ("status", "stage")}

    for key, value in updates.items():
        setattr(task, key, value)

    # 自动锁终态：进入 done/failed/cancelled 即 is_terminal_locked=1，并归档 correlation_id
    if task.status in ("done", "failed", "cancelled"):
        if int(getattr(task, "is_terminal_locked", 0) or 0) != 1:
            task.is_terminal_locked = 1
        if not task.correlation_id:
            task.correlation_id = _gen_correlation_id()

    task.updated_at = _now()
    db.commit()
    db.refresh(task)
    return task


def _append_error(task: AsyncTaskRecord, error: dict) -> None:
    """追加错误到 errors_json，保留最近 20 条。"""
    errors = normalize_errors(_json_loads(task.errors_json, []))
    errors.append(normalize_error(error))
    task.errors_json = json.dumps(errors[-20:], ensure_ascii=False, default=str)


def _start_worker(task_id: str, worker_func) -> None:
    """启动守护线程执行 worker 函数。

    FR-P1-2 强化：Worker crash 时：
    - 若 is_terminal_locked==1 则不覆盖（_set_task 内部已保证，这里前置加速退出）。
    - 把 correlation_id 写回 task.correlation_id 以便跨表查询。
    - 进入终态后 is_terminal_locked 自动置 1。
    """
    def _run():
        try:
            worker_func(task_id)
        except Exception as exc:
            cid = _gen_correlation_id()
            logger.exception("Worker crashed for task %s [cid=%s]", task_id, cid)
            # 尝试标记任务为 failed
            try:
                SessionLocal = get_session_local()
                db = SessionLocal()
                try:
                    task = db.get(AsyncTaskRecord, task_id)
                    if task and int(getattr(task, "is_terminal_locked", 0) or 0) != 1 and task.status not in ("done", "failed", "cancelled"):
                        code, category, title_zh = _classify_exception_code(exc)
                        msg_limited = str(exc)
                        if len(msg_limited) > 4000:
                            msg_limited = msg_limited[:4000] + "...(truncated)"
                        detail_zh = (
                            f"[correlation_id={cid}] "
                            f"异常类型：{type(exc).__name__}\n"
                            f"异常信息：{msg_limited}\n"
                            f"\n排查建议：先复制 correlation_id 在 async_task_records.errors_json 或后端日志中按 cid={cid} 全局搜索；"
                            f"如果是数据库错误（2013/2006/1452/1062），请检查数据库连接池、主记录是否存在、幂等去重逻辑。"
                        )
                        err_item = normalize_error({
                            "code": code,
                            "severity": "error",
                            "category": category,
                            "title_zh": title_zh,
                            "detail_zh": detail_zh,
                            "correlation_id": cid,
                            "evidence": {
                                "correlation_id": cid,
                                "exception_type": type(exc).__name__,
                                "exception_module": getattr(type(exc), "__module__", ""),
                                "exception_msg": msg_limited,
                            },
                            "retryable": code in (
                                "eval.db.mysql_lost_connection_2013",
                                "eval.db.mysql_gone_away_2006",
                                "eval.db.lock_wait_timeout_1205",
                                "eval.infra.timeout",
                            ),
                        }, fallback_cid=cid)
                        task.status = "failed"
                        task.stage = "failed"
                        task.message = f"Worker crashed: {code} [cid={cid}]"[:500]
                        prev = normalize_errors(_json_loads(task.errors_json, []))
                        prev.append(err_item)
                        task.errors_json = json.dumps(prev[-50:], ensure_ascii=False, default=str)
                        rj = _json_loads(task.result_json, {})
                        if isinstance(rj, dict):
                            rj.setdefault("correlation_id", cid)
                            rj.setdefault("worker_crashed", True)
                            rj.setdefault("exception_type", type(exc).__name__)
                            rj.setdefault("exception_msg", msg_limited)
                            task.result_json = json.dumps(rj, ensure_ascii=False, default=str)
                        task.finished_at = _now()
                        task.updated_at = _now()
                        # FR-P1-2：归档 correlation_id + 终态锁
                        if not task.correlation_id:
                            task.correlation_id = cid
                        task.is_terminal_locked = 1
                        db.commit()
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
            except Exception:
                logger.exception("Failed to mark task %s as failed after crash", task_id)

    # A3. Register worker thread for graceful shutdown wait.
    with _WORKER_THREADS_GUARD:
        existing = _WORKER_THREADS.pop(task_id, None)
        if existing is not None:
            try: existing.join(timeout=0.1)
            except Exception: pass
    thread = threading.Thread(target=_run, daemon=True)
    with _WORKER_THREADS_GUARD:
        _WORKER_THREADS[task_id] = thread
    thread.start()


# ── 公共 API ──────────────────────────────────────────────


def _compute_idempotency_key(task_type: str, payload: dict) -> str | None:
    """按 AC-10 约定生成幂等键（task_type + portfolio_id + date + payload_hash + cid）。

    仅当 payload 可识别业务上下文时生成（含 portfolio_id / date / start_date 等）；
    否则返回 None，调用方在不幂等保护的任务上不设此字段。
    """
    try:
        pid = payload.get("portfolio_id") if isinstance(payload, dict) else None
        d = payload.get("date") or payload.get("start_date") if isinstance(payload, dict) else None
        if isinstance(d, (datetime, date)):
            d = d.isoformat()
        elif d is not None:
            d = str(d)
        if d is None and isinstance(payload, dict):
            for k in ("trade_date", "target_date", "asof_date", "data_cutoff_date"):
                if k in payload and payload[k] is not None:
                    d = payload[k].isoformat() if isinstance(payload[k], (datetime, date)) else str(payload[k])
                    break
        payload_canon = json.dumps(
            payload if isinstance(payload, dict) else {},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        ph = hashlib.sha1(payload_canon.encode("utf-8")).hexdigest()[:16]
        return f"{task_type}|{pid}|{d or ''}|{ph}"
    except Exception:
        return None


def _pick_session_factory(use_control_plane: bool):
    """根据 use_control_plane 标志选择 SessionFactory：
    - False → 主 QueuePool（数据面重任务 / 长事务用）
    - True  → NullPool 控制平面：提交 / 心跳 / 列表 等对延迟敏感的轻量接口，
               不参与主 30 连接大池排队，避免触发前端 axios 90s 超时。
    """
    if use_control_plane:
        from app.db.session import get_control_session_local
        return get_control_session_local()
    return get_session_local()


def create_async_task(
    task_type: str,
    payload: dict,
    *,
    use_control_plane: bool = False,
    force_new: bool = False,
) -> AsyncTaskRead:
    """创建异步任务并启动后台 worker（需调用方传入 worker_func 并自行调用 _start_worker）。

    此函数仅创建 DB 记录，不启动线程。
    返回任务快照供调用方立即响应前端。

    FR-P1-2 强化：
    - 创建时生成 correlation_id（用于跨 outbox/decision/audit 串联）。
    - 按业务上下文生成 idempotency_key（AC-10 重复投递 0 重复下单）。
    - 初始化 is_terminal_locked=0。

    use_control_plane：提交请求是轻 INSERT，传 True 可绕过主连接池排队。
    """
    SessionLocal = _pick_session_factory(use_control_plane)
    db = SessionLocal()
    try:
        _run_patrol(db)
        _expire_stale_tasks(db)
        idempotency_key = _compute_idempotency_key(task_type, payload)
        # 显式重跑仍保留业务 payload，但使用一次性幂等键，避免底层
        # AC-10 去重逻辑把请求回读成历史终态任务。
        if force_new and idempotency_key:
            idempotency_key = f"{idempotency_key}|rerun:{uuid4().hex}"

        # 幂等键为空的任务保持原有语义：每次调用都创建新任务。
        # 有幂等键时先回读，终态也必须复用，显式重跑应由调用方生成新业务键。
        create_guard = _IDEMPOTENT_CREATE_LOCK if idempotency_key else nullcontext()
        with create_guard:
            if idempotency_key:
                existing = db.execute(
                    select(AsyncTaskRecord)
                    .where(AsyncTaskRecord.idempotency_key == idempotency_key)
                    .order_by(AsyncTaskRecord.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if existing is not None:
                    return _task_to_read(existing)

            task_id = uuid4().hex
            cid = _gen_correlation_id()
            task = AsyncTaskRecord(
                id=task_id,
                task_type=task_type,
                status="queued",
                stage="queued",
                percent=0,
                message="Task created",
                payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                correlation_id=cid,
                idempotency_key=idempotency_key,
                is_terminal_locked=0,
            )
            db.add(task)
            try:
                db.commit()
            except IntegrityError:
                # MySQL/新 SQLite 库上的唯一索引可能在并发请求间先触发；
                # 回滚后回读已提交任务，只有找不到同键记录时才保留原异常。
                db.rollback()
                if idempotency_key:
                    existing = db.execute(
                        select(AsyncTaskRecord)
                        .where(AsyncTaskRecord.idempotency_key == idempotency_key)
                        .order_by(AsyncTaskRecord.created_at.asc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if existing is not None:
                        return _task_to_read(existing)
                raise
            db.refresh(task)
            return _task_to_read(task)
    finally:
        db.close()


def get_async_task(
    task_id: str,
    *,
    use_control_plane: bool = False,
) -> AsyncTaskRead | None:
    """查询任务状态。use_control_plane=True 心跳轮询走 NullPool 不排队。"""
    SessionLocal = _pick_session_factory(use_control_plane)
    db = SessionLocal()
    try:
        _run_patrol(db)
        _expire_stale_tasks(db)
        task = db.get(AsyncTaskRecord, task_id)
        return _task_to_read(task) if task is not None else None
    finally:
        db.close()


def list_async_tasks(
    task_type: str | None = None,
    limit: int = 20,
    *,
    use_control_plane: bool = False,
) -> list[AsyncTaskRead]:
    """列出最近的异步任务。"""
    SessionLocal = _pick_session_factory(use_control_plane)
    db = SessionLocal()
    try:
        _run_patrol(db)
        _expire_stale_tasks(db)
        stmt = select(AsyncTaskRecord).order_by(desc(AsyncTaskRecord.created_at)).limit(limit)
        if task_type:
            stmt = stmt.where(AsyncTaskRecord.task_type == task_type)
        rows = db.execute(stmt).scalars().all()
        return [_task_to_read(item) for item in rows]
    finally:
        db.close()


def cancel_async_task(task_id: str, *, timeout_seconds: int = 60) -> AsyncTaskRead:
    """取消任务（FR-P1-2/AC-10 增强版）。

    - 若已处终态或 is_terminal_locked==1 → 直接返回，绝不覆盖。
    - 否则写入 cancel_requested=1，cancelled_timeout_at=now+timeout（供巡检升级
      CANCELLED_TIMEOUT 审计记录，而不破坏 status=cancelled 的语义）。
    - 把 correlation_id 写回 errors_json，便于 outbox/audit 串联。
    - 如果原本已是 queued（无 Worker 运行），则直接置 cancelled + is_terminal_locked=1；
      若为 running，仅置 cancel_requested + cancelled_timeout_at，让 Worker 自检
      `cancel_requested==1` 后自止，置 cancelled + 终态锁（见 heartbeat 自检）。
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise ValueError("Async task not found")

        # 终态硬锁：不改动 status/stage
        if int(getattr(task, "is_terminal_locked", 0) or 0) == 1:
            return _task_to_read(task)
        if task.status in ("done", "failed", "cancelled"):
            return _task_to_read(task)

        cid = task.correlation_id or _gen_correlation_id()
        if not task.correlation_id:
            task.correlation_id = cid
        cancelled_blocker = {
            "code": "eval.task.cancelled_by_user",
            "severity": "warn",
            "category": "config",
            "title_zh": "任务已被用户取消",
            "detail_zh": (
                "用户主动调用了任务取消接口，评估流程提前终止。"
                f"correlation_id={cid} timeout_seconds={timeout_seconds}"
            ),
            "correlation_id": cid,
            "evidence": {"cancelled_by_user": True, "correlation_id": cid, "timeout_seconds": timeout_seconds},
            "retryable": True,
        }
        try:
            prev = json.loads(task.errors_json) if task.errors_json else []
            if not isinstance(prev, list):
                prev = []
            prev.append(cancelled_blocker)
            task.errors_json = json.dumps(prev[-20:], ensure_ascii=False)
        except Exception:
            task.errors_json = json.dumps([cancelled_blocker], ensure_ascii=False)

        task.cancel_requested = 1
        task.cancelled_timeout_at = _now() + timedelta(seconds=max(1, int(timeout_seconds or 60)))
        task.updated_at = _now()

        # queued 阶段没有 Worker 可以自止，直接置 cancelled + 终态锁
        if task.status == "queued":
            task.status = "cancelled"
            task.stage = "cancelled"
            task.message = "Task cancelled by user"
            task.finished_at = _now()
            task.is_terminal_locked = 1

        db.commit()
        db.refresh(task)
        return _task_to_read(task)
    finally:
        db.close()
