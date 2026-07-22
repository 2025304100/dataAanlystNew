"""AI Profile 管理 API（WP-AI.2 多 Profile 主备降级）。

端点：
- GET    /api/v1/ai/profiles          - 列出 Profile
- POST   /api/v1/ai/profiles          - 创建 Profile
- PUT    /api/v1/ai/profiles/{id}     - 更新 Profile
- DELETE /api/v1/ai/profiles/{id}     - 删除 Profile
- POST   /api/v1/ai/profiles/{id}/test - 测试连接
- GET    /api/v1/ai/profiles/{id}/models - 发现模型
- GET    /api/v1/ai/profiles/{id}/usage - 获取用量
- GET    /api/v1/ai/health            - 健康状态总览

约束：
- 永不返回明文 Secret：响应中只有 secret_key_ref（key 名称）
- AI 失败不阻塞业务流程：测试/发现模型失败时返回 {success: False, error}
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.ai_profile import (
    AIProfileCreate,
    AIProfileHealth,
    AIProfileRead,
    AIProfileTestResult,
    AIProfileUpdate,
    AIProfileUsage,
)
from app.services import ai_profile_service
from app.services.ai_failover import get_failover_manager

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_read(profile) -> AIProfileRead:
    """将 ORM 对象转为响应 schema（处理 datetime → str）。"""
    return AIProfileRead(
        id=profile.id,
        name=profile.name,
        provider=profile.provider,
        base_url=profile.base_url,
        model=profile.model,
        auth_type=profile.auth_type,
        secret_key_ref=profile.secret_key_ref,  # 只返回 key 名，不是值
        timeout_seconds=profile.timeout_seconds,
        max_tokens=profile.max_tokens,
        max_context_tokens=profile.max_context_tokens,
        daily_request_limit=profile.daily_request_limit,
        max_concurrent=profile.max_concurrent,
        purpose=profile.purpose,
        priority=profile.priority,
        is_enabled=profile.is_enabled,
        is_fallback=profile.is_fallback,
        health_status=profile.health_status,
        last_health_check=profile.last_health_check.isoformat()
        if profile.last_health_check
        else None,
        daily_request_count=profile.daily_request_count,
        daily_request_reset_at=profile.daily_request_reset_at.isoformat()
        if profile.daily_request_reset_at
        else None,
        created_at=profile.created_at.isoformat() if profile.created_at else None,
        updated_at=profile.updated_at.isoformat() if profile.updated_at else None,
    )


@router.get("/ai/profiles", response_model=list[AIProfileRead])
def list_profiles(db: Session = Depends(get_db)):
    """列出所有 Profile。"""
    profiles = ai_profile_service.list_profiles(db)
    return [_to_read(p) for p in profiles]


@router.post("/ai/profiles", response_model=AIProfileRead)
def create_profile(payload: AIProfileCreate, db: Session = Depends(get_db)):
    """创建 Profile。

    secret_value 仅写入 SecretStore，响应中不返回。
    """
    try:
        # 检查 name 唯一
        existing = ai_profile_service.get_profile_by_name(db, payload.name)
        if existing is not None:
            raise HTTPException(status_code=400, detail="Profile 名称已存在")
        profile = ai_profile_service.create_profile(
            db,
            name=payload.name,
            provider=payload.provider,
            model=payload.model,
            base_url=payload.base_url,
            auth_type=payload.auth_type,
            secret_value=payload.secret_value,
            timeout_seconds=payload.timeout_seconds,
            max_tokens=payload.max_tokens,
            max_context_tokens=payload.max_context_tokens,
            daily_request_limit=payload.daily_request_limit,
            max_concurrent=payload.max_concurrent,
            purpose=payload.purpose,
            priority=payload.priority,
            is_enabled=payload.is_enabled,
            is_fallback=payload.is_fallback,
        )
        db.commit()
        db.refresh(profile)
        return _to_read(profile)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("create_profile failed")
        raise HTTPException(status_code=500, detail=f"创建失败：{exc}") from exc


@router.put("/ai/profiles/{profile_id}", response_model=AIProfileRead)
def update_profile(
    profile_id: int, payload: AIProfileUpdate, db: Session = Depends(get_db)
):
    """更新 Profile。"""
    try:
        # 检查 name 唯一（若更新了 name）
        if payload.name is not None:
            existing = ai_profile_service.get_profile_by_name(db, payload.name)
            if existing is not None and existing.id != profile_id:
                raise HTTPException(status_code=400, detail="Profile 名称已存在")
        profile = ai_profile_service.update_profile(
            db,
            profile_id,
            name=payload.name,
            provider=payload.provider,
            base_url=payload.base_url,
            model=payload.model,
            auth_type=payload.auth_type,
            secret_value=payload.secret_value,
            timeout_seconds=payload.timeout_seconds,
            max_tokens=payload.max_tokens,
            max_context_tokens=payload.max_context_tokens,
            daily_request_limit=payload.daily_request_limit,
            max_concurrent=payload.max_concurrent,
            purpose=payload.purpose,
            priority=payload.priority,
            is_enabled=payload.is_enabled,
            is_fallback=payload.is_fallback,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile 不存在")
        db.commit()
        db.refresh(profile)
        return _to_read(profile)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("update_profile failed")
        raise HTTPException(status_code=500, detail=f"更新失败：{exc}") from exc


@router.delete("/ai/profiles/{profile_id}")
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    """删除 Profile（同时删除 SecretStore 中的 secret）。"""
    ok = ai_profile_service.delete_profile(db, profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Profile 不存在")
    db.commit()
    return {"status": "ok", "message": "已删除"}


@router.post("/ai/profiles/{profile_id}/test", response_model=AIProfileTestResult)
def test_connection(profile_id: int, db: Session = Depends(get_db)):
    """测试连接。AI 失败不阻塞：返回 {success: False, error}。"""
    result = ai_profile_service.test_connection(db, profile_id)
    db.commit()
    return AIProfileTestResult(**result)


@router.get("/ai/profiles/{profile_id}/models")
def discover_models(profile_id: int, db: Session = Depends(get_db)):
    """发现可用模型。AI 失败不阻塞：返回空列表。"""
    models = ai_profile_service.discover_models(db, profile_id)
    return {"models": models}


@router.get("/ai/profiles/{profile_id}/usage", response_model=AIProfileUsage)
def get_usage(profile_id: int, db: Session = Depends(get_db)):
    """获取当日用量。"""
    profile = ai_profile_service.get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile 不存在")
    usage = ai_profile_service.get_daily_usage(db, profile_id)
    return AIProfileUsage(**usage)


@router.get("/ai/health", response_model=list[AIProfileHealth])
def get_health(db: Session = Depends(get_db)):
    """健康状态总览。"""
    mgr = get_failover_manager()
    return mgr.get_health_status(db)


__all__ = ["router"]
