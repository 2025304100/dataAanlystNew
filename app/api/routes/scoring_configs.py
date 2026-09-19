from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.scoring_config import ScoringConfig
from app.schemas.scoring_config import (
    ScoringConfigCreate,
    ScoringConfigDuplicate,
    ScoringConfigParsed,
    ScoringConfigRead,
    ScoringConfigUpdate,
    ScoringConfigVersionRead,
)


router = APIRouter()


def _to_parsed(cfg: ScoringConfig) -> ScoringConfigParsed:
    try:
        parsed = json.loads(cfg.config_json) if cfg.config_json else None
    except (json.JSONDecodeError, TypeError):
        parsed = None
    return ScoringConfigParsed(
        id=cfg.id,
        asset_type=cfg.asset_type,
        preset_key=cfg.preset_key,
        name=cfg.name,
        description=cfg.description,
        version=cfg.version,
        config_json=cfg.config_json,
        preset_source=cfg.preset_source,
        is_system=cfg.is_system,
        is_active=cfg.is_active,
        is_latest=cfg.is_latest,
        base_preset_key=cfg.base_preset_key,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
        config=parsed,
    )


def _normalize_preset_key(value: str) -> str:
    """preset_key 只允许字母/数字/下划线。"""
    import re
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", value):
        raise HTTPException(status_code=400, detail="preset_key must start with a letter and contain only letters, digits and underscores.")
    return value


@router.get("/settings/scoring-configs", response_model=list[ScoringConfigParsed])
def list_scoring_configs(asset_type: Literal["stock", "etf"], db: Session = Depends(get_db)):
    """列出指定资产类型下的所有最新预设（系统预设 + 用户预设）。"""
    rows = db.execute(
        select(ScoringConfig)
        .where(ScoringConfig.asset_type == asset_type, ScoringConfig.is_latest == 1)
        .order_by(ScoringConfig.is_system.desc(), ScoringConfig.preset_key.asc())
    ).scalars().all()
    return [_to_parsed(r) for r in rows]


@router.get("/settings/scoring-configs/active", response_model=ScoringConfigParsed | None)
def get_active_scoring_config(asset_type: Literal["stock", "etf"], db: Session = Depends(get_db)):
    """获取指定资产类型当前激活的预设。"""
    row = db.execute(
        select(ScoringConfig).where(
            ScoringConfig.asset_type == asset_type,
            ScoringConfig.is_active == 1,
            ScoringConfig.is_latest == 1,
        ).limit(1)
    ).scalars().first()
    if row is None:
        return None
    return _to_parsed(row)


