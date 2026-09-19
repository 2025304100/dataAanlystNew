"""挖掘向导 · 字段异步校验路由（SD-v2.0 §6.15；任务 T14）。

四个端点
========
- `POST /factor-mining/validations`                   创建（或幂等复用）
- `GET  /factor-mining/validations/{task_id}`         进度 + 报告（报告从库里读）
- `GET  /factor-mining/validations/{task_id}/valid`   24h 有效期判定
- `POST /factor-mining/validations/{task_id}/resume`  断点续跑
- `POST /factor-mining/validations/{task_id}/pause`   暂停（复用既有 cancel 语义）

⚠️ 本文件只做 DTO 转换与入参校验（R5）；业务全在
`app/services/factors/mining/validation_service.py`。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.async_tasks import cancel_async_task
from app.services.factors.mining import validation_service as VS

router = APIRouter()


class ValidationCreate(BaseModel):
    """创建（或复用）校验任务入参。"""

    draft_id: str = Field(..., min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict,
                                   description="Step3 全量配置（哈希去重的键）")
    fields: list[Any] = Field(..., min_length=1,
                              description="勾选的字段（字符串或 {field: ...}）")
    operator_id: str = "system"


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error_code": "NOT_FOUND",
            "title_zh": "校验任务不存在",
            "detail_zh": f"未找到字段校验任务 {task_id}。",
            "impact": "本次查询无结果",
            "fix_link": "/settings/factor-mining?step=3",
            "retryable": False,
        },
    )


@router.post("/factor-mining/validations", status_code=201)
def create_validation(payload: ValidationCreate,
                      db: Session = Depends(get_db)) -> dict[str, Any]:
    """创建（或**幂等复用**）一次字段校验任务。

    同一 `draft_id + config_hash` 已有活动任务（queued/running）时**复用**返回
    （`reused=true`），不重复创建（需求 §3.5 配置哈希去重）。
    校验不占 `mining_domain`；只读 DuckDB，也不需要 `duckdb_write`。
    """
    try:
        return VS.create_or_reuse_validation(
            draft_id=payload.draft_id, config=payload.config,
            fields=payload.fields, operator_id=payload.operator_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR", "detail_zh": str(exc),
        }) from exc


@router.get("/factor-mining/validations/{task_id}")
def get_validation(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """进度 + 报告（报告保存在库里，不是前端临时态）。"""
    progress = VS.get_validation_progress(db, task_id)
    if not progress.get("found"):
        raise _not_found(task_id)
    return progress


@router.get("/factor-mining/validations/{task_id}/valid")
def validation_is_valid(task_id: str,
                        db: Session = Depends(get_db)) -> dict[str, Any]:
    """24h 有效期判定（过期后 Step4 提交被阻断的依据）。"""
    progress = VS.get_validation_progress(db, task_id)
    if not progress.get("found"):
        raise _not_found(task_id)
    return {
        "task_id": task_id,
        "valid": VS.is_validation_valid(db, task_id),
        "valid_until": progress.get("valid_until"),
        "ttl_hours": VS.VALIDATION_TTL_HOURS,
        "verdict": (progress.get("report") or {}).get("verdict"),
    }


@router.post("/factor-mining/validations/{task_id}/resume")
def resume_validation(
    task_id: str,
    operator_id: str = Query(default="system"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """断点续跑：对 failed/cancelled 任务以**相同 payload** 新建任务。

    已完成分片由 `batch_recovery_json` 天然跳过（不重复执行）。
    """
    try:
        return VS.resume_validation(task_id=task_id, operator_id=operator_id)
    except ValueError as exc:
        raise _not_found(task_id) from exc


@router.post("/factor-mining/validations/{task_id}/pause")
def pause_validation(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """暂停校验。

    复用 `cancel_async_task` 既有语义（任务卡明确：不改 async_tasks 既有语义），
    不新增状态机分支；续跑用 `POST .../resume`，已完成分片不会重复。
    """
    progress = VS.get_validation_progress(db, task_id)
    if not progress.get("found"):
        raise _not_found(task_id)
    if progress["status"] in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail={
            "error_code": "BUSINESS_BLOCKED",
            "title_zh": "任务已结束",
            "detail_zh": f"校验任务 {task_id} 已是终态（{progress['status']}），无法暂停。",
        })
    read = cancel_async_task(task_id)
    return {"task_id": task_id, "status": read.status,
            "completed_shards": progress.get("completed_shards"),
            "total_shards": progress.get("total_shards")}
