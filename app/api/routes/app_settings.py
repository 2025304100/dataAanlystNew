"""通用应用设置端点：`GET/PUT /settings/app`（用户级持久化，落 `app_settings` 表）。

错误信封遵循 PT-DEF-8 的结论：`detail` 传 **dict**（含 `error_code`），而不是裸字符串，
否则前端拿不到稳定错误码；对应错误码已在 `ERROR_CODE_LIBRARY` 登记
（未登记的码会回退成 UNKNOWN_ERROR「服务暂时不可用」，与"参数不对、重试无用"相反）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.app_settings import (
    AppSettingRead,
    AppSettingUpdateRequest,
    AppSettingUpdateResponse,
)
from app.services import app_settings as svc

router = APIRouter()


@router.get("/settings/app", response_model=list[AppSettingRead])
def list_app_settings(db: Session = Depends(get_db)):
    """列出所有已登记设置项的生效值与来源（供设置页渲染）。"""
    return svc.list_effective_settings(db)


@router.put("/settings/app/{key}", response_model=AppSettingUpdateResponse)
def update_app_setting(
    key: str,
    payload: AppSettingUpdateRequest,
    db: Session = Depends(get_db),
    x_user: str | None = Header(default=None, alias="X-User"),
):
    """写入一个设置值。只接受登记过的键，并按登记类型校验归一化。"""
    try:
        value = svc.coerce_setting_value(key, payload.value)
    except svc.SettingKeyUnknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "UNKNOWN_SETTING_KEY",
                "message": f"未登记的设置键：{key}",
                "known_keys": sorted(svc.APP_SETTING_REGISTRY),
            },
        ) from None
    except svc.SettingValueInvalid as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "SETTING_VALUE_INVALID",
                "message": str(exc),
            },
        ) from None

    row = svc.set_app_setting(
        db, key, value, actor=f"settings-page:{x_user or 'anonymous'}"
    )
    return AppSettingUpdateResponse(
        key=row.key, value=value, source="table", updated_by=row.updated_by
    )
