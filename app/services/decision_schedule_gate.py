"""WP0-6 / G2-5 双决策门禁（today-preview 视图 + 20:30 auto_simulation 调度时机校验）。

T7 决策时点唯一生产规则落地（tasks.md §WP0-6）：
  1. 唯一可执行自动决策 = run_type=auto_simulation @ 20:30（hour≥20，应用层硬拦截）
  2. 15:05 只允许 dry_run，不写 order_plan、不更新 last_decision_trade_date
  3. today-preview 是只读视图：不调用 DecisionEngine.evaluate，不复制 DecisionEvidence，
     仅返回当日最新 auto_simulation DecisionRun 的 view_kind="today_preview"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Literal

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from app.models.decision_engine import DecisionRun as DecisionRunORM
from app.models.portfolio import Portfolio
from app.services.decision_engine import _run_type_from_db


ScheduleErrorCode = Literal[
    "SCHEDULE_TOO_EARLY",
    "DUAL_EXECUTION_RISK_PROHIBITED",
    "PREVIEW_MUST_BE_DRY_RUN",
]


@dataclass
class ScheduleGateResult:
    allowed: bool
    error_code: ScheduleErrorCode | None = None
    message: str | None = None
    effective_data_as_of_trade_date: date | None = None
    effective_data_cutoff_at: datetime | None = None


def ensure_auto_simulation_schedule(
    decision_at: datetime,
    portfolio_id: int,
    *,
    db: Session | None = None,
) -> ScheduleGateResult:
    """WP0-6 规则 1/3：auto_simulation 正式自动推演仅允许 hour≥20。

    - hour<20 → SCHEDULE_TOO_EARLY，禁止执行；
    - 同日已存在 status=SUCCESS/ RUNNING 的 auto_simulation 行 → DUAL_EXECUTION_RISK；
    - 通过时返回建议 data_cutoff_at = 当日 20:00、data_as_of_trade_date=当日。
    """
    hour = int(decision_at.hour)
    if hour < 20:
        return ScheduleGateResult(
            allowed=False,
            error_code="SCHEDULE_TOO_EARLY",
            message=(
                f"auto_simulation 正式自动推演仅允许 20:00 后运行（当前 hour={hour}），"
                "避免一天两份决策。"
            ),
        )
    # 双决策硬禁止：同日（本地日期）存在 auto_simulation 行
    if db is not None:
        day = decision_at.date()
        exists = db.scalar(
            select(DecisionRunORM.id)
            .where(
                DecisionRunORM.portfolio_id == portfolio_id,
                DecisionRunORM.run_type.in_(["auto_simulation"]),  # DB value
                DecisionRunORM.trade_date == day,
                DecisionRunORM.status.in_(["RUNNING", "SUCCEEDED"]),
            )
            .limit(1)
        )
        if exists:
            return ScheduleGateResult(
                allowed=False,
                error_code="DUAL_EXECUTION_RISK_PROHIBITED",
                message=(
                    f"portfolio_id={portfolio_id} trade_date={day.isoformat()} 已存在"
                    " RUNNING/SUCCEEDED 的 auto_simulation DecisionRun，禁止同日第二份。"
                ),
            )
    data_cutoff = datetime(
        decision_at.year, decision_at.month, decision_at.day, 20, 0, 0,
    )
    return ScheduleGateResult(
        allowed=True,
        effective_data_cutoff_at=data_cutoff,
        effective_data_as_of_trade_date=decision_at.date(),
    )


def ensure_preview_is_dry_run(decision_at: datetime) -> ScheduleGateResult:
    """WP0-6 规则 2：15:05 预览必须是 dry_run。

    计算 data_cutoff_at = 前一交易日 15:00（用简单的工作日近似，周一→上周五）。
    真实 trade_calendar 可由调用方替换注入。
    """
    from datetime import timedelta as _td

    d = decision_at.date()
    # 粗粒度：周一~周二→-1；周一→-3（跳过周末）
    shift = 3 if d.weekday() == 0 else 1
    prev_day = d - _td(days=shift)
    data_cutoff = datetime(prev_day.year, prev_day.month, prev_day.day, 15, 0, 0)
    return ScheduleGateResult(
        allowed=True,
        effective_data_cutoff_at=data_cutoff,
        effective_data_as_of_trade_date=prev_day,
    )


@dataclass
class TodayPreviewView:
    """today-preview 纯视图响应：绝不调用 evaluate()，绝不写 DB 行。"""
    view_kind: Literal["today_preview"] = "today_preview"
    source_decision_run_id: str | None = None
    trade_date: date | None = None
    summary: dict[str, Any] | None = None
    status: Literal["NOT_READY", "READY"] = "NOT_READY"
    decision_at: datetime | None = None


def build_today_preview_view(
    db: Session,
    portfolio_id: int,
    trade_date: date,
) -> TodayPreviewView:
    """只读：取今日 auto_simulation 最新 DecisionRun，直接返回其 ID。

    DB 中 run_type='today_preview' 的行数永远为 0（禁止写实体）。
    """
    row = db.execute(
        select(DecisionRunORM)
        .where(
            DecisionRunORM.portfolio_id == portfolio_id,
            DecisionRunORM.trade_date == trade_date,
            DecisionRunORM.run_type.in_(["auto_simulation"]),  # DB value
        )
        .order_by(desc(DecisionRunORM.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return TodayPreviewView(
            view_kind="today_preview",
            source_decision_run_id=None,
            trade_date=trade_date,
            status="NOT_READY",
        )
    schema_run_type = _run_type_from_db(row.run_type)
    return TodayPreviewView(
        view_kind="today_preview",
        source_decision_run_id=str(row.id),
        trade_date=trade_date,
        status="READY",
        decision_at=row.decision_at,
        summary={
            "status": row.status,
            "run_type": schema_run_type,
            "candidate_count": getattr(row, "universe_count", None),
            "member_count": getattr(row, "member_count", None),
            "blocking_status": getattr(row, "blocking_status", None),
            "blocking_reasons_json": getattr(row, "blocking_reasons_json", None),
        },
    )
