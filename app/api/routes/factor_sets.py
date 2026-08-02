"""WP7-01: FactorSet API 路由。

端点：
- POST /factor-sets：创建 FactorSet
- GET /factor-sets：列出 FactorSet
- GET /factor-sets/{factor_set_id}：获取 FactorSet 详情（含成员）
- POST /factor-sets/{factor_set_id}/members：添加成员
- DELETE /factor-sets/{factor_set_id}/members/{factor_id}：移除成员
- POST /factor-sets/{factor_set_id}/freeze：冻结 FactorSet
- POST /factor-sets/{factor_set_id}/deprecate：废弃 FactorSet
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.db.session import SessionLocal
from app.schemas.factor_library import (
    FactorSetCreate,
    FactorSetFreezeRequest,
    FactorSetMemberCreate,
)
from app.services.factors.factor_set_service import (
    FactorSetError,
    add_member,
    create_factor_set,
    deprecate_factor_set,
    freeze_factor_set,
    get_factor_set,
    list_factor_sets,
    remove_member,
    to_read_dict,
)


router = APIRouter()


def _handle_error(exc: FactorSetError) -> HTTPException:
    """将服务层错误转换为 HTTP 异常。"""
    status_map = {
        "set_not_found": 404,
        "version_not_found": 404,
        "factor_not_found": 404,
        "member_not_found": 404,
    }
    status = status_map.get(exc.code, 422)
    return HTTPException(status_code=status, detail=f"{exc.code}: {exc.message}")


@router.post("/factor-sets")
def post_create_factor_set(payload: FactorSetCreate):
    """创建 FactorSet（draft 状态）。"""
    with SessionLocal() as db:
        try:
            result = create_factor_set(db, payload=payload)
            db.commit()
            return to_read_dict(result.factor_set)
        except FactorSetError as exc:
            raise _handle_error(exc) from exc


@router.get("/factor-sets")
def get_factor_sets(
    status: str | None = Query(None, description="按状态筛选: draft/frozen/deprecated"),
    limit: int = Query(50, ge=1, le=200),
):
    """列出 FactorSet。"""
    with SessionLocal() as db:
        sets = list_factor_sets(db, status=status, limit=limit)
        return [to_read_dict(s) for s in sets]


@router.get("/factor-sets/{factor_set_id}")
def get_factor_set_detail(factor_set_id: str):
    """获取 FactorSet 详情（含成员列表）。"""
    with SessionLocal() as db:
        factor_set = get_factor_set(db, factor_set_id)
        if factor_set is None:
            raise HTTPException(status_code=404, detail=f"set_not_found: {factor_set_id}")
        return to_read_dict(factor_set)


@router.post("/factor-sets/{factor_set_id}/members")
def post_add_member(factor_set_id: str, payload: FactorSetMemberCreate):
    """向 FactorSet 添加成员（仅 draft 状态）。"""
    with SessionLocal() as db:
        try:
            member = add_member(db, factor_set_id=factor_set_id, payload=payload)
            db.commit()
            return {
                "id": member.id,
                "factor_set_id": member.factor_set_id,
                "factor_id": member.factor_id,
                "factor_version_id": member.factor_version_id,
                "factor_code": member.factor_code,
                "factor_version": member.factor_version,
                "role": member.role,
                "weight_constraint": member.weight_constraint,
                "display_order": member.display_order,
                "missing_policy": member.missing_policy,
            }
        except FactorSetError as exc:
            raise _handle_error(exc) from exc


@router.delete("/factor-sets/{factor_set_id}/members/{factor_id}")
def delete_member(factor_set_id: str, factor_id: int):
    """从 FactorSet 移除成员（仅 draft 状态）。"""
    with SessionLocal() as db:
        try:
            remove_member(db, factor_set_id=factor_set_id, factor_id=factor_id)
            db.commit()
            return {"success": True, "factor_set_id": factor_set_id, "factor_id": factor_id}
        except FactorSetError as exc:
            raise _handle_error(exc) from exc


@router.post("/factor-sets/{factor_set_id}/freeze")
def post_freeze(factor_set_id: str, payload: FactorSetFreezeRequest):
    """冻结 FactorSet（draft → frozen，不可再修改成员）。"""
    with SessionLocal() as db:
        try:
            factor_set = freeze_factor_set(
                db,
                factor_set_id=factor_set_id,
                actor=payload.actor,
                reason=payload.reason,
            )
            db.commit()
            return to_read_dict(factor_set)
        except FactorSetError as exc:
            raise _handle_error(exc) from exc


@router.post("/factor-sets/{factor_set_id}/deprecate")
def post_deprecate(factor_set_id: str, payload: FactorSetFreezeRequest):
    """废弃 FactorSet（draft/frozen → deprecated，终态）。"""
    with SessionLocal() as db:
        try:
            factor_set = deprecate_factor_set(
                db,
                factor_set_id=factor_set_id,
                reason=payload.reason,
            )
            db.commit()
            return to_read_dict(factor_set)
        except FactorSetError as exc:
            raise _handle_error(exc) from exc