@router.post("/settings/scoring-configs", response_model=ScoringConfigParsed)
def create_scoring_config(payload: ScoringConfigCreate, db: Session = Depends(get_db)):
    """新建用户预设。"""
    _normalize_preset_key(payload.preset_key)
    # 同 asset_type + preset_key 不允许重复（即使是历史版本）
    existing = db.execute(
        select(ScoringConfig).where(
            ScoringConfig.asset_type == payload.asset_type,
            ScoringConfig.preset_key == payload.preset_key,
        ).limit(1)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=400, detail="preset_key already exists under this asset_type")
    cfg = ScoringConfig(
        asset_type=payload.asset_type,
        preset_key=payload.preset_key,
        name=payload.name,
        description=payload.description,
        version=1,
        config_json=json.dumps(payload.config, ensure_ascii=False),
        preset_source="user",
        is_system=0,
        is_active=0,
        is_latest=1,
        base_preset_key=payload.base_preset_key,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return _to_parsed(cfg)


@router.put("/settings/scoring-configs/{config_id}", response_model=ScoringConfigParsed)
def update_scoring_config(config_id: int, payload: ScoringConfigUpdate, db: Session = Depends(get_db)):
    """保存用户预设新版本；系统预设不允许直接覆盖。"""
    cfg = db.get(ScoringConfig, config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="scoring config not found")
    if cfg.is_system == 1:
        raise HTTPException(status_code=400, detail="system preset cannot be overwritten; please duplicate it as a user preset first")

    # 新版本号 = 同 asset_type + preset_key 下 max(version) + 1
    max_version_row = db.execute(
        select(ScoringConfig.version)
        .where(ScoringConfig.asset_type == cfg.asset_type, ScoringConfig.preset_key == cfg.preset_key)
        .order_by(ScoringConfig.version.desc())
        .limit(1)
    ).scalars().first()
    new_version = (max_version_row or 0) + 1

    # 旧版本 is_latest 置 0
    db.execute(
        update(ScoringConfig)
        .where(
            ScoringConfig.asset_type == cfg.asset_type,
            ScoringConfig.preset_key == cfg.preset_key,
            ScoringConfig.is_latest == 1,
        )
        .values(is_latest=0)
    )

    new_cfg = ScoringConfig(
        asset_type=cfg.asset_type,
        preset_key=cfg.preset_key,
        name=payload.name or cfg.name,
        description=payload.description if payload.description is not None else cfg.description,
        version=new_version,
        config_json=json.dumps(payload.config, ensure_ascii=False) if payload.config is not None else cfg.config_json,
        preset_source="user",
        is_system=0,
        is_active=cfg.is_active,  # 若原本激活，新版本继续激活
        is_latest=1,
        base_preset_key=cfg.base_preset_key,
    )
    # 若新版本激活，则把同 asset_type + preset_key 旧版本的 is_active 全部置 0
    if new_cfg.is_active == 1:
        db.execute(
            update(ScoringConfig)
            .where(
                ScoringConfig.asset_type == cfg.asset_type,
                ScoringConfig.preset_key == cfg.preset_key,
                ScoringConfig.id != new_cfg.id,
            )
            .values(is_active=0)
        )
    db.add(new_cfg)
    db.commit()
    # 风控加固：失效激活预设缓存（新版本若被激活需重新读取）
    from app.services.scoring_config_engine import invalidate_active_config_cache
    invalidate_active_config_cache()
    db.refresh(new_cfg)
    return _to_parsed(new_cfg)


@router.post("/settings/scoring-configs/{config_id}/activate", response_model=ScoringConfigParsed)
def activate_scoring_config(config_id: int, db: Session = Depends(get_db)):
    """激活该预设：必须是最新版本；同 asset_type 其他预设 is_active 置 0。"""
    cfg = db.get(ScoringConfig, config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="scoring config not found")
    if cfg.is_latest != 1:
        raise HTTPException(status_code=400, detail="only the latest version of a preset can be activated")
    # 停用同 asset_type 其他预设
    db.execute(
        update(ScoringConfig)
        .where(ScoringConfig.asset_type == cfg.asset_type, ScoringConfig.is_active == 1)
        .values(is_active=0)
    )
    cfg.is_active = 1
    db.commit()
    # 风控加固：失效激活预设缓存，确保新激活立即生效
    from app.services.scoring_config_engine import invalidate_active_config_cache
    invalidate_active_config_cache()
    db.refresh(cfg)
    return _to_parsed(cfg)


@router.post("/settings/scoring-configs/{config_id}/duplicate", response_model=ScoringConfigParsed)
def duplicate_scoring_config(config_id: int, payload: ScoringConfigDuplicate, db: Session = Depends(get_db)):
    """复制系统预设或用户预设为新的用户预设（version=1）。"""
    src = db.get(ScoringConfig, config_id)
    if src is None:
        raise HTTPException(status_code=404, detail="scoring config not found")
    _normalize_preset_key(payload.new_preset_key)
    # 新 key 不可与同 asset_type 已有 key 重复
    existing = db.execute(
        select(ScoringConfig).where(
            ScoringConfig.asset_type == src.asset_type,
            ScoringConfig.preset_key == payload.new_preset_key,
        ).limit(1)
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=400, detail="new_preset_key already exists under this asset_type")
    try:
        new_config_json = json.loads(src.config_json) if src.config_json else {}
    except (json.JSONDecodeError, TypeError):
        new_config_json = {}
    if isinstance(new_config_json, dict):
        new_config_json["preset_key"] = payload.new_preset_key
        new_config_json["name"] = payload.new_name
        new_config_json["asset_type"] = src.asset_type
        new_config_json["preset_source"] = "user"
        new_config_json["description"] = payload.new_description if payload.new_description is not None else src.description
    new_cfg = ScoringConfig(
        asset_type=src.asset_type,
        preset_key=payload.new_preset_key,
        name=payload.new_name,
        description=payload.new_description if payload.new_description is not None else src.description,
        version=1,
        config_json=json.dumps(new_config_json, ensure_ascii=False) if new_config_json else src.config_json,
        preset_source="user",
        is_system=0,
        is_active=0,
        is_latest=1,
        base_preset_key=src.preset_key,
    )
    db.add(new_cfg)
    db.commit()
    # 风控加固：失效激活预设缓存（新版本若被激活需重新读取）
    from app.services.scoring_config_engine import invalidate_active_config_cache
    invalidate_active_config_cache()
    db.refresh(new_cfg)
    return _to_parsed(new_cfg)


@router.get("/settings/scoring-configs/{config_id}/versions", response_model=list[ScoringConfigVersionRead])
def list_scoring_config_versions(config_id: int, db: Session = Depends(get_db)):
    """查看该预设历史版本。"""
    cfg = db.get(ScoringConfig, config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="scoring config not found")
    rows = db.execute(
        select(ScoringConfig)
        .where(ScoringConfig.asset_type == cfg.asset_type, ScoringConfig.preset_key == cfg.preset_key)
        .order_by(ScoringConfig.version.desc())
    ).scalars().all()
    return rows


@router.delete("/settings/scoring-configs/{config_id}")
def delete_scoring_config(config_id: int, db: Session = Depends(get_db)):
    """删除用户预设；系统预设返回 400；正在激活的预设需先切换到其他预设。"""
    cfg = db.get(ScoringConfig, config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="scoring config not found")
    if cfg.is_system == 1:
        raise HTTPException(status_code=400, detail="system preset cannot be deleted")
    if cfg.is_active == 1:
        raise HTTPException(status_code=400, detail="cannot delete the active preset; please switch to another preset first")
    # 删除该 preset_key 的所有版本
    rows = db.execute(
        select(ScoringConfig).where(
            ScoringConfig.asset_type == cfg.asset_type,
            ScoringConfig.preset_key == cfg.preset_key,
        )
    ).scalars().all()
    for r in rows:
        db.delete(r)
    db.commit()
    return {"ok": True, "deleted": len(rows)}

