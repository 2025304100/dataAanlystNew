"""FR-P1-2 任务心跳 + 取消令牌自检 + CANCELLED_TIMEOUT 升级。

目标（AC-10）：
- Worker 内每步都心跳 + 自检取消令牌（cancel_requested==1）及时自止。
- 若 running 任务 heartbeat_at 长期停滞（>HEARTBEAT_STALL_SECONDS），
  巡检先标记 stage=stalled，suggested_action=restart_or_resume，不强制改 status，
  以避免误判网络抖动导致的短暂心跳中断。
- 若 cancel_requested==1 且 cancelled_timeout_at 已过期，说明 Worker 未响应，
  升级为 CANCELLED_TIMEOUT：
    * status/cancelled 语义不变（status=cancelled）；
    * errors_json 追加 TASK_CANCEL_TIMEOUT（code=eval.task.cancel_timeout）；
    * 写入 GovernanceOutboxEvent.event_type=CANCELLED_TIMEOUT，供审计/对账串联；
    * 写入 audit_event.action=TASK_CANCEL_TIMEOUT。
- 全部巡检入口不改动 is_terminal_locked==1 的任务（任何逻辑都不再覆盖终态）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 心跳停滞阈值：running + heartbeat_at < now - N → stage=stalled
HEARTBEAT_STALL_SECONDS = int(3 * 60)           # 3 分钟无心跳视为疑似卡死
# CANCEL 自止宽容期：已请求 cancel 但 N 秒后仍未终态 → 升级 CANCELLED_TIMEOUT
CANCELLED_TIMEOUT_GRACE_SECONDS = 10            # 比 cancel 设置的 timeout 多 10 秒再升级
# 心跳批刷新间隔（Worker 自检周期，默认 Worker 每 5~30 秒调用一次 heartbeat）
DEFAULT_HEARTBEAT_BUDGET_SECONDS = int(30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_cid() -> str:
    return uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Worker 侧：心跳 + 取消令牌自检
# ---------------------------------------------------------------------------
def heartbeat_task(db: Session, task_id: str, *,
                   stage: str | None = None,
                   percent: float | None = None,
                   message: str | None = None,
                   ok_count: int | None = None,
                   failed_count: int | None = None,
                   processed: int | None = None,
                   current_item: str | None = None,
                   stage_budget_seconds: int | None = None,
                   stage_started_at: datetime | None = None,
                   current_step_description: str | None = None,
                   batch_recovery: dict | list | None = None,
                   ) -> tuple[bool, bool]:
    """Worker 内调用：刷新 heartbeat + 自检取消令牌 + 终态硬锁。

    返回 (should_continue, is_cancelled)：
    - should_continue=False：任务已是终态 or 已取消令牌 → Worker 必须立即返回/退出。
    - is_cancelled=True：cancel_requested==1，Worker 应尽快安全自止（写完 checkpoint），
      随后用 finalize_cancelled_self_exit() 置 status=cancelled + 终态锁。
    """
    # 终态硬锁：is_terminal_locked==1 或 status 已终态 → Worker 退出（永远不重写终态）
    locked = False
    cancelled = False
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        return False, False

    if int(getattr(task, "is_terminal_locked", 0) or 0) == 1:
        return False, task.status == "cancelled"
    if task.status in ("done", "failed", "cancelled"):
        return False, task.status == "cancelled"
    if int(getattr(task, "cancel_requested", 0) or 0) == 1:
        cancelled = True

    # 刷心跳
    task.heartbeat_at = _utcnow()
    if stage is not None:
        task.stage = stage
    if percent is not None:
        task.percent = max(0.0, min(100.0, float(percent)))
        task.last_progress_percent = task.percent
        task.last_progress_at = _utcnow()
    if message is not None:
        task.message = message
    if ok_count is not None:
        task.ok_count = int(ok_count)
    if failed_count is not None:
        task.failed_count = int(failed_count)
    if processed is not None:
        task.processed = int(processed)
    if current_item is not None:
        task.current_item = current_item
    if stage_budget_seconds is not None:
        task.stage_budget_seconds = int(stage_budget_seconds)
    if stage_started_at is not None:
        task.stage_started_at = stage_started_at
    if current_step_description is not None:
        task.current_step_description = current_step_description
    if batch_recovery is not None:
        task.batch_recovery_json = json.dumps(batch_recovery, ensure_ascii=False, default=str)
    task.updated_at = _utcnow()
    task.last_patrol_at = _utcnow()
    db.commit()
    db.refresh(task)

    # 自检：cancel_requested → Worker 立即自止（注意：这里不直接改 status，
    # 而是由 Worker 自己在 finalize_cancelled_self_exit 里安全置终态 + 终态锁，
    # 避免 Worker 中途 checkpoint 还没落库就被巡检刷成 cancelled）。
    if int(getattr(task, "cancel_requested", 0) or 0) == 1:
        cancelled = True

    should_continue = not cancelled and int(getattr(task, "is_terminal_locked", 0) or 0) != 1
    return should_continue, cancelled


def finalize_cancelled_self_exit(db: Session, task_id: str, *, message: str | None = None, extra_errors: list[dict] | None = None) -> None:
    """Worker 自检到 cancelled=True 后，安全自止：置 status=cancelled + 终态锁 + correlation_id。

    前置条件：Worker 已把需要持久化的 checkpoint/订单回滚/决策幂等落库完毕，
    再调用本函数。
    """
    task = db.get(AsyncTaskRecord, task_id)
    if task is None:
        return
    if int(getattr(task, "is_terminal_locked", 0) or 0) == 1:
        return  # 已被巡检或 cancel_async_task 锁死

    cid = task.correlation_id or _gen_cid()
    if not task.correlation_id:
        task.correlation_id = cid

    # 追加结构化错误（自止原因）
    errors = []
    try:
        errors = json.loads(task.errors_json) if task.errors_json else []
        if not isinstance(errors, list):
            errors = []
    except Exception:
        errors = []
    errors.append({
        "code": "eval.task.cancelled_self_exit",
        "severity": "warn",
        "category": "config",
        "title_zh": "Worker 检测到取消令牌，安全自止",
        "detail_zh": (
            "Worker 在执行步骤中发现 cancel_requested=1，"
            "已完成 checkpoint 落库后主动退出。"
            f"correlation_id={cid}"
        ),
        "correlation_id": cid,
        "evidence": {"correlation_id": cid, "self_exit": True, "final_message": message or ""},
        "retryable": True,
    })
    if extra_errors:
        errors.extend(extra_errors)
    task.errors_json = json.dumps(errors[-50:], ensure_ascii=False, default=str)

    task.status = "cancelled"
    task.stage = "cancelled"
    task.message = message or "Task cancelled by worker (self-exit on cancel_requested)"
    task.finished_at = _utcnow()
    task.updated_at = _utcnow()
    task.is_terminal_locked = 1
    db.commit()


# ---------------------------------------------------------------------------
# 巡检：心跳停滞 + CANCELLED_TIMEOUT 升级
# ---------------------------------------------------------------------------
def _is_terminal(task: AsyncTaskRecord) -> bool:
    if int(getattr(task, "is_terminal_locked", 0) or 0) == 1:
        return True
    return task.status in ("done", "failed", "cancelled")


def patrol_stalled_and_cancel_timeout(db: Session,
                                      *,
                                      heartbeat_stall_seconds: int = HEARTBEAT_STALL_SECONDS,
                                      emit_event_cb: Callable[[dict], None] | None = None,
                                      write_audit_cb: Callable[[dict], None] | None = None,
                                      ) -> dict:
    """对 async_tasks 做 FR-P1-2 巡检：

    1) running 心跳停滞 → stage=stalled + suggested_action，不改 status
       （避免误判网络抖动直接置失败，进而触发订单/持仓状态机被覆盖）。
    2) cancel_requested==1 且 cancelled_timeout_at<now → CANCELLED_TIMEOUT 升级：
       errors_json[TASK_CANCEL_TIMEOUT] + outbox 事件 + audit；
       再把 status 置 cancelled + 终态锁（若当前 status 仍为 running/queued）。

    emit_event_cb / write_audit_cb 可选（无 governance_outbox 审计模块时忽略），
    签名均为 `fn(payload_dict) -> None`，payload_dict 至少含 correlation_id、
    occurred_at、task_id、portfolio_id 等。

    返回统计：{stalled_marked, cancelled_timeout_upgraded, skipped_locked}
    """
    stats = {"stalled_marked": 0, "cancelled_timeout_upgraded": 0, "skipped_locked": 0}
    now = _utcnow()

    # 1. 心跳停滞：running（非终态锁） + heartbeat_at 停滞 OR last_progress_at 停滞
    stall_cutoff = now - timedelta(seconds=max(10, heartbeat_stall_seconds))
    stmt = (
        select(AsyncTaskRecord)
        .where(
            AsyncTaskRecord.status.in_(("queued", "running")),
            or_(
                (AsyncTaskRecord.is_terminal_locked == 0),
                AsyncTaskRecord.is_terminal_locked.is_(None),
            ),
            or_(
                AsyncTaskRecord.heartbeat_at < stall_cutoff,
                and_(
                    AsyncTaskRecord.last_progress_at.is_not(None),
                    AsyncTaskRecord.last_progress_at < stall_cutoff,
                ),
            ),
        )
    )
    stalled_rows = db.execute(stmt).scalars().all()
    for task in stalled_rows:
        if _is_terminal(task):
            stats["skipped_locked"] += 1
            continue
        if task.stage != "stalled":
            task.stage = "stalled"
        task.suggested_action = (
            "restart_or_resume: heartbeat/progress stalled > "
            f"{heartbeat_stall_seconds}s; status kept unchanged to avoid rollback noise."
        )
        task.last_patrol_at = now
        task.updated_at = now
        stats["stalled_marked"] += 1
    if stalled_rows:
        db.commit()

    # 2. CANCELLED_TIMEOUT 升级：cancel_requested==1 且 cancelled_timeout_at 已过期
    grace = timedelta(seconds=CANCELLED_TIMEOUT_GRACE_SECONDS)
    stmt2 = (
        select(AsyncTaskRecord)
        .where(
            or_(AsyncTaskRecord.cancel_requested == 1, AsyncTaskRecord.cancel_requested.is_(None).isnot(True)),
            AsyncTaskRecord.cancelled_timeout_at.is_not(None),
            AsyncTaskRecord.cancelled_timeout_at < now,
        )
    )
    cancel_timeout_rows = db.execute(stmt2).scalars().all()

    for task in cancel_timeout_rows:
        if _is_terminal(task):
            # 终态锁/已终态 → 跳过（保证 project_memory #8：终态永不被覆盖）
            stats["skipped_locked"] += 1
            continue
        if int(getattr(task, "cancel_requested", 0) or 0) != 1:
            continue

        cid = task.correlation_id or _gen_cid()
        if not task.correlation_id:
            task.correlation_id = cid

        # 追加 CANCEL_TIMEOUT 错误到 errors_json
        err_item = {
            "code": "eval.task.cancel_timeout",
            "severity": "error",
            "category": "infra",
            "title_zh": "取消请求超时未响应，升级为 CANCELLED_TIMEOUT",
            "detail_zh": (
                "用户调用取消接口后，Worker 未在 cancelled_timeout_at 之前自止并写终态，"
                "巡检升级为 CANCELLED_TIMEOUT（status=cancelled 语义不变，以避免破坏下游对账）。"
                f"correlation_id={cid} task_id={task.id}"
            ),
            "correlation_id": cid,
            "evidence": {
                "correlation_id": cid,
                "task_id": task.id,
                "cancelled_timeout_at": task.cancelled_timeout_at.isoformat() if task.cancelled_timeout_at else None,
                "patrol_now": now.isoformat(),
                "status_before_upgrade": task.status,
                "stage_before_upgrade": task.stage,
                "upgrade_mode": "CANCELLED_TIMEOUT_KEEP_STATUS_CANCELLED",
            },
            "retryable": True,
        }
        try:
            prev = json.loads(task.errors_json) if task.errors_json else []
            if not isinstance(prev, list):
                prev = []
            prev.append(err_item)
            task.errors_json = json.dumps(prev[-50:], ensure_ascii=False, default=str)
        except Exception:
            task.errors_json = json.dumps([err_item], ensure_ascii=False, default=str)

        # 升级：status 直接置 cancelled + 终态锁
        task.status = "cancelled"
        task.stage = "cancel_timeout"
        task.message = "Task cancelled_timeout: worker did not respond within timeout window"
        task.finished_at = now
        task.updated_at = now
        task.is_terminal_locked = 1

        # Outbox 事件 + 审计记录（可选回调，pytest 会注入 mock/fake 去验证）
        try:
            payload = {
                "correlation_id": cid,
                "occurred_at": now.isoformat() + "Z",
                "operator_id": "system:patrol",
                "task_id": task.id,
                "portfolio_id": (
                    (json.loads(task.payload_json or "{}").get("portfolio_id")
                     if task.payload_json else None)
                ),
                "event_type": "CANCELLED_TIMEOUT",
                "status_before": err_item["evidence"]["status_before_upgrade"],
                "stage_before": err_item["evidence"]["stage_before_upgrade"],
                "upgrade_mode": err_item["evidence"]["upgrade_mode"],
            }
            if emit_event_cb is not None:
                try:
                    emit_event_cb(payload)
                except Exception:
                    logger.exception("emit_event_cb(CANCELLED_TIMEOUT) failed; ignoring")
            if write_audit_cb is not None:
                try:
                    write_audit_cb({
                        **payload,
                        "action": "TASK_CANCEL_TIMEOUT",
                        "entity_type": "async_task",
                        "entity_id": task.id,
                    })
                except Exception:
                    logger.exception("write_audit_cb(TASK_CANCEL_TIMEOUT) failed; ignoring")
        except Exception:
            logger.exception("Patrol CANCELLED_TIMEOUT side-effects failed")

        stats["cancelled_timeout_upgraded"] += 1
    if cancel_timeout_rows:
        db.commit()

    return stats


# ---------------------------------------------------------------------------
# 对外便捷入口：带 Session 的独立巡检（Cron / 手工刷新任务列表前调用）
# ---------------------------------------------------------------------------
def run_task_reliability_patrol(*,
                                heartbeat_stall_seconds: int = HEARTBEAT_STALL_SECONDS,
                                emit_event_cb=None,
                                write_audit_cb=None,
                                ) -> dict:
    """在独立 Session 中执行可靠性巡检，返回统计 dict。

    注意：若数据库还没有新增列（is_terminal_locked / cancelled_timeout_at 等），
    本函数会降级为无操作，避免阻塞其他功能（方便开发阶段分阶段部署）。
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        # 最低限度探测列是否存在
        try:
            from sqlalchemy import inspect as sqla_inspect
            insp = sqla_inspect(db.bind)
            cols = {c["name"] for c in insp.get_columns("async_tasks")}
            required = {"is_terminal_locked", "cancelled_timeout_at", "correlation_id", "idempotency_key"}
            if not required.issubset(cols):
                logger.warning(
                    "task_heartbeat_service skipped: async_tasks missing columns %s (run alembic upgrade)",
                    required - cols,
                )
                return {"stalled_marked": 0, "cancelled_timeout_upgraded": 0, "skipped_locked": 0, "warning": "missing_async_tasks_columns"}
        except Exception:
            pass
        return patrol_stalled_and_cancel_timeout(
            db,
            heartbeat_stall_seconds=heartbeat_stall_seconds,
            emit_event_cb=emit_event_cb,
            write_audit_cb=write_audit_cb,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
