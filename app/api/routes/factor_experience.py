"""F1 历史经验库 HTTP 路由（设计文档 §6.14 / 开发文档 §3.12 / §4，T26）。

⚠️ 本项目路由范式：`router = APIRouter()` **无 prefix**，装饰器里写**全路径**；
   `/api/v1` 由 `app/api/router.py` 统一挂载。

3 个接口（M1 最小内核）：
  POST /factor-experience          存回（因子入库后自动调用）
  GET  /factor-experience/sample   抽取（初始种群）
  GET  /factor-experience/related  相关经验（本期实现，F2 二期才调用）

红线 C5：挖掘模块只经本 HTTP 面读写 F1，不得 import F1 service/model。
分层纪律（R5）：本文件只做 DTO 转换与入参校验，业务全在 service 层。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.factor_experience import (
    RelatedExperienceItem,
    RelatedExperienceResponse,
    SampleExperienceItem,
    SampleExperienceResponse,
    StoreExperienceRequest,
    StoreExperienceResponse,
)
from app.services.factors.experience import service as experience_service

router = APIRouter()


@router.post("/factor-experience", response_model=StoreExperienceResponse)
def store_experience(
    req: StoreExperienceRequest,
    db: Session = Depends(get_db),
) -> StoreExperienceResponse:
    """存回一条经验（参数泛化 → 三层指纹去重 → 分类 → 打标签 → 写 4 表）。"""
    try:
        exp_id, status = experience_service.store_experience(
            db, payload=req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return StoreExperienceResponse(experience_id=exp_id, status=status)


@router.get("/factor-experience/sample", response_model=SampleExperienceResponse)
def sample_experiences(
    stock_pool: str | None = Query(default=None, description="标的池场景匹配"),
    field_scope: str | None = Query(
        default=None, description="允许字段集合（逗号分隔；硬过滤）"),
    market_env: str | None = Query(default=None, description="市场环境场景匹配"),
    count: int = Query(default=5, ge=1, le=50, description="抽取数量"),
    db: Session = Depends(get_db),
) -> SampleExperienceResponse:
    """抽取经验（字段硬过滤 → 场景匹配 → 加权随机 → 参数实例化）。

    `is_negative_sample=1` 与 archived 永不参与抽取。
    """
    scope = (
        [f.strip() for f in field_scope.split(",") if f.strip()]
        if field_scope else None
    )
    try:
        items = experience_service.sample_experiences(
            db,
            stock_pool=stock_pool,
            field_scope=scope,
            market_env=market_env,
            count=count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SampleExperienceResponse(items=[SampleExperienceItem(**i) for i in items])


@router.get("/factor-experience/related", response_model=RelatedExperienceResponse)
def related_experiences(
    experience_id: str = Query(..., description="基准经验 id"),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> RelatedExperienceResponse:
    """相关经验查询（本期实现，F2 二期才调用）。"""
    items = experience_service.related_experiences(
        db, experience_id=experience_id, limit=limit)
    return RelatedExperienceResponse(
        items=[RelatedExperienceItem(**i) for i in items])


# ══════════════════════════════════════════════════════════
# B3：列表 / 详情 / 归档（前端经验库页 + 第一批沉淀可核验）
# ══════════════════════════════════════════════════════════


@router.get("/factor-experience")
def list_experiences(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    category: str | None = Query(default=None, description="6 类之一"),
    source: str | None = Query(default=None),
    is_negative_sample: int | None = Query(default=None, description="1=负样本"),
    db: Session = Depends(get_db),
) -> dict:
    """分页经验列表（负样本/归档也返回，供管理侧核验）。"""
    return experience_service.list_experiences(
        db, page=page, page_size=page_size,
        category=category, source=source,
        is_negative_sample=is_negative_sample)


@router.get("/factor-experience/{experience_id}")
def get_experience(experience_id: str, db: Session = Depends(get_db)):
    """经验详情（含指标历史）。"""
    item = experience_service.get_experience(db, experience_id)
    if item is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "EXPERIENCE_NOT_FOUND",
            "title_zh": "经验不存在",
            "detail_zh": f"未找到经验 {experience_id}。",
        })
    return item


@router.post("/factor-experience/{experience_id}/archive")
def archive_experience(experience_id: str, db: Session = Depends(get_db)):
    """归档经验（抽取与抽样硬化排除；负样本规避不受影响）。"""
    try:
        return experience_service.archive_experience(db, experience_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


__all__ = ["router"]
