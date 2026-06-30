from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.discovery_plan import DiscoveryPlan
from app.schemas.discovery_plan import DiscoveryPlanCreate, DiscoveryPlanRead, DiscoveryPlanUpdate


router = APIRouter()


def _json_loads(value: str | None, default):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _format_plan(row: DiscoveryPlan) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "logic": row.logic or "AND",
        "pool_tab": row.pool_tab or "actionable",
        "filters": _json_loads(row.filters_json, []),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _ensure_unique_name(db: Session, name: str, exclude_id: int | None = None) -> None:
    stmt = select(DiscoveryPlan).where(DiscoveryPlan.name == name)
    if exclude_id is not None:
        stmt = stmt.where(DiscoveryPlan.id != exclude_id)
    existing = db.execute(stmt).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Discovery plan name already exists")


@router.get("/settings/discovery-plans", response_model=list[DiscoveryPlanRead])
def list_discovery_plans(db: Session = Depends(get_db)):
    stmt = select(DiscoveryPlan).order_by(desc(DiscoveryPlan.updated_at), desc(DiscoveryPlan.id))
    rows = db.execute(stmt).scalars().all()
    return [_format_plan(row) for row in rows]


@router.post("/settings/discovery-plans", response_model=DiscoveryPlanRead, status_code=201)
def create_discovery_plan(payload: DiscoveryPlanCreate, db: Session = Depends(get_db)):
    _ensure_unique_name(db, payload.name)
    row = DiscoveryPlan(
        name=payload.name,
        logic=payload.logic,
        pool_tab=payload.pool_tab,
        filters_json=json.dumps([item.model_dump() for item in payload.filters], ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _format_plan(row)


@router.put("/settings/discovery-plans/{plan_id}", response_model=DiscoveryPlanRead)
def update_discovery_plan(plan_id: int, payload: DiscoveryPlanUpdate, db: Session = Depends(get_db)):
    row = db.get(DiscoveryPlan, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Discovery plan not found")

    next_name = payload.name if payload.name is not None else row.name
    _ensure_unique_name(db, next_name, exclude_id=plan_id)

    if payload.name is not None:
        row.name = payload.name
    if payload.logic is not None:
        row.logic = payload.logic
    if payload.pool_tab is not None:
        row.pool_tab = payload.pool_tab
    if payload.filters is not None:
        row.filters_json = json.dumps([item.model_dump() for item in payload.filters], ensure_ascii=False)
    row.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(row)
    return _format_plan(row)


@router.delete("/settings/discovery-plans/{plan_id}")
def delete_discovery_plan(plan_id: int, db: Session = Depends(get_db)):
    row = db.get(DiscoveryPlan, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Discovery plan not found")
    db.delete(row)
    db.commit()
    return {"success": True}
