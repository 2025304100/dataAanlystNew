"""WP5 评估实验室 API 路由。

端点：
- POST /factor-evaluation/tasks：创建评估任务
- GET /factor-evaluation/tasks：列出评估任务
- GET /factor-evaluation/tasks/{task_id}：获取任务详情
- POST /factor-evaluation/tasks/{task_id}/cancel：取消任务
- GET /factor-evaluation/runs：列出评估运行记录
- GET /factor-evaluation/runs/{run_id}：获取运行详情（含 metrics 和门禁结论）
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
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
)


router = APIRouter()


class EvaluationTaskCreate(BaseModel):
    """创建评估任务请求。"""

    factor_code: str = Field(..., description="因子代码")
    factor_kind: str = Field("continuous", description="因子类型: continuous/event/regime")
    target_horizon: int = Field(5, ge=1, le=60, description="目标收益 horizon（天）")
    n_groups: int = Field(5, ge=2, le=10, description="分组数")
    cost_rate: float = Field(0.001, ge=0.0, le=0.01, description="单边成本率")
    created_by: str = Field("local_user", description="创建者")


@router.post("/factor-evaluation/tasks")
def create_eval_task(payload: EvaluationTaskCreate):
    """创建因子评估异步任务。"""
    try:
        return create_evaluation_task(
            factor_code=payload.factor_code,
            factor_kind=payload.factor_kind,
            target_horizon=payload.target_horizon,
            n_groups=payload.n_groups,
            cost_rate=payload.cost_rate,
            created_by=payload.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
