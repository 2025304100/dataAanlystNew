"""数据中心 · 历史行情镜像路由（向导 §10；任务 T15）。

端点
====
- `GET  /data-mirror/status`                当前镜像状态（区间/行数/标的/低覆盖年份）
- `POST /data-mirror/tasks`                 创建镜像任务（范围预设 + 磁盘检查 + 估算）
- `GET  /data-mirror/tasks`                 最近任务列表
- `GET  /data-mirror/tasks/{task_id}`       进度 / 分块 / 报告
- `POST /data-mirror/tasks/{task_id}/cancel` 取消（分块边界自止；已完成块保留）

⚠️ 只做 DTO 转换与入参校验（R5）；业务全在 `mirror_task.py`。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.async_tasks import cancel_async_task
from app.services.factors import mirror_task as MT

router = APIRouter()


class MirrorCreate(BaseModel):
    """创建镜像任务入参（向导 §10.1 范围选择）。"""

    preset: str = Field(default="5y", description="5y | 10y | full | custom")
    start_date: date | None = Field(default=None, description="preset=custom 必填")
    end_date: date | None = Field(default=None, description="preset=custom 必填")
    batch_size: int = Field(default=1000, ge=1, le=50_000)
    skip_disk_check: bool = Field(default=False, description="仅测试/运维用")
    operator_id: str = "system"


def _duckdb_write_busy(exc: RuntimeError) -> HTTPException:
    """worker/提交层抛的 JSON 串 → 409（冲突方信息透传）。"""
    try:
        detail = json_loads(str(exc))
    except Exception:  # noqa: BLE001
        detail = {"error_code": "DUCKDB_WRITE_BUSY"}
    return HTTPException(status_code=409, detail={
        "error_code": "DUCKDB_WRITE_BUSY",
        "title_zh": "DuckDB 写通道被占用",
        "detail_zh": (
            f"当前有任务正在写入 DuckDB（owner={detail.get('owner_task_id')}），"
            "镜像被拒绝且未创建任务。请等待其完成后重试。"
        ),
        "impact": "本次提交被拒绝，未创建任务",
        "fix_link": "/settings/data-center",
        "retryable": True,
        "extras": detail,
    })


def json_loads(raw: str) -> dict[str, Any]:
    import json

    return json.loads(raw)


def _not_found(task_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail={
        "error_code": "NOT_FOUND",
        "title_zh": "镜像任务不存在",
        "detail_zh": f"未找到镜像任务 {task_id}。",
        "fix_link": "/settings/data-center",
    })


@router.get("/data-mirror/status")
def mirror_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """当前状态：已镜像区间 / 行数 / 标的数 / 低覆盖年份（向导 §10.1、§10.2）。"""
    return MT.get_mirror_status(db)


@router.post("/data-mirror/tasks", status_code=201)
def create_mirror_task(payload: MirrorCreate,
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    """创建镜像任务。

    - `duckdb_write` 被占 → **409**，未创建任务（镜像持有该锁，重活冲突不排队）
    - 磁盘剩余不足 → 400（阻断，含所需空间提示）
    - 返回体含**估算**行数 / 耗时（明确标注估算）
    """
    try:
        return MT.create_mirror_task(
            preset=payload.preset, start_date=payload.start_date,
            end_date=payload.end_date, batch_size=payload.batch_size,
            operator_id=payload.operator_id,
            skip_disk_check=payload.skip_disk_check,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error_code": "VALIDATION_ERROR", "detail_zh": str(exc),
        }) from exc
    except RuntimeError as exc:
        raise _duckdb_write_busy(exc) from exc


@router.get("/data-mirror/tasks")
def list_mirror_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """最近镜像任务（新→旧，含分块进度）。"""
    return {"items": MT.list_mirror_tasks(db, limit=limit)}


@router.get("/data-mirror/tasks/{task_id}")
def mirror_task_progress(task_id: str,
                         db: Session = Depends(get_db)) -> dict[str, Any]:
    """任务进度：已完成块 / 总块数 / 行数 / 报告。"""
    progress = MT.get_mirror_task_progress(db, task_id)
    if not progress.get("found"):
        raise _not_found(task_id)
    return progress


@router.post("/data-mirror/tasks/{task_id}/cancel")
def cancel_mirror_task(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """取消镜像任务（复用既有 cancel 语义；分块边界自止，已完成块保留）。"""
    progress = MT.get_mirror_task_progress(db, task_id)
    if not progress.get("found"):
        raise _not_found(task_id)
    if progress["status"] in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail={
            "error_code": "BUSINESS_BLOCKED",
            "title_zh": "任务已结束",
            "detail_zh": f"镜像任务 {task_id} 已是终态（{progress['status']}）。",
        })
    read = cancel_async_task(task_id)
    return {"task_id": task_id, "status": read.status,
            "completed_chunks": progress.get("completed_chunks"),
            "total_chunks": progress.get("total_chunks")}
