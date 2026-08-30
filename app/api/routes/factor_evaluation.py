"""WP5 评估实验室 API 路由。

端点：
- POST /factor-evaluation/preflight：评价前预检（6 项检查）
- POST /factor-evaluation/tasks：创建评估任务
- GET /factor-evaluation/tasks：列出评估任务
- GET /factor-evaluation/tasks/{task_id}：获取任务详情
- POST /factor-evaluation/tasks/{task_id}/cancel：取消任务
- GET /factor-evaluation/runs：列出评估运行记录
- GET /factor-evaluation/runs/{run_id}：获取运行详情（含 metrics 和门禁结论）
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.db.session import SessionLocal
from app.services.factors.factor_evaluator import (
    get_evaluation_run,
    list_evaluation_runs,
)
from app.services.factors.wp5_eval_task import (
    cancel_evaluation_task,
    create_evaluation_task,
    get_evaluation_task,
    list_evaluation_tasks,
    preflight_factor_evaluation,
)


logger = logging.getLogger(__name__)

router = APIRouter()


def _make_correlation_id() -> str:
    """生成 8 位 hex correlation_id（T3-5：用于 5xx 响应 + Worker errors_json）。"""
    return uuid.uuid4().hex[:8]


def _structured_5xx_error(exc: Exception, correlation_id: str, *, endpoint_name: str) -> tuple[int, dict]:
    """构造结构化 5xx 响应（不暴露堆栈）。

    Returns:
        (status_code, response_dict)
    """
    from sqlalchemy.exc import OperationalError as _SAOpErr

    if isinstance(exc, _SAOpErr):
        status_code = 503
        title_zh = "数据库连接异常（请稍后重试）"
        detail = "DB connection unavailable"
        error_code = "eval.db.operational_error"
    else:
        status_code = 500
        title_zh = "服务端未知异常（请联系管理员）"
        detail = "Internal server error"
        error_code = "eval.server.internal_error"

    error_item = {
        "code": error_code,
        "severity": "error",
        "category": "config",
        "title_zh": title_zh,
        "detail_zh": (
            f"{type(exc).__name__}：服务端执行评估请求时出现非预期错误，"
            f"请提供 correlation_id={correlation_id} 给管理员定位问题。"
        ),
        "correlation_id": correlation_id,
        "endpoint": endpoint_name,
        "exception_type": type(exc).__name__,
        "evidence": {"correlation_id": correlation_id},
        "retryable": True,
    }
    body = {
        "detail": detail,
        "error": title_zh,
        "correlation_id": correlation_id,
        "errors_json": [error_item],
    }
    return status_code, body


class FactorEvaluationPreflightRequest(BaseModel):
    """评价预检请求。"""

    factor_code: str = Field(..., description="因子代码")
    factor_version_id: str | None = Field(None, description="因子版本 ID（可选，默认使用最新版本）")
    universe: str = Field("all_a_shares", description="股票池标识")
    start_date: date | None = Field(None, description="评价起始日期（可选，系统自动推算）")
    end_date: date | None = Field(None, description="评价结束日期（可选，系统自动推算）")
    target_horizon: int = Field(5, ge=1, le=60, description="目标收益 horizon（天）")


class PreflightCheckItem(BaseModel):
    """单条预检检查项。"""

    code: str = Field(..., description="稳定检查代码")
    severity: Literal["pass", "warn", "error", "info"] = Field(..., description="严重等级")
    category: Literal["formula", "data", "config", "sample", "pit", "target"] = Field(
        ..., description="检查维度分类"
    )
    title_zh: str = Field(..., description="中文标题")
    detail_zh: str = Field(..., description="中文详细说明")
    evidence: dict = Field(default_factory=dict, description="结构化证据数据")
    fix_link: dict | None = Field(None, description='前端跳转链接: {"tab":"","subtab":"","label_zh":""}')
    retryable: bool = Field(True, description="修复后是否可重试通过")


class FactorEvaluationPreflightResponse(BaseModel):
    """评价预检响应。"""

    overall: dict = Field(
        ...,
        description='总体结论: {"passed": bool, "blocking_count": int, "recommended_date_range": [start,end]|None}',
    )
    items: list[PreflightCheckItem] = Field(..., description="按顺序输出的检查项列表（至少 6 项）")


class EvaluationTaskCreate(BaseModel):
    """创建评估任务请求。"""

    factor_code: str = Field(..., description="因子代码")
    factor_kind: str = Field("continuous", description="因子类型: continuous/event/regime")
    factor_version_id: int | None = Field(None, description="因子版本 ID（可选，默认使用最新版本）")
    universe: str = Field("all_a_shares", description="股票池标识")
    start_date: date | None = Field(None, description="评价起始日期（可选）")
    end_date: date | None = Field(None, description="评价结束日期（可选）")
    target_horizon: int = Field(5, ge=1, le=60, description="目标收益 horizon（天）")
    n_groups: int = Field(5, ge=2, le=10, description="分组数")
    cost_rate: float = Field(0.001, ge=0.0, le=0.01, description="单边成本率")
    direction: str | None = Field(None, description="因子方向: higher_better/lower_better/nonlinear（可选，默认因子版本配置）")
    created_by: str = Field("local_user", description="创建者")
    force_new: bool = Field(False, description="显式重新评估时跳过已完成任务的幂等复用")


@router.post("/factor-evaluation/preflight", response_model=FactorEvaluationPreflightResponse)
def preflight_eval(payload: FactorEvaluationPreflightRequest) -> FactorEvaluationPreflightResponse:
    """执行因子评价前预检（6 项检查，按顺序输出）。"""
    try:
        result = preflight_factor_evaluation(
            factor_code=payload.factor_code,
            factor_version_id=payload.factor_version_id,
            universe=payload.universe,
            start_date=payload.start_date,
            end_date=payload.end_date,
            target_horizon=payload.target_horizon,
        )
        return FactorEvaluationPreflightResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - T3-5: 非预期异常给 correlation_id
        cid = _make_correlation_id()
        logger.exception("[%s] preflight_eval unexpected error", cid)
        status_code, body = _structured_5xx_error(exc, cid, endpoint_name="preflight")
        return JSONResponse(status_code=status_code, content=body)


@router.post("/factor-evaluation/tasks")
def create_eval_task(payload: EvaluationTaskCreate):
    """创建因子评估异步任务。"""
    try:
        return create_evaluation_task(
            factor_code=payload.factor_code,
            factor_kind=payload.factor_kind,
            factor_version_id=payload.factor_version_id,
            universe=payload.universe,
            start_date=payload.start_date,
            end_date=payload.end_date,
            target_horizon=payload.target_horizon,
            n_groups=payload.n_groups,
            cost_rate=payload.cost_rate,
            direction=payload.direction,
            created_by=payload.created_by,
            force_new=payload.force_new,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - T3-5: DB OperationalError 等给 correlation_id
        cid = _make_correlation_id()
        logger.exception("[%s] create_eval_task unexpected error", cid)
        status_code, body = _structured_5xx_error(exc, cid, endpoint_name="create_task")
        return JSONResponse(status_code=status_code, content=body)


@router.get("/factor-evaluation/tasks")
def list_eval_tasks(limit: int = Query(default=20, ge=1, le=100)):
    """列出评估任务。"""
    return list_evaluation_tasks(limit=limit)


@router.get("/factor-evaluation/tasks/{task_id}")
def get_eval_task(task_id: str):
    """获取评估任务详情。"""
    task = get_evaluation_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="evaluation_task_not_found")
    return task


@router.post("/factor-evaluation/tasks/{task_id}/cancel")
def cancel_eval_task(task_id: str):
    """取消评估任务。"""
    try:
        return cancel_evaluation_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/factor-evaluation/runs")
def list_eval_runs(
    factor_version_id: int | None = Query(default=None),
    gate_result: str | None = Query(default=None, pattern="^(passed|rejected|warn)$"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """列出评估运行记录。"""
    db = SessionLocal()
    try:
        runs = list_evaluation_runs(
            db,
            factor_version_id=factor_version_id,
            gate_result=gate_result,
            limit=limit,
        )
        return [_run_to_dict(r) for r in runs]
    finally:
        db.close()


@router.get("/factor-evaluation/runs/{run_id}")
def get_eval_run(run_id: str):
    """获取评估运行详情（含完整 metrics 和门禁结论）。"""
    db = SessionLocal()
    try:
        run = get_evaluation_run(db, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        return _run_to_dict(run)
    finally:
        db.close()


def _run_to_dict(run: Any) -> dict:
    """将 EvaluationRun ORM 转为 dict。"""
    return {
        "id": run.id,
        "factor_version_id": run.factor_version_id,
        "universe_snapshot_id": run.universe_snapshot_id,
        "data_cutoff_at": run.data_cutoff_at.isoformat() if run.data_cutoff_at else None,
        "target_code": run.target_code,
        "train_start_date": run.train_start_date.isoformat() if run.train_start_date else None,
        "train_end_date": run.train_end_date.isoformat() if run.train_end_date else None,
        "validation_start_date": run.validation_start_date.isoformat() if run.validation_start_date else None,
        "validation_end_date": run.validation_end_date.isoformat() if run.validation_end_date else None,
        "config": json.loads(run.config_json) if run.config_json else {},
        "metrics": json.loads(run.metrics_json) if run.metrics_json else {},
        "gate_result": run.gate_result,
        "rejection_reasons": json.loads(run.rejection_reasons_json) if run.rejection_reasons_json else [],
        "artifact_path": run.artifact_path,
        "task_id": run.task_id,
        "created_by": run.created_by,
        "selected_trade_date": run.selected_trade_date,
        "observed_symbols": run.observed_symbols,
        "expected_symbols": run.expected_symbols,
        "completeness_ratio": run.completeness_ratio,
        "fallback_reason": run.fallback_reason,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }
