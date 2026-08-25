"""FR-P1-2 Portfolio Resume：故障/中断后逐日补算，以 idempotency_key 防重复。

核心逻辑：
1. 找出 next_unprocessed_trade_date：
   - base = 取 max(last_reconciled_trade_date, last_decision_trade_date, effective_start_date-1d, created_at.date()-1d)
   - next = base 的下一交易日（通过 market_calendar 查）。
   - 若 next 超过 today() → 无待补交易日，返回空计划。
2. 每日补算使用 **相同 idempotency_key**（resume|portfolio_id|trade_date|fixed_hash），
   再叠加 async_tasks.idempotency_key 防重复创建任务；
   同时如果 decision_runs 上同 (portfolio_id, trade_date, idempotency_key) 已有成功结果，
   则跳过该日（AC-10：绝不重复下单/重复成交）。
3. 补算任务 payload 中明确写 `correlation_id` 与 `resume_mode=true`，
   使 Outbox/DedupLog 可跨日串联。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.portfolio import Portfolio
from app.models.decision_engine import DecisionRun
from app.models.async_task import AsyncTaskRecord

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_cid() -> str:
    return uuid4().hex[:8]


# ---------------------------------------------------------------------------
# 交易日历辅助（从 market_calendar 表查；如无则回退"周一~周五"弱近似）
# ---------------------------------------------------------------------------
def _next_trade_date(db: Session, after: date) -> date | None:
    """返回 `after` 之后的第一个交易日。"""
    try:
        from app.models.market_data import MarketCalendar
        stmt = (
            select(MarketCalendar.trade_date)
            .where(MarketCalendar.trade_date > after, MarketCalendar.is_trading_day == 1)
            .order_by(MarketCalendar.trade_date.asc())
            .limit(1)
        )
        row = db.execute(stmt).scalar_one_or_none()
        if row is not None:
            return row
    except Exception:
        pass

    # 弱回退：扫 1~14 天，取第一个周一到周五（不含 10/1、5/1 等假期，但 resume 前若
    # 有 DB 应该走上面分支；这里兜底仅用于单测或无 market_calendar 场景）
    d = after + timedelta(days=1)
    for _ in range(30):
        if d.weekday() < 5:
            return d
        d += timedelta(days=1)
    return None


def _is_trade_date(db: Session, d: date) -> bool:
    try:
        from app.models.market_data import MarketCalendar
        stmt = select(MarketCalendar).where(
            MarketCalendar.trade_date == d,
            MarketCalendar.is_trading_day == 1,
        )
        if db.execute(stmt).first() is not None:
            return True
    except Exception:
        return d.weekday() < 5
    return False


# ---------------------------------------------------------------------------
# idempotency_key 计算
# ---------------------------------------------------------------------------
def resume_idempotency_key(portfolio_id: int, trade_date: date, resumption_cid: str) -> str:
    """恢复模式下（portfolio + 某交易日）幂等键稳定值，跨 resume 调用不变。

    说明：跨不同 cid 的多次 resume，对同 (portfolio_id, trade_date) 必须给出相同
    idempotency_key，否则 decision_already_succeeded 的 where idempotency_key=X
    会不命中，导致重复下单。因此把 core 的 sha1 作为 suffix（resumption_cid 参数
    仅保留 API 兼容，不再参与哈希，以便旧代码调用不报错）。
    """
    core = f"resume|{int(portfolio_id)}|{trade_date.isoformat()}|v1"
    return f"{core}|{hashlib.sha1(core.encode('utf-8')).hexdigest()[:12]}"


def _decision_dedup_token(portfolio_id: int, trade_date: date, idem_key: str) -> str:
    return f"decision_run|portfolio={portfolio_id}|d={trade_date.isoformat()}|idem={idem_key}"


# ---------------------------------------------------------------------------
# next_unprocessed_trade_date 计算
# ---------------------------------------------------------------------------
def compute_next_unprocessed_trade_date(db: Session, portfolio: Portfolio, *,
                                        today: date | None = None) -> date | None:
    """根据 portfolio 锚点列计算下一个未处理的交易日。

    定义（对齐 project_memory）：
    - 若对账有进展（last_reconciled_trade_date 非空）→ next = reconciliation 下一日
    - 否则若决策有进展（last_decision_trade_date 非空）→ next = 决策完成日下一日
    - 否则 next = effective_start_date 或 created_at.date() 或今天 - 20 天 的下一交易日
    - 若 next > 今天 → 无待处理日（返回 None）。
    """
    today = today or datetime.now(timezone.utc).date()
    # An explicit progress marker is authoritative.  Using ``max`` across
    # these fields lets a newly-created row's ``created_at`` move the resume
    # anchor past an older reconciliation/decision checkpoint, silently
    # skipping valid historical days.  Only use the later fallbacks when no
    # explicit progress marker exists.
    if portfolio.last_reconciled_trade_date is not None:
        base = portfolio.last_reconciled_trade_date
    elif portfolio.last_decision_trade_date is not None:
        base = portfolio.last_decision_trade_date
    elif portfolio.effective_start_date is not None:
        base = portfolio.effective_start_date - timedelta(days=1)
    elif portfolio.created_at is not None:
        # naive vs tz-aware → date()
        ca = portfolio.created_at
        if ca.tzinfo is not None:
            ca = ca.astimezone(timezone.utc).replace(tzinfo=None)
        base = ca.date() - timedelta(days=1)
    else:
        base = today - timedelta(days=20)
    nxt = _next_trade_date(db, base)
    if nxt is None or nxt > today:
        return None
    return nxt


def compute_pending_trade_dates(db: Session, portfolio: Portfolio, *,
                                today: date | None = None,
                                max_days: int = 20) -> list[date]:
    """列出所有待补算的交易日（最多 max_days 个）。用于 UI 展示或 resume 批量执行。"""
    today = today or datetime.now(timezone.utc).date()
    out: list[date] = []
    current = compute_next_unprocessed_trade_date(db, portfolio, today=today)
    while current is not None and len(out) < max_days:
        out.append(current)
        current = _next_trade_date(db, current)
        if current is None or current > today:
            break
    return out


# ---------------------------------------------------------------------------
# 决策/任务去重：同 idempotency_key 不重复
# ---------------------------------------------------------------------------
def decision_already_succeeded(db: Session, portfolio_id: int, trade_date: date,
                               idempotency_key: str) -> DecisionRun | None:
    """若同 portfolio + trade_date + idempotency_key 的 DecisionRun 已成功，返回之。

    （真实生产环境可把 idempotency_key 作为 DecisionRun 唯一约束的一部分；
     这里在 ORM 层做 SELECT 前置防御即可满足 AC-10「绝不重复下单」。）
    """
    stmt = (
        select(DecisionRun)
        .where(
            DecisionRun.portfolio_id == int(portfolio_id),
            DecisionRun.trade_date == trade_date,
            DecisionRun.status == "SUCCEEDED",
            DecisionRun.idempotency_key == idempotency_key,
        )
        .order_by(DecisionRun.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def async_task_already_exists_for_resume(db: Session, portfolio_id: int, trade_date: date,
                                         idempotency_key: str) -> AsyncTaskRecord | None:
    """同 (portfolio_id, trade_date, idempotency_key) 的 async_task 已存在则返回之。

    恢复接口 / portoflio_auto_simulation 扫描都会先查本函数，避免任务重复创建。
    """
    stmt = (
        select(AsyncTaskRecord)
        .where(
            AsyncTaskRecord.idempotency_key == idempotency_key,
            AsyncTaskRecord.task_type.in_(("portfolio_backtest", "portfolio_auto_run", "portfolio_resume")),
            AsyncTaskRecord.status.in_(("queued", "running", "done")),
        )
        .order_by(AsyncTaskRecord.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Public：plan_resume & execute_resume_plan（返回结构化结果）
# ---------------------------------------------------------------------------
def plan_portfolio_resume(db: Session, portfolio_id: int, *,
                          today: date | None = None,
                          max_days: int = 20,
                          operator_id: str = "system:resume",
                          ) -> dict[str, Any]:
    """生成组合恢复计划（不实际跑任务，仅返回待补日期与幂等 token 去重结果）。

    返回：
    {
      "portfolio_id": int,
      "resumption_correlation_id": str,
      "today": date.iso,
      "next_unprocessed_trade_date": str | None,
      "pending_days": [{"date": str, "decision_done": bool, "task_exists": bool, "will_skip": bool, "idempotency_key": str}],
      "will_create_tasks": int,
      "will_skip_days": int,
      "max_days": int,
    }
    """
    portfolio = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    cid = _gen_cid()
    days = compute_pending_trade_dates(db, portfolio, today=today, max_days=max_days)
    pending_days: list[dict] = []
    will_create = 0
    will_skip = 0
    today_out = today or datetime.now(timezone.utc).date()
    next_un = compute_next_unprocessed_trade_date(db, portfolio, today=today_out)
    for d in days:
        idem = resume_idempotency_key(portfolio.id, d, cid)
        dec_done = decision_already_succeeded(db, portfolio.id, d, idem) is not None
        task_ex = async_task_already_exists_for_resume(db, portfolio.id, d, idem) is not None
        will_skip_ = dec_done or task_ex
        if will_skip_:
            will_skip += 1
        else:
            will_create += 1
        pending_days.append({
            "date": d.isoformat(),
            "decision_done": dec_done,
            "task_exists": task_ex,
            "will_skip": will_skip_,
            "idempotency_key": idem,
        })
    return {
        "portfolio_id": portfolio.id,
        "resumption_correlation_id": cid,
        "today": today_out.isoformat(),
        "next_unprocessed_trade_date": next_un.isoformat() if next_un else None,
        "pending_days": pending_days,
        "will_create_tasks": will_create,
        "will_skip_days": will_skip,
        "max_days": max_days,
        "operator_id": operator_id,
    }


def create_resume_tasks_from_plan(db: Session, plan: dict[str, Any], *,
                                  payload_builder: Callable | None = None,
                                  task_type: str = "portfolio_resume",
                                  ) -> list[dict[str, Any]]:
    """根据 plan_resume 结果，为每个不 skip 的日期创建 async_tasks。

    payload_builder(portfolio_id, trade_date, idempotency_key, correlation_id) 用于
    自定义 payload（真实场景会构造 portfolio_auto_run / decision_engine 任务 payload）。
    默认构造一个最小 payload 供测试。
    """
    from app.services.async_tasks import create_async_task, _compute_idempotency_key  # noqa: F401
    created: list[dict[str, Any]] = []
    portfolio_id = int(plan["portfolio_id"])
    cid = plan["resumption_correlation_id"]
    for item in plan["pending_days"]:
        if item["will_skip"]:
            continue
        trade_date = date.fromisoformat(item["date"])
        idem = item["idempotency_key"]
        # 若相同 idempotency_key 已在 async_tasks 存在（另一线程抢跑）→ 跳过
        if async_task_already_exists_for_resume(db, portfolio_id, trade_date, idem) is not None:
            continue
        base_payload: dict[str, Any] = {
            "resume_mode": True,
            "portfolio_id": portfolio_id,
            "trade_date": trade_date.isoformat(),
            "resumption_correlation_id": cid,
            "idempotency_key": idem,
            "operator_id": plan.get("operator_id", "system:resume"),
            "phase": "daily_fill_gap",
        }
        if payload_builder is not None:
            base_payload.update(payload_builder(portfolio_id, trade_date, idem, cid))
        task = create_async_task(task_type, base_payload)
        # 强制以稳定 idem 写回（create_async_task 自己算的可能不同；这里覆盖）
        db.commit()
        t = db.get(AsyncTaskRecord, task.id)
        if t is not None:
            if t.idempotency_key != idem:
                t.idempotency_key = idem
            if not t.correlation_id:
                t.correlation_id = cid
            db.commit()
            db.refresh(t)
            created.append({
                "task_id": t.id,
                "trade_date": trade_date.isoformat(),
                "idempotency_key": t.idempotency_key,
                "correlation_id": t.correlation_id,
                "status": t.status,
            })
        else:
            created.append({
                "task_id": task.id,
                "trade_date": trade_date.isoformat(),
                "idempotency_key": idem,
                "correlation_id": task.correlation_id or cid,
                "status": task.status,
            })
    return created


# ---------------------------------------------------------------------------
# 便捷：独立 Session 做一次 plan（供 REST 调用）
# ---------------------------------------------------------------------------
def run_resume_plan_api(portfolio_id: int, *, today: date | None = None, max_days: int = 20,
                        operator_id: str = "system:resume") -> dict[str, Any]:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        return plan_portfolio_resume(db, portfolio_id, today=today, max_days=max_days, operator_id=operator_id)
    finally:
        try:
            db.close()
        except Exception:
            pass


def run_resume_execute_api(portfolio_id: int, *, today: date | None = None, max_days: int = 20,
                           operator_id: str = "system:resume",
                           task_type: str = "portfolio_resume",
                           payload_builder=None,
                           ) -> dict[str, Any]:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        plan = plan_portfolio_resume(db, portfolio_id, today=today, max_days=max_days, operator_id=operator_id)
        created = create_resume_tasks_from_plan(db, plan, payload_builder=payload_builder, task_type=task_type)
        plan["created_tasks"] = created
        return plan
    finally:
        try:
            db.close()
        except Exception:
            pass
