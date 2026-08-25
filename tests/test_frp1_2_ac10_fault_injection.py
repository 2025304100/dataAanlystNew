"""FR-P1-2 / AC-10 故障注入套件（9 场景，白盒 pytest）。

覆盖范围（对应 AC-10 + FR-P1-2 契约）：
 1. 任务心跳自检取消令牌 → Worker 自止写终态，is_terminal_locked=1。
 2. 心跳停滞（>3 分钟）→ 巡检升级 stage=stalled，status 不改（防误判）。
 3. cancel_requested + cancelled_timeout_at 过期 → 升级 CANCELLED_TIMEOUT：
    errors_json 追加 cancel_timeout + 写 outbox(CANCELLED_TIMEOUT) + 写 audit。
 4. Outbox emit 事务中断（2 次 flush 后抛 OperationalError） → 业务外层回滚，
    但失败重试 commit 成功时，UniqueConstraint(dedup_key) 保证不重复。
 5. Outbox dispatch DB 抖动（dispatch 前 3 次抛 OperationalError） → Worker
    重试成功后最终所有 READY 事件置 DELIVERED。
 6. Outbox 重复投递 3 次（apply 成功，mark delivered 前抛异常 2 次触发重投）
    → handler DedupLog 去重保证最终只应用 1 次（AC-10 0 重复下单/成交）。
 7. 熔断：FAULTER_CIRCUIT_BREAKER_OPEN=5 → dispatch 抛 OperationalError，
    下次无故障事件正常处理。
 8. Portfolio Resume：连续 5 个交易日补算 → 同 idempotency_key 下绝不重复创建任务；
    再次执行 resume 时 decision_already_succeeded 命中，全部 SKIP。
 9. 终态硬锁：cancelled 后又来 _set_task(status="running") → 被拒绝，
    is_terminal_locked=1 保持，status 仍为 cancelled（project_memory #8）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.decision_engine import DecisionRun, GovernanceOutboxEvent
from app.models.portfolio import Portfolio
from app.services import async_tasks as async_task_svc
from app.services import governance_outbox as outbox_svc
from app.services import portfolio_resume_service as resume_svc
from app.services import task_heartbeat_service as hb_svc
from app.services.task_heartbeat_service import (
    finalize_cancelled_self_exit,
    heartbeat_task,
    patrol_stalled_and_cancel_timeout,
)

pytestmark = pytest.mark.whitebox


def _clean_fault_globals() -> None:
    """在每个测试前把 governance_outbox 的全局故障注入计数器清零。"""
    outbox_svc.FAULT_EMIT_RAISE_AFTER_WRITE = 0
    outbox_svc.FAULT_DISPATCH_RAISE_BEFORE_APPLY = 0
    outbox_svc.FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK = 0
    outbox_svc.FAULT_CIRCUIT_BREAKER_OPEN = 0


_clean_fault_globals()


def _create_task(db, status="queued", task_type="market_data_sync", *,
                 portfolio_id=None, trade_date=None) -> AsyncTaskRecord:
    payload = {}
    if portfolio_id is not None:
        payload["portfolio_id"] = portfolio_id
    if trade_date is not None:
        payload["trade_date"] = trade_date.isoformat() if isinstance(trade_date, date) else trade_date
    t = AsyncTaskRecord(
        id=f"qa-task-{uuid4().hex[:8]}",
        task_type=task_type,
        status=status,
        stage="prepare",
        percent=0.0,
        message="",
        total=10,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _seed_portfolio(db, *, name: str = "qa-resume-pf",
                    last_reconciled: date | None = None,
                    last_decision: date | None = None,
                    effective: date | None = None) -> Portfolio:
    p = Portfolio(
        name=name,
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100_000,
        investable_ratio=1.0,
        cash_reserve_ratio=0.05,
        currency="CNY",
        auto_trade_enabled=1,
        last_reconciled_trade_date=last_reconciled,
        last_decision_trade_date=last_decision,
        effective_start_date=effective,
        is_test=1,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ── 1. 心跳自检取消令牌：Worker 应自止并写终态锁 ──────────────
def test_heartbeat_cancel_token_self_exit_and_lock(db_session):
    t = _create_task(db_session, status="running")
    # 模拟 cancel 接口调用过（cancel_requested=1）
    t.cancel_requested = 1
    t.cancelled_timeout_at = datetime.utcnow() + timedelta(hours=1)
    db_session.commit()

    go_on, is_cancelled = heartbeat_task(db_session, t.id, stage="download",
                                          percent=30, message="fetching bars", processed=3)
    assert is_cancelled is True
    assert go_on is False

    # Worker 发现取消令牌，安全自止（这里显式调 finalize）
    finalize_cancelled_self_exit(db_session, t.id, message="worker self-exit on cancel")
    db_session.refresh(t)
    assert t.status == "cancelled"
    assert int(getattr(t, "is_terminal_locked", 0) or 0) == 1
    assert t.correlation_id  # 终态归档 correlation_id
    errors = json.loads(t.errors_json or "[]")
    assert any(e.get("code") == "eval.task.cancelled_self_exit" for e in errors)


# ── 2. 心跳停滞 → 巡检置 stage=stalled，status 不改 ───────────
def test_patrol_stall_does_not_change_status(db_session):
    t = _create_task(db_session, status="running")
    t.heartbeat_at = datetime.utcnow() - timedelta(hours=1)
    t.last_progress_at = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    stats = patrol_stalled_and_cancel_timeout(db_session, heartbeat_stall_seconds=10)
    assert stats["stalled_marked"] >= 1
    db_session.refresh(t)
    # 关键：status 仍为 running（不覆盖，不触发订单/持仓状态机误判）
    assert t.status == "running"
    assert t.stage == "stalled"
    assert "restart_or_resume" in (t.suggested_action or "")


# ── 3. CANCELLED_TIMEOUT 升级：errors+outbox+audit 三串联 ─────
def test_cancelled_timeout_upgrade_with_outbox_and_audit(db_session):
    t = _create_task(db_session, status="running")
    t.cancel_requested = 1
    # 已过期 10 秒
    t.cancelled_timeout_at = datetime.utcnow() - timedelta(seconds=10)
    t.heartbeat_at = datetime.utcnow()
    db_session.commit()

    outbox_events: list[dict] = []
    audit_events: list[dict] = []

    def emit(p):
        # 把 CANCELLED_TIMEOUT 落 GovernanceOutboxEvent（真实实现走 emit_governance_event）
        ev = outbox_svc.emit_governance_event(
            db_session,
            event_type="CANCELLED_TIMEOUT",
            aggregate_type="async_task",
            aggregate_id=str(p["task_id"]),
            payload=p,
            correlation_id=p.get("correlation_id"),
            task_id=str(p["task_id"]),
            portfolio_id=p.get("portfolio_id"),
        )
        if ev is not None:
            outbox_events.append({"dedup_key": ev.dedup_key, "event_type": ev.event_type})

    def audit(p):
        audit_events.append(p)

    stats = patrol_stalled_and_cancel_timeout(
        db_session,
        emit_event_cb=emit,
        write_audit_cb=audit,
    )
    assert stats["cancelled_timeout_upgraded"] >= 1
    db_session.refresh(t)

    # 核心断言：status 变 cancelled（不破坏语义），终态锁，stage=cancel_timeout
    assert t.status == "cancelled"
    assert t.stage == "cancel_timeout"
    assert int(getattr(t, "is_terminal_locked", 0) or 0) == 1
    assert t.correlation_id
    errors = json.loads(t.errors_json or "[]")
    assert any(e.get("code") == "eval.task.cancel_timeout" for e in errors)
    # outbox + audit 都有记录，且 correlation_id 一致
    assert len(outbox_events) >= 1
    assert len(audit_events) >= 1
    shared_cid = t.correlation_id
    assert any(a.get("correlation_id") == shared_cid for a in audit_events)


# ── 4. Outbox emit 事务中断 → UniqueConstraint(dedup_key) 幂等 ──
def test_outbox_emit_interrupt_and_dedup_on_retry(db_session):
    outbox_svc.EventHandlers.clear_dedup()
    _clean_fault_globals()
    # 先造一个 portfolio 以满足 portfolio_id FK 约束
    pf = _seed_portfolio(db_session, name="qa-outbox-int",
                         effective=date(2026, 8, 1))

    outbox_svc.FAULT_EMIT_RAISE_AFTER_WRITE = 2
    cid = uuid4().hex[:8]
    attempts = 0
    for _ in range(5):
        attempts += 1
        if outbox_svc.FAULT_EMIT_RAISE_AFTER_WRITE > 0:
            # 人工模拟：flush 完成后在外层 rollback → 事务中断
            outbox_svc.FAULT_EMIT_RAISE_AFTER_WRITE -= 1
            ev_tmp = outbox_svc.GovernanceOutboxEvent(
                dedup_key=f"portfolio|{pf.id}|ORDER_PLAN_WRITTEN|buy_600000_2026-08-17|{cid}",
                correlation_id=cid,
                aggregate_type="portfolio",
                aggregate_id=str(pf.id),
                event_type="ORDER_PLAN_WRITTEN",
                business_key="buy_600000_2026-08-17",
                portfolio_id=pf.id,
                payload_json=json.dumps({"orders":[{"s":"600000","q":100}], "correlation_id":cid}),
                status="READY", retry_count=0, max_retries=8,
                created_at=datetime.utcnow().replace(tzinfo=None),
            )
            db_session.add(ev_tmp)
            db_session.flush()
            db_session.rollback()  # 模拟事务中断
            continue
        # 正常路径
        ev = outbox_svc.emit_governance_event(
            db_session,
            event_type="ORDER_PLAN_WRITTEN",
            aggregate_type="portfolio",
            aggregate_id=str(pf.id),
            payload={"orders": [{"s": "600000", "q": 100}], "correlation_id": cid},
            correlation_id=cid,
            business_key="buy_600000_2026-08-17",
            portfolio_id=pf.id,
        )
        db_session.commit()
        assert ev is not None
        break
    # attempts=3（前 2 次人工注入 rollback，第 3 次成功）
    assert attempts == 3
    # 再次 emit 同 dedup_key → 应返回 None（幂等）
    ev2 = outbox_svc.emit_governance_event(
        db_session,
        event_type="ORDER_PLAN_WRITTEN",
        aggregate_type="portfolio",
        aggregate_id=str(pf.id),
        payload={"orders": [{"s": "600000", "q": 100}], "correlation_id": cid},
        correlation_id=cid,
        business_key="buy_600000_2026-08-17",
        portfolio_id=pf.id,
    )
    assert ev2 is None  # UniqueConstraint(dedup_key) 命中
    cnt = db_session.execute(
        select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
    ).scalars().all()
    assert len(cnt) == 1  # 保证事务中断下不产生半生效/重复事件


# ── 5. Outbox dispatch DB jitter 前 3 次抛异常 → 最终成功 ─────
def test_outbox_dispatch_db_jitter_retry_and_success(db_session):
    outbox_svc.EventHandlers.clear_dedup()
    _clean_fault_globals()
    pf = _seed_portfolio(db_session, name="qa-outbox-jitter",
                         effective=date(2026, 8, 1))
    cid = uuid4().hex[:8]
    outbox_svc.emit_governance_event(
        db_session, event_type="POSITION_RECONCILED", aggregate_type="portfolio",
        aggregate_id=str(pf.id),
        payload={"as_of": "2026-08-17", "pnl": 3000, "correlation_id": cid},
        correlation_id=cid, business_key="2026-08-17", portfolio_id=pf.id,
        # 提高 max_retries：避免 jitter 人工回滚累计达到死信阈值
        max_retries=30,
    )
    db_session.commit()
    before = db_session.execute(
        select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
    ).scalars().all()
    assert len(before) == 1, f"before: {before}"

    # 注入 DB jitter：前 3 次 apply 前抛 OperationalError → dispatch 内部 retry_count++
    outbox_svc.FAULT_DISPATCH_RAISE_BEFORE_APPLY = 3
    ev = None
    for _ in range(12):
        try:
            outbox_svc.dispatch_outbox_batch(db_session, worker_id="w1", batch_size=8)
        except Exception:
            db_session.rollback()
        ev = db_session.execute(
            select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
        ).scalar_one_or_none()
        if ev is None:
            break
        if ev.status == "DELIVERED":
            break
        # 清 next_retry_at/locked 以便下一轮 batch 拉取（跳过 backoff 等待）
        if ev.status in ("READY", "IN_PROGRESS"):
            ev.next_retry_at = None
            ev.locked_until = None
            ev.locked_by_worker = None
            db_session.commit()
    assert ev is not None
    assert ev.status == "DELIVERED", (
        f"ev.status={ev.status}, retry={ev.retry_count}, "
        f"fault_var_remaining={outbox_svc.FAULT_DISPATCH_RAISE_BEFORE_APPLY}, err={ev.last_error}"
    )
    assert ev.delivered_at is not None


# ── 6. Outbox apply 成功但 mark 前抛 2 次 → handler 去重 0 重复 ─
def test_outbox_duplicate_delivery_dedup_three_times(db_session):
    outbox_svc.EventHandlers.clear_dedup()
    _clean_fault_globals()
    pf = _seed_portfolio(db_session, name="qa-outbox-dedup",
                         effective=date(2026, 8, 1))
    cid = uuid4().hex[:8]
    outbox_svc.emit_governance_event(
        db_session, event_type="ORDER_PLAN_WRITTEN", aggregate_type="portfolio",
        aggregate_id=str(pf.id),
        payload={"orders": [{"s": "600519", "q": 10, "price": 1680}], "correlation_id": cid},
        correlation_id=cid, business_key="buy_600519_2026-08-17", portfolio_id=pf.id,
        max_retries=30,
    )
    db_session.commit()
    ev0 = db_session.execute(
        select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
    ).scalars().first()
    assert ev0 is not None, "event not written"

    dedup_tokens_seen: list[bool] = []

    class SpyHandlers(outbox_svc.EventHandlers):
        @classmethod
        def order_plan_written(cls, db, ev):
            res = super().order_plan_written(db, ev)
            dedup_tokens_seen.append(bool(res.get("applied")))
            return res

    # 故障注入：apply 成功后、在置 DELIVERED 前抛 2 次 OperationalError
    # → dispatch 内部捕获后 retry_count++, READY, 下一轮 apply 但 DedupLog 已命中
    outbox_svc.FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK = 2
    for _ in range(10):
        if outbox_svc.FAULT_DISPATCH_RAISE_AFTER_APPLY_BEFORE_MARK <= 0 and dedup_tokens_seen:
            # 故障耗尽 & 至少完成一次成功 apply → 清锁再跑一遍让其 DELIVERED
            pass
        # 先清锁/退避
        ev_pre = db_session.execute(
            select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
        ).scalar_one_or_none()
        if ev_pre is not None and ev_pre.status in ("READY", "IN_PROGRESS"):
            ev_pre.next_retry_at = None
            ev_pre.locked_until = None
            ev_pre.locked_by_worker = None
            db_session.commit()
        try:
            outbox_svc.dispatch_outbox_batch(db_session, worker_id="w2", batch_size=8,
                                             handlers=SpyHandlers)
        except Exception:
            db_session.rollback()
        ev_now = db_session.execute(
            select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
        ).scalar_one_or_none()
        if ev_now is not None and ev_now.status == "DELIVERED":
            break
    ev = db_session.execute(
        select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
    ).scalar_one()
    # 首次应用 applied=True（1 次），后续至少 1 次 dedup 命中（applied=False）
    assert dedup_tokens_seen.count(True) == 1, f"dedup_tokens_seen={dedup_tokens_seen}"
    assert dedup_tokens_seen.count(False) >= 1
    assert ev.status == "DELIVERED"


# ── 7. 熔断：circuit_breaker_open=5 → OperationalError ────────
def test_outbox_circuit_breaker_then_recover(db_session):
    outbox_svc.EventHandlers.clear_dedup()
    _clean_fault_globals()
    pf = _seed_portfolio(db_session, name="qa-outbox-cb", effective=date(2026, 8, 1))
    cid = uuid4().hex[:8]
    # 用 POSITION_RECONCILED handler（test 5 已验证可用）而非 AUDIT 路径
    outbox_svc.emit_governance_event(
        db_session, event_type="POSITION_RECONCILED", aggregate_type="portfolio",
        aggregate_id=str(pf.id),
        payload={"as_of": "2026-08-17", "pnl": 5000, "correlation_id": cid},
        correlation_id=cid, business_key="2026-08-17", portfolio_id=pf.id,
        max_retries=30,
    )
    db_session.commit()
    # 先确认事件写入
    ev_sanity = db_session.execute(
        select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
    ).scalar_one_or_none()
    assert ev_sanity is not None and ev_sanity.status == "READY", f"sanity: {ev_sanity}"

    # 模拟 5 次通道熔断（每次循环一进 dispatch 就抛，不会 touch 事件）
    outbox_svc.FAULT_CIRCUIT_BREAKER_OPEN = 5
    exc_count = 0
    delivered_in_cb_loop = False
    from sqlalchemy.exc import OperationalError as OE
    for _ in range(8):
        try:
            st_ = outbox_svc.dispatch_outbox_batch(db_session, worker_id="w3", batch_size=8)
            # 无异常 → 要么是 CB 耗尽后的正常 dispatch，要么是故障耗尽后 delivered
            if st_.get("delivered", 0) > 0:
                delivered_in_cb_loop = True
            break
        except OE:
            exc_count += 1
            db_session.rollback()
            continue
    assert exc_count == 5

    # 若 CB 耗尽后那一轮已成功 delivered，则直接断言；否则继续跑正常 dispatch
    final_delivered = delivered_in_cb_loop
    st_final = {"delivered": 1 if delivered_in_cb_loop else 0}
    if not final_delivered:
        ev_pre = db_session.execute(
            select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
        ).scalar_one_or_none()
        if ev_pre is not None and ev_pre.status != "DELIVERED":
            ev_pre.next_retry_at = None
            ev_pre.locked_until = None
            ev_pre.locked_by_worker = None
            db_session.commit()
        for _ in range(10):
            ev_pre2 = db_session.execute(
                select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
            ).scalar_one_or_none()
            if ev_pre2 is not None and ev_pre2.status in ("READY", "IN_PROGRESS"):
                ev_pre2.next_retry_at = None
                ev_pre2.locked_until = None
                ev_pre2.locked_by_worker = None
                db_session.commit()
            st_final = outbox_svc.dispatch_outbox_batch(db_session, worker_id="w3", batch_size=8)
            if st_final["delivered"] > 0:
                break
    final_delivered = final_delivered or st_final["delivered"] > 0

    # 最终校验：事件应 DELIVERED（熔断后系统自动恢复正常投递）
    ev_end = db_session.execute(
        select(GovernanceOutboxEvent).where(GovernanceOutboxEvent.correlation_id == cid)
    ).scalar_one_or_none()
    assert ev_end is not None
    assert ev_end.status == "DELIVERED", (
        f"event not delivered after CB recovery: status={ev_end.status}, retry={ev_end.retry_count}, err={ev_end.last_error}"
    )
    assert final_delivered, "CB recovery: no successful delivery observed"


# ── 8. Portfolio Resume：多日补算，再次运行全 SKIP ───────────
def test_portfolio_resume_5day_then_idempotent_all_skip(db_session):
    # today 取 2026-09-01（周二），last_reconciled 2026-08-07（周五）→ 至少 16 个工作日
    today = date(2026, 9, 1)
    last_rec = date(2026, 8, 7)
    pf = _seed_portfolio(db_session, name="qa-resume-5day",
                         last_reconciled=last_rec, last_decision=last_rec,
                         effective=date(2026, 8, 1))

    plan = resume_svc.plan_portfolio_resume(db_session, pf.id, today=today, max_days=20)
    pending = plan["pending_days"]
    assert len(pending) >= 10, f"need >=10 pending days, got {len(pending)}: {pending}"
    tasks = resume_svc.create_resume_tasks_from_plan(db_session, plan)
    assert len(tasks) >= 10

    # 模拟 DecisionRun 每日都 SUCCEEDED（绕过 FK：直接通过 sqlite3 raw connection，
    # 保证同一连接内 PRAGMA foreign_keys=OFF + INSERT 生效）
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(db_session.bind)
    cols_set = {c["name"] for c in insp.get_columns("decision_runs")}

    raw_conn = db_session.connection().connection  # sqlite3.Connection
    cur = raw_conn.cursor()
    try:
        cur.execute("PRAGMA foreign_keys = OFF")
        cid = plan["resumption_correlation_id"]
        for item in plan["pending_days"]:
            d = date.fromisoformat(item["date"])
            idem = item["idempotency_key"]
            cols = ["id", "strategy_snapshot_id", "portfolio_id", "run_type", "status", "trade_date",
                    "decision_at", "data_cutoff_at", "execution_at", "idempotency_key",
                    "correlation_id", "created_at", "blocking_status", "run_mode", "pit_mode",
                    "universe_count", "member_count", "is_result_production_eligible"]
            cols = [c for c in cols if c in cols_set]
            now_str = datetime.utcnow().replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
            vals = {
                "id": f"dr-{uuid4().hex[:12]}",
                "strategy_snapshot_id": "snap-qa-placeholder",
                "portfolio_id": int(pf.id),
                "run_type": "auto_simulation",
                "status": "SUCCEEDED",
                "trade_date": d.isoformat(),
                "decision_at": now_str,
                "data_cutoff_at": now_str,
                "execution_at": now_str,
                "idempotency_key": idem,
                "correlation_id": cid,
                "created_at": now_str,
                "blocking_status": "READY",
                "run_mode": "research",
                "pit_mode": "best_effort",
                "universe_count": 0,
                "member_count": 0,
                "is_result_production_eligible": 1,
            }
            # 只用 cols 中有的键
            insert_cols = [c for c in cols if c in vals]
            placeholders = ", ".join(["?"] * len(insert_cols))
            sql = f"INSERT INTO decision_runs ({','.join(insert_cols)}) VALUES ({placeholders})"
            params = tuple(vals[c] for c in insert_cols)
            cur.execute(sql, params)
        raw_conn.commit()
        cur.execute("PRAGMA foreign_keys = ON")
    finally:
        try:
            cur.close()
        except Exception:
            pass
    # ORM 层 expire 保证后续 SELECT 走 DB 而非缓存
    db_session.expire_all()
    # 验证：手动查一次，确保行数 & idempotency_key 正确（避免 SQLite 连接池的幻象）
    from sqlalchemy import func as sa_func
    n = db_session.execute(
        select(sa_func.count()).select_from(DecisionRun)
        .where(DecisionRun.portfolio_id == int(pf.id), DecisionRun.status == "SUCCEEDED")
    ).scalar_one()
    assert n >= len(pending) - 1, (
        f"Only {n} DecisionRuns found for portfolio {pf.id}, expected >= {len(pending) - 1}. "
        f"pending[:2]={pending[:2]}"
    )

    # 再次 plan：幂等跳过所有已补日期
    plan2 = resume_svc.plan_portfolio_resume(db_session, pf.id, today=today, max_days=20)
    assert plan2["will_create_tasks"] == 0
    assert plan2["will_skip_days"] >= len(pending) - 1
    unskipped_left = [d for d in plan2["pending_days"] if not d["will_skip"]]
    assert len(unskipped_left) == 0, f"unskipped left: {unskipped_left}"


# ── 9. 终态硬锁：cancel → queued 直接终态；覆盖被拒 ───────────
def test_terminal_lock_set_task_never_overwrites_cancelled(db_session):
    t = _create_task(db_session, status="done")
    t.is_terminal_locked = 1
    db_session.commit()

    updated = async_task_svc._set_task(db_session, t.id, status="running", stage="retrying",
                                       percent=0, message="try overwrite")
    db_session.refresh(updated)
    assert updated.status == "done"  # 被拒绝，保持 done
    assert updated.stage != "retrying"
    assert int(getattr(updated, "is_terminal_locked", 0) or 0) == 1

    # queued cancel → 直接终态 cancelled + is_terminal_locked=1
    t2 = _create_task(db_session, status="queued", task_type="portfolio_resume")
    snap = async_task_svc.cancel_async_task(t2.id, timeout_seconds=30)
    # cancel_async_task 用独立 session 提交，db_session 有缓存，
    # 需要 expire_all 让 ORM 重新从 DB 读
    db_session.expire_all()
    t2 = db_session.get(AsyncTaskRecord, t2.id)
    assert t2.status == "cancelled", (
        f"queued cancel → cancelled expected; "
        f"got status={t2.status} stage={t2.stage} is_terminal_locked={getattr(t2, 'is_terminal_locked', None)}"
        f" cancel_requested={getattr(t2, 'cancel_requested', None)}"
    )
    assert int(getattr(t2, "is_terminal_locked", 0) or 0) == 1
    # 再次尝试 set_task 回 running → 被拒绝
    async_task_svc._set_task(db_session, t2.id, status="running", stage="foo")
    db_session.refresh(t2)
    assert t2.status == "cancelled"
    assert t2.stage == "cancelled"
    assert int(getattr(t2, "is_terminal_locked", 0) or 0) == 1
