"""G1-WP0-3c + Q30 G2：DecisionEngine 评估 + 证据查询 API。

路由：
- POST /portfolios/{portfolio_id}/evaluate         （dry-run 可查询；persist=True 落库）
- GET  /decision-runs/{run_id}
- GET  /portfolios/{portfolio_id}/decision-runs    （按组合分页）
- GET  /decision-runs/{run_id}/evidence            （证据分页 + action 过滤，回测拒绝记录 Tab 用）
- GET  /decision-runs/{run_id}/evidence/{symbol_id}（单证券完整解释，按需加载）
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func as sa_func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.decision_engine import (
    DecisionEvidence as DecisionEvidenceORM,
    DecisionOrderPlanRecord,
    DecisionRun as DecisionRunORM,
)
from app.schemas.decision_engine import (
    DecisionEvaluateRequest,
    DecisionEvaluateResponse,
    DecisionEvidenceDetailRead,
    DecisionEvidenceRead,
    DecisionOrderPlanRead,
    DecisionRunRead,
    PreflightWarning,
)
from app.services import decision_engine as de
from app.services.decision_engine import default_engine as _default_engine
from app.services.decision_engine import _run_type_from_db  # noqa: E402
from app.services.decision_schedule_gate import (
    ensure_auto_simulation_schedule,
    build_today_preview_view,
)  # noqa: E402

router = APIRouter()


def _actor(x_user: str | None = Query(default=None, alias="X-User")) -> str:
    return x_user or "local_user"


def _coerce_run_for_schema(row: DecisionRunORM) -> DecisionRunORM:
    """把 DB 层 run_type (dry_run/backtest/auto_simulation) 转为对外枚举后再做 Schema 校验。"""
    coerced = _run_type_from_db(getattr(row, "run_type", None))
    if coerced != getattr(row, "run_type", None):
        # 写一个瞬态副本返回（避免污染 ORM 持久态）
        setattr(row, "run_type", coerced)
    return row


# ──────────────────────────────────────────────────────────── Evaluate
@router.post(
    "/portfolios/{portfolio_id}/evaluate",
    response_model=DecisionEvaluateResponse,
    status_code=200,
    tags=["decision-engine"],
    summary="G1-WP0-3a：统一决策 dry-run / persist 入口",
)
def evaluate_portfolio(
    portfolio_id: int,
    payload: DecisionEvaluateRequest,
    db: Session = Depends(get_db),
    _actor: str = Depends(_actor),
) -> DecisionEvaluateResponse:
    """Q25/WP0-3c：evaluate 编排接口。

    persist=False（默认）：不落 DecisionRun/DecisionEvidence 表，只返回证据。
    persist=True：写入两张表；相同幂等键不报错（Q28）。
    """
    # 确认 portfolio 存在
    from app.models.portfolio import Portfolio
    p = db.get(Portfolio, portfolio_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")

    # 确认 snapshot 存在
    from app.models.decision_engine import StrategyExecutionSnapshot
    snap = db.get(StrategyExecutionSnapshot, payload.strategy_snapshot_id)
    if snap is None:
        raise HTTPException(status_code=404, detail={
            "error": "SNAPSHOT_NOT_FOUND",
            "strategy_snapshot_id": payload.strategy_snapshot_id,
        })
    if snap.portfolio_id != portfolio_id:
        raise HTTPException(status_code=400, detail={
            "error": "SNAPSHOT_PORTFOLIO_MISMATCH",
            "snapshot_portfolio_id": snap.portfolio_id,
            "request_portfolio_id": portfolio_id,
        })

    # WP0-6 T7 决策门禁：auto_simulation 仅允许 hour≥20 且同日唯一
    # （payload.decision_at 显式传入时用它，否则用 server 当前时间做审计）
    from datetime import datetime as _dt
    audit_decision_at = payload.decision_at or _dt.now()
    if payload.run_type == "auto_simulation":  # Schema 层的对外枚举值
        gate = ensure_auto_simulation_schedule(
            audit_decision_at, portfolio_id, db=db,
        )
        if not gate.allowed:
            status_code = 409 if gate.error_code == "DUAL_EXECUTION_RISK_PROHIBITED" else 400
            raise HTTPException(status_code=status_code, detail={
                "error": gate.error_code,
                "message": gate.message,
            })

    result = _default_engine.evaluate(
        db,
        portfolio_id=portfolio_id,
        strategy_snapshot_id=payload.strategy_snapshot_id,
        trade_date=payload.trade_date,
        run_type=payload.run_type,
        dry_run=not payload.persist,
    )
    # WP1-1 Critical Fix：当 _persist 调用 flush 写 DecisionRun/DecisionEvidence 后，
    # 必须显式 commit，否则数据仅存在当前事务，GET /decision-runs/:id 将返回 404。
    if payload.persist and result.persisted:
        try:
            db.commit()
        except Exception as _e:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "DECISION_PERSIST_COMMIT_FAILED",
                    "message": f"DecisionRun 落库 commit 失败：{type(_e).__name__}: {_e}",
                    "decision_run_id": result.decision_run_id,
                },
            ) from _e
    # 转换 evidence（取前 50 条做 preview；更多通过 GET 分页）
    preview_orms: list[DecisionEvidenceORM] = []
    for ev in result.evidence[:50]:
        preview_orms.append(DecisionEvidenceORM(
            id="", decision_run_id=result.decision_run_id,
            strategy_snapshot_id=payload.strategy_snapshot_id,
            portfolio_id=portfolio_id, symbol_id=ev.symbol_id,
            trade_date=payload.trade_date,
            decision_at=result.clock.decision_at,
            data_cutoff_at=result.clock.data_cutoff_at,
            execution_at=result.clock.execution_at,
            action=ev.action, action_subtype=ev.action_subtype,
            target_position_pct=ev.target_position_pct,
            min_lot_size=ev.min_lot_size, target_quantity=ev.target_quantity,
            intended_price=ev.intended_price, executed_price=ev.executed_price,
            slippage_bps=ev.slippage_bps, rejection_reason=ev.rejection_reason,
            rejection_detail=ev.rejection_detail, score_id=ev.score_id,
            score_value=ev.score_value, score_rank=ev.score_rank,
            score_published_at=ev.score_published_at, pit_safe_flag=ev.pit_safe_flag,
            constraints_json=_j(ev.constraints),
            versions_json=None, reason_codes_json=_j(ev.reason_codes),
            factor_contributions_json=_j(ev.factor_contributions),
            content_hash="dry_run_preview_only",
        ))

    warnings: list[PreflightWarning] = [
        PreflightWarning(severity="blocking" if r.get("severity") == "blocking" else "warning",
                         code=str(r.get("code", "UNKNOWN")), message=str(r.get("message", "")),
                         detail=r.get("detail") if isinstance(r.get("detail"), dict | None) else {"raw": r["detail"]})
        for r in result.blocking_reasons
    ]

    return DecisionEvaluateResponse(
        decision_run_id=result.decision_run_id,
        blocking_status=result.blocking_status,
        blocking_reasons=result.blocking_reasons,
        clock={
            "decision_at_utc": result.clock.decision_at.isoformat(),
            "data_cutoff_at_utc": result.clock.data_cutoff_at.isoformat(),
            "execution_at_utc": result.clock.execution_at.isoformat(),
            "decision_at_sh": result.clock.decision_at_sh.isoformat(),
            "data_cutoff_at_sh": result.clock.data_cutoff_at_sh.isoformat(),
            "execution_at_sh": result.clock.execution_at_sh.isoformat(),
        },
        score_coverage_pct=result.score_coverage_pct,
        score_max_age_days=result.score_max_age_days,
        evidence_count=len(result.evidence),
        persisted=result.persisted,
        dry_run=result.dry_run,
        evidence_preview=[DecisionEvidenceRead.model_validate(x) for x in preview_orms],
        order_plans=[DecisionOrderPlanRead.model_validate({
            "order_plan_id": plan.order_plan_id,
            "decision_run_id": plan.decision_run_id,
            "evidence_id": plan.evidence_id,
            "symbol_id": plan.symbol_id,
            "action": plan.action,
            "signal_date": plan.signal_date,
            "execution_date": plan.execution_date,
            "target_quantity": plan.target_quantity,
            "direction": plan.direction,
            "intended_price": plan.intended_price,
            "reason_code": plan.reason_code,
            "rejection_trace": plan.rejection_trace,
        }) for plan in result.order_plans],
        warnings=warnings,
    )


# ──────────────────────────────────────────────────────────── DecisionRun 查询
@router.get(
    "/decision-runs/{run_id}",
    response_model=DecisionRunRead,
    tags=["decision-engine"],
    summary="G1-WP0-3c：按 ID 取单个 DecisionRun 元数据",
)
def get_decision_run(run_id: str, db: Session = Depends(get_db)) -> DecisionRunRead:
    row = db.get(DecisionRunORM, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"DecisionRun {run_id} not found")
    row = _coerce_run_for_schema(row)
    return DecisionRunRead.model_validate(row)


@router.get(
    "/portfolios/{portfolio_id}/decision-runs",
    response_model=dict[str, Any],
    tags=["decision-engine"],
    summary="G1-WP0-3c：按组合分页 DecisionRun（trade_date DESC）",
)
def list_portfolio_decision_runs(
    portfolio_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.scalar(
        select(sa_func.count()).select_from(DecisionRunORM)
        .where(DecisionRunORM.portfolio_id == portfolio_id)
    ) or 0
    rows = db.execute(
        select(DecisionRunORM)
        .where(DecisionRunORM.portfolio_id == portfolio_id)
        .order_by(DecisionRunORM.trade_date.desc(), DecisionRunORM.created_at.desc())
        .limit(limit).offset(offset)
    ).scalars().all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [DecisionRunRead.model_validate(_coerce_run_for_schema(r)) for r in rows],
    }


# ──────────────────────────────────────────────────────────── Today-Preview 纯视图
@router.get(
    "/portfolios/{portfolio_id}/today-preview",
    response_model=dict[str, Any],
    tags=["decision-engine"],
    summary="G2-WP0-6c：今日决策只读视图（不调用DecisionEngine，不写新行，不复制Evidence）",
)
def get_today_preview(
    portfolio_id: int,
    trade_date: date = Query(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    view = build_today_preview_view(db, portfolio_id, trade_date)
    return view.__dict__


# ──────────────────────────────────────────────────────────── Evidence 查询（回测 Tab4 拒绝记录）
@router.get(
    "/decision-runs/{run_id}/evidence",
    response_model=dict[str, Any],
    tags=["decision-engine"],
    summary="G1-WP0-3c：DecisionRun 证据分页；action 过滤（REJECTED/DATA_BLOCKED=拒绝记录 Tab）",
)
def list_run_evidence(
    run_id: str,
    action: str | None = Query(default=None, description="按动作过滤，例如 REJECTED,DATA_BLOCKED 逗号分隔"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    run = db.get(DecisionRunORM, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"DecisionRun {run_id} not found")
    where = [DecisionEvidenceORM.decision_run_id == run_id]
    if isinstance(action, str) and action:
        tokens = [x.strip() for x in action.split(",") if x.strip()]
        if tokens:
            where.append(DecisionEvidenceORM.action.in_(tokens))
    total_stmt = (select(sa_func.count())
                  .select_from(DecisionEvidenceORM)
                  .where(and_(*where)))
    total = db.scalar(total_stmt) or 0
    rows_stmt = (select(DecisionEvidenceORM).where(and_(*where))
                 .order_by(DecisionEvidenceORM.symbol_id.asc())
                 .limit(limit).offset(offset))
    rows = db.execute(rows_stmt).scalars().all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [DecisionEvidenceRead.model_validate(r) for r in rows],
    }


@router.get(
    "/decision-runs/{run_id}/order-plans",
    response_model=dict[str, Any],
    tags=["decision-engine"],
    summary="查询 DecisionRun 的独立订单计划账本",
)
def list_run_order_plans(
    run_id: str,
    action: str | None = Query(default=None, description="按 BUY/SELL/HOLD 等动作过滤"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    run = db.get(DecisionRunORM, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"DecisionRun {run_id} not found")
    where = [DecisionOrderPlanRecord.decision_run_id == run_id]
    if isinstance(action, str) and action:
        tokens = [value.strip() for value in action.split(",") if value.strip()]
        if tokens:
            where.append(DecisionOrderPlanRecord.action.in_(tokens))
    total = db.scalar(
        select(sa_func.count()).select_from(DecisionOrderPlanRecord).where(and_(*where))
    ) or 0
    rows = db.execute(
        select(DecisionOrderPlanRecord)
        .where(and_(*where))
        .order_by(DecisionOrderPlanRecord.symbol_id.asc(), DecisionOrderPlanRecord.order_plan_id.asc())
        .limit(limit).offset(offset)
    ).scalars().all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "order_plan_id": row.order_plan_id,
                "decision_run_id": row.decision_run_id,
                "evidence_id": row.evidence_id,
                "symbol_id": row.symbol_id,
                "action": row.action,
                "signal_date": row.signal_date,
                "execution_date": row.execution_date,
                "target_quantity": row.target_quantity,
                "direction": row.direction,
                "intended_price": row.intended_price,
                "reason_code": row.reason_code,
                "rejection_trace": _json_load(row.rejection_trace_json, []),
            }
            for row in rows
        ],
    }


@router.get(
    "/decision-runs/{run_id}/evidence/{symbol_id}",
    response_model=DecisionEvidenceDetailRead,
    tags=["decision-engine"],
    summary="G1-WP0-3c：单证券完整 DecisionEvidence 解释",
)
def get_run_symbol_evidence(
    run_id: str,
    symbol_id: int,
    db: Session = Depends(get_db),
) -> DecisionEvidenceDetailRead:
    """Return one persisted evidence row scoped to its DecisionRun.

    The run scope is deliberate: a symbol can appear in multiple runs, and a
    drawer must never accidentally display another run's evidence.
    """
    run = db.get(DecisionRunORM, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"DecisionRun {run_id} not found")

    evidence = db.scalar(
        select(DecisionEvidenceORM).where(
            DecisionEvidenceORM.decision_run_id == run_id,
            DecisionEvidenceORM.symbol_id == symbol_id,
        )
    )
    if evidence is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "DECISION_EVIDENCE_NOT_FOUND",
                "decision_run_id": run_id,
                "symbol_id": symbol_id,
            },
        )
    return DecisionEvidenceDetailRead.model_validate(evidence)


# ──────────────────────────────────────────────────────────── helpers
from app.core.hash_utils import canonical_json  # noqa: E402
from sqlalchemy import func as sa_func  # noqa: E402


def _j(obj: Any) -> str | None:
    return canonical_json(obj) if obj is not None and obj != [] and obj != {} else None


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
