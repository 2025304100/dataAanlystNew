"""FR-P1-2 Governance Outbox 事务消息投送器（AC-10 0 重复成交核心）。

角色区分：
- `emit_governance_event(session, payload)` — **业务代码调用入口**：
  与业务写入同事务落 `governance_events_outbox`，利用
  `UniqueConstraint(dedup_key)` 保证同 dedup_key 幂等（IntegrityError 吞掉）。
  此函数只写表，不立刻执行下游（订单/持仓/审计）写操作。
- `dispatch_outbox_batch(session, worker_id, batch_size)` — **Cron/守护 Worker 调用**：
  拉取 READY（或到期重试）事件并锁定（locked_by_worker/locked_until），
  然后按 event_type 交给 `EventHandlers` 处理。处理结果：
    * 成功 → status=DELIVERED，delivered_at=now
    * 临时失败 → retry_count++，next_retry_at=now + 指数退避；到达 max_retries 时 DEAD_LETTERED
    * 重复事件（通过事件自身再次查目标表里的 dedup_key 已经应用过）→ 直接置 DELIVERED
- `EventHandlers` 提供 `decision_made / order_plan_written / position_reconciled / audit_written / task_cancel_timeout`
  的默认实现：
  每个 handler 在其目标表内查 `correlation_id + event_type + business_key` 已存在则 SKIP，
  否则写入、再写一个 DedupLog（或用自身业务表的唯一键）标记已应用，保证
  **同事件重复投递 N 次只会生效 1 次**（AC-10 重复投递 3 次 0 重复下单/成交）。

故障注入点（供 pytest monkeypatch 用）：
- governance_outbox.FAULT_EMIT_RAISE_AFTER_WRITE = N
  emit 在写完 outbox 后抛异常 N 次（模拟事务中断）。
- governance_outbox.FAULT_DISPATCH_RAISE_BEFORE_APPLY = N
  dispatch 在实际 apply 之前抛异常 N 次（模拟 DB jitter）。
- governance_outbox.FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK = N
  apply 成功但置 DELIVERED 前抛异常 N 次（触发 handler 内部 DedupLog 去重）。
- governance_outbox.FAULT_CIRCUIT_BREAKER_OPEN = N
  dispatch 前 N 次直接抛 OperationalError 模拟 DB/通道熔断。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.decision_engine import GovernanceOutboxEvent


logger = logging.getLogger(__name__)


# 故障注入全局变量（pytest monkeypatch 直接改）
FAULT_EMIT_RAISE_AFTER_WRITE: int = 0
FAULT_DISPATCH_RAISE_BEFORE_APPLY: int = 0
FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK: int = 0
FAULT_CIRCUIT_BREAKER_OPEN: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_cid() -> str:
    return uuid4().hex[:8]


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# emit_governance_event：业务层同事务落事件
# ---------------------------------------------------------------------------
def emit_governance_event(
    db: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict,
    correlation_id: str | None = None,
    business_key: str | None = None,
    task_id: str | None = None,
    portfolio_id: int | None = None,
    dedup_key: str | None = None,
    max_retries: int = 8,
    _fault_after_write_counter: list[int] | None = None,
) -> Optional[GovernanceOutboxEvent]:
    """写入 GovernanceOutboxEvent（事务原子性：与业务写入同 session.commit）。

    dedup_key 默认 = aggregate_type|aggregate_id|event_type|business_key|correlation_id，
    若 UniqueConstraint(dedup_key) 命中 → 返回 None（视为「同事件已写入」，幂等）。
    """
    cid = correlation_id or payload.get("correlation_id") or _gen_cid()
    payload = dict(payload or {})
    payload.setdefault("correlation_id", cid)
    payload.setdefault("occurred_at", _utcnow().isoformat() + "Z")
    payload.setdefault("event_type", event_type)

    key = dedup_key or f"{aggregate_type}|{aggregate_id}|{event_type}|{business_key or ''}|{cid}"

    ev = GovernanceOutboxEvent(
        dedup_key=key,
        correlation_id=cid,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        event_type=event_type,
        business_key=business_key,
        task_id=task_id,
        portfolio_id=portfolio_id,
        payload_json=_json_dumps(payload),
        status="READY",
        retry_count=0,
        max_retries=max(1, min(max_retries, 200)),
        next_retry_at=None,
        created_at=_utcnow(),
    )
    db.add(ev)
    try:
        db.flush()  # 触发 UniqueConstraint；不 commit，交给业务外层
    except IntegrityError as exc:
        # UniqueConstraint(dedup_key)：已写入则幂等
        db.rollback()
        logger.info("emit_governance_event dedup_hit dedup_key=%s exc=%s", key, exc)
        return None

    # 故障注入：写成功后模拟事务中断（业务外层 commit 失败 → outbox 记录未真正落库）
    global FAULT_EMIT_RAISE_AFTER_WRITE
    counter = _fault_after_write_counter
    if FAULT_EMIT_RAISE_AFTER_WRITE > 0:
        if counter is not None:
            counter[0] += 1
            if counter[0] <= FAULT_EMIT_RAISE_AFTER_WRITE:
                raise OperationalError(
                    statement="fault_inject: emit_raise_after_write",
                    params={"dedup_key": key},
                    orig=RuntimeError("fault_inject emit_raise_after_write"),
                )
        else:
            FAULT_EMIT_RAISE_AFTER_WRITE -= 1
            raise OperationalError(
                statement="fault_inject: emit_raise_after_write",
                params={"dedup_key": key},
                orig=RuntimeError("fault_inject emit_raise_after_write"),
            )

    return ev


# ---------------------------------------------------------------------------
# EventHandlers：至少一次投递后，下游幂等写入业务表（示例实现）
# ---------------------------------------------------------------------------
class EventHandlers:
    """默认 Outbox 事件处理器：下游幂等应用。

    每个 handler 返回 {"applied": bool, "reason": str}。若 `applied=False`
    表示事件已被应用或跳过；dispatch 会直接把 outbox 置 DELIVERED。

    为了演示 AC-10 的「重复投递 3 次 0 重复成交」，这里用一张轻量级
    DedupLog（内存 dict 或 SQLite/MySQL 表）记录已应用的 (target, dedup_token)。
    真实生产环境：应该直接用下游业务表本身的 UK（如 order_plans.dedup_key）。
    """

    _dedup_store: dict[str, str] = {}

    @classmethod
    def clear_dedup(cls):
        cls._dedup_store.clear()

    @classmethod
    def _is_applied(cls, target: str, dedup_token: str) -> tuple[bool, str]:
        k = f"{target}::{dedup_token}"
        if k in cls._dedup_store:
            return True, cls._dedup_store[k]
        return False, ""

    @classmethod
    def _mark_applied(cls, target: str, dedup_token: str, value: str = "1"):
        cls._dedup_store[f"{target}::{dedup_token}"] = value

    @staticmethod
    def _ensure_hashes(payload: dict) -> dict:
        """给 payload 填 pre_hash/post_hash/version，默认 sha1(payload_json 切片)。"""
        p = dict(payload)
        if not p.get("version"):
            p["version"] = 1
        if not p.get("pre_hash"):
            p["pre_hash"] = hashlib.sha1(_json_dumps({k: v for k, v in p.items() if k != "post_hash"}).encode("utf-8")).hexdigest()[:16]
        if not p.get("post_hash"):
            p["post_hash"] = hashlib.sha1(_json_dumps(p).encode("utf-8")).hexdigest()[:16]
        return p

    # ── 各类事件处理器 ────────────────────────────────────────────
    @classmethod
    def decision_made(cls, db: Session, ev: GovernanceOutboxEvent) -> dict:
        """DECISION_MADE：落 DecisionRun correlation_id/task_id 幂等写入。

        演示：若 decision_runs 里已存在同 (portfolio_id, trade_date, content_hash)
        或同 correlation_id → SKIP。这里用 DedupLog 做模拟。
        """
        payload = json.loads(ev.payload_json or "{}")
        token = f"{ev.correlation_id}|{ev.event_type}|{ev.business_key or ''}|DECISION"
        applied, reason = cls._is_applied("decision_run", token)
        if applied:
            return {"applied": False, "reason": f"dedup_hit {reason}"}
        payload = cls._ensure_hashes(payload)
        cls._mark_applied("decision_run", token, ev.correlation_id)
        return {"applied": True, "decision": payload.get("trade_date"), "pre_hash": payload["pre_hash"], "post_hash": payload["post_hash"]}

    @classmethod
    def order_plan_written(cls, db: Session, ev: GovernanceOutboxEvent) -> dict:
        """ORDER_PLAN_WRITTEN：订单计划幂等写入。

        对应 AC-10 关键：同 correlation_id + same_symbol_buy/sell_date + portfolio_id
        在 order_plans 里只能有 1 条。这里以 DedupLog 模拟。
        """
        payload = json.loads(ev.payload_json or "{}")
        token = f"{ev.correlation_id}|{ev.event_type}|{ev.business_key or ''}|ORDER"
        applied, reason = cls._is_applied("order_plan", token)
        if applied:
            return {"applied": False, "reason": f"dedup_hit {reason}"}
        payload = cls._ensure_hashes(payload)
        cls._mark_applied("order_plan", token, ev.correlation_id)
        # 统计重复触发保护：重复进来 DedupLog 已命中 → 不重复下单
        return {"applied": True, "orders": payload.get("orders"), "pre_hash": payload["pre_hash"], "post_hash": payload["post_hash"]}

    @classmethod
    def position_reconciled(cls, db: Session, ev: GovernanceOutboxEvent) -> dict:
        """POSITION_RECONCILED：持仓对账幂等写入。"""
        payload = json.loads(ev.payload_json or "{}")
        token = f"{ev.correlation_id}|{ev.event_type}|{ev.business_key or ''}|POSITION"
        applied, reason = cls._is_applied("position", token)
        if applied:
            return {"applied": False, "reason": f"dedup_hit {reason}"}
        payload = cls._ensure_hashes(payload)
        cls._mark_applied("position", token, ev.correlation_id)
        return {"applied": True, "as_of": payload.get("as_of"), "pre_hash": payload["pre_hash"], "post_hash": payload["post_hash"]}

    @classmethod
    def audit_written(cls, db: Session, ev: GovernanceOutboxEvent) -> dict:
        """AUDIT_WRITTEN：审计事件幂等写入。"""
        payload = json.loads(ev.payload_json or "{}")
        token = f"{ev.correlation_id}|{ev.event_type}|{ev.business_key or ''}|AUDIT"
        applied, reason = cls._is_applied("audit", token)
        if applied:
            return {"applied": False, "reason": f"dedup_hit {reason}"}
        payload = cls._ensure_hashes(payload)
        cls._mark_applied("audit", token, ev.correlation_id)
        return {"applied": True, "action": payload.get("action"), "pre_hash": payload["pre_hash"], "post_hash": payload["post_hash"]}

    @classmethod
    def task_cancel_timeout(cls, db: Session, ev: GovernanceOutboxEvent) -> dict:
        """CANCELLED_TIMEOUT：写审计幂等（同 correlation_id 只写一次）。"""
        payload = json.loads(ev.payload_json or "{}")
        token = f"{ev.correlation_id}|{ev.event_type}|TASK_CANCEL_TIMEOUT"
        applied, reason = cls._is_applied("cancel_timeout_audit", token)
        if applied:
            return {"applied": False, "reason": f"dedup_hit {reason}"}
        payload = cls._ensure_hashes(payload)
        cls._mark_applied("cancel_timeout_audit", token, ev.correlation_id)
        return {"applied": True, "upgrade_mode": payload.get("upgrade_mode")}

    @classmethod
    def default(cls, db: Session, ev: GovernanceOutboxEvent) -> dict:
        """未知事件类型：默认直接置 DELIVERED（避免在队列中堆积）。"""
        return {"applied": True, "reason": "unhandled_event_type_marked_delivered"}


# ---------------------------------------------------------------------------
# dispatch_outbox_batch：Worker 拉取 + 锁定 + 处理 + 状态更新
# ---------------------------------------------------------------------------
def _backoff_seconds(retry_count: int) -> int:
    """指数退避，[1,2,4,8,...,60] 夹逼。"""
    return max(1, min(60, int(2 ** min(retry_count, 6))))


def dispatch_outbox_batch(
    db: Session,
    *,
    worker_id: str,
    batch_size: int = 16,
    lock_seconds: int = 300,
    handlers: type[EventHandlers] = EventHandlers,
) -> dict:
    """拉取 READY/到期重试的 outbox 事件并执行。

    流程：
    1) SELECT ... WHERE status IN (READY, IN_PROGRESS) AND (next_retry_at IS NULL OR next_retry_at<=now)
       FOR UPDATE SKIP LOCKED（MySQL/SQLite 兼容：用 locked_until/locked_by_worker 近似实现）。
    2) 对每个事件：调用 handler，根据返回值或异常更新 status/retry/死信。
    3) 所有写入都在本 session 内 commit；异常导致单条回滚时，仍在 catch 块内更新 retry。
    """
    stats = {"fetched": 0, "delivered": 0, "dead_lettered": 0, "retrying": 0, "dedup_skipped": 0, "faults": 0}
    now = _utcnow()

    # 故障注入：DB/通道熔断（抛 OperationalError）
    global FAULT_CIRCUIT_BREAKER_OPEN
    global FAULT_DISPATCH_RAISE_BEFORE_APPLY
    global FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK
    if FAULT_CIRCUIT_BREAKER_OPEN > 0:
        FAULT_CIRCUIT_BREAKER_OPEN -= 1
        stats["faults"] += 1
        raise OperationalError(
            statement="fault_inject: circuit_breaker_open",
            params={},
            orig=RuntimeError("fault_inject circuit_breaker_open"),
        )

    # 抓取候选（未锁或锁已过期）
    lock_cutoff = now
    stmt = (
        select(GovernanceOutboxEvent)
        .where(
            GovernanceOutboxEvent.status.in_(("READY", "IN_PROGRESS")),
            or_(
                GovernanceOutboxEvent.next_retry_at.is_(None),
                GovernanceOutboxEvent.next_retry_at <= now,
            ),
            or_(
                GovernanceOutboxEvent.locked_until.is_(None),
                GovernanceOutboxEvent.locked_until < lock_cutoff,
            ),
        )
        .order_by(GovernanceOutboxEvent.created_at.asc())
        .limit(batch_size)
    )
    try:
        rows = db.execute(stmt).scalars().all()
    except Exception as exc:
        logger.exception("dispatch_outbox_batch select failed: %s", exc)
        return stats

    # 加锁（以 update 保证原子）
    locked_ids: list[int] = []
    for ev in rows:
        upd = (
            db.query(GovernanceOutboxEvent)
            .filter(
                GovernanceOutboxEvent.id == ev.id,
                or_(
                    GovernanceOutboxEvent.locked_until.is_(None),
                    GovernanceOutboxEvent.locked_until < lock_cutoff,
                ),
            )
            .update(
                {
                    "locked_by_worker": worker_id,
                    "locked_until": now + timedelta(seconds=max(30, lock_seconds)),
                    "status": "IN_PROGRESS",
                },
                synchronize_session="fetch",
            )
        )
        if upd:
            locked_ids.append(ev.id)
    db.commit()

    if not locked_ids:
        return stats

    stats["fetched"] = len(locked_ids)
    for ev_id in locked_ids:
        ev = db.get(GovernanceOutboxEvent, ev_id)
        if ev is None:
            continue
        try:
            # 故障注入：apply 前抛异常
            if FAULT_DISPATCH_RAISE_BEFORE_APPLY > 0:
                FAULT_DISPATCH_RAISE_BEFORE_APPLY -= 1
                stats["faults"] += 1
                raise OperationalError(
                    statement="fault_inject: before_apply",
                    params={"event_id": ev.id},
                    orig=RuntimeError("fault_inject before_apply"),
                )

            result: dict = {}
            if ev.event_type == "DECISION_MADE":
                result = handlers.decision_made(db, ev)
            elif ev.event_type == "ORDER_PLAN_WRITTEN":
                result = handlers.order_plan_written(db, ev)
            elif ev.event_type == "POSITION_RECONCILED":
                result = handlers.position_reconciled(db, ev)
            elif ev.event_type == "AUDIT_WRITTEN":
                result = handlers.audit_written(db, ev)
            elif ev.event_type == "CANCELLED_TIMEOUT":
                result = handlers.task_cancel_timeout(db, ev)
            else:
                result = handlers.default(db, ev)

            # 故障注入：apply 成功后，置 DELIVERED 前抛异常
            if FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK > 0:
                FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK -= 1
                stats["faults"] += 1
                raise OperationalError(
                    statement="fault_inject: after_apply_before_mark",
                    params={"event_id": ev.id},
                    orig=RuntimeError("fault_inject after_apply_before_mark"),
                )

            # 已被 handler 去重（幂等命中）→ 直接置 DELIVERED，不视为重试
            if result.get("applied") is False and "dedup_hit" in (result.get("reason") or ""):
                ev.status = "DELIVERED"
                ev.delivered_at = _utcnow()
                ev.last_error = None
                ev.locked_by_worker = None
                ev.locked_until = None
                stats["dedup_skipped"] += 1
                stats["delivered"] += 1
            else:
                ev.status = "DELIVERED"
                ev.delivered_at = _utcnow()
                ev.last_error = None
                ev.locked_by_worker = None
                ev.locked_until = None
                stats["delivered"] += 1
            db.commit()
        except Exception as exc:
            # 失败 → retry_count++，指数退避；到 max_retries 死信
            db.rollback()
            ev = db.get(GovernanceOutboxEvent, ev_id)
            if ev is None:
                continue
            ev.retry_count = int(ev.retry_count or 0) + 1
            err = f"{type(exc).__name__}: {str(exc)[:800]}"
            ev.last_error = err
            if ev.retry_count >= int(ev.max_retries or 8):
                ev.status = "DEAD_LETTERED"
                ev.dead_lettered_at = _utcnow()
                ev.next_retry_at = None
                ev.locked_by_worker = None
                ev.locked_until = None
                stats["dead_lettered"] += 1
            else:
                ev.status = "READY"
                ev.next_retry_at = _utcnow() + timedelta(seconds=_backoff_seconds(ev.retry_count))
                ev.locked_by_worker = None
                ev.locked_until = None
                stats["retrying"] += 1
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("dispatch commit retry-status failed for event %s", ev.id)
    return stats


# ---------------------------------------------------------------------------
# 便捷入口：独立 Session 跑一次 dispatch（Cron 调用）
# ---------------------------------------------------------------------------
def run_dispatch_once(*, worker_id: str = "cron", batch_size: int = 16, lock_seconds: int = 300,
                      handlers: type[EventHandlers] = EventHandlers,
                      require_table: bool = False) -> dict:
    """以独立 Session 跑一次 outbox dispatch。

    若 governance_events_outbox 表还不存在：返回 None 警告（不阻塞其他功能）。
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        from sqlalchemy import inspect as sqla_inspect
        insp = sqla_inspect(db.bind)
        if not insp.has_table("governance_events_outbox"):
            if require_table:
                raise RuntimeError("governance_events_outbox missing; run alembic upgrade")
            logger.warning("run_dispatch_once skipped: governance_events_outbox table missing")
            return {"warning": "missing_table", "fetched": 0}
        return dispatch_outbox_batch(
            db,
            worker_id=worker_id,
            batch_size=batch_size,
            lock_seconds=lock_seconds,
            handlers=handlers,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
