from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.alert import AlertRule
from app.schemas.alert import AlertRuleCreate, AlertRuleRead, AlertRuleUpdate
from app.services.alerts import (
    acknowledge_alert,
    acknowledge_all_alerts,
    evaluate_all_rules,
    get_active_alerts,
)

router = APIRouter()


# ── 告警规则 CRUD ─────────────────────────────────────────


@router.get("/alerts/rules", response_model=list[AlertRuleRead])
def list_alert_rules(db: Session = Depends(get_db)):
    stmt = select(AlertRule).order_by(AlertRule.created_at.desc())
    return db.execute(stmt).scalars().all()


@router.post("/alerts/rules", response_model=AlertRuleRead)
def create_alert_rule(payload: AlertRuleCreate, db: Session = Depends(get_db)):
    rule = AlertRule(
        name=payload.name,
        alert_type=payload.alert_type,
        enabled=int(payload.enabled),
        severity=payload.severity,
        config_json=json.dumps(payload.config, ensure_ascii=False) if payload.config else None,
        cooldown_minutes=payload.cooldown_minutes,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/alerts/rules/{rule_id}", response_model=AlertRuleRead)
def update_alert_rule(rule_id: int, payload: AlertRuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "config":
            setattr(rule, "config_json", json.dumps(value, ensure_ascii=False) if value else None)
        elif key == "enabled":
            setattr(rule, key, int(value) if value is not None else 1)
        else:
            setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404, "Alert rule not found")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


# ── 告警事件 ──────────────────────────────────────────────


@router.get("/alerts/events")
def list_alert_events(
    limit: int = Query(default=50, ge=1, le=200),
    include_acknowledged: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    from app.models.alert import AlertEvent
    from app.services.alerts import (
        _alert_resolution_fields,
        _safe_load_data_json,
        reconcile_task_alert_recoveries,
    )
    from sqlalchemy import select as sa_select

    reconcile_task_alert_recoveries(db)
    stmt = sa_select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(limit)
    if not include_acknowledged:
        stmt = stmt.where(AlertEvent.acknowledged == 0)
    rows = db.execute(stmt).scalars().all()

    result = []
    for ev in rows:
        # 风控加固：复用 service 层统一解析函数，避免静默吞没 JSON 异常
        data = _safe_load_data_json(ev)
        state = _alert_resolution_fields(data)
        result.append({
            "id": ev.id,
            "rule_id": ev.rule_id,
            "alert_type": ev.alert_type,
            "severity": ev.severity,
            "title": ev.title,
            "message": ev.message,
            "symbol_id": ev.symbol_id,
            "data": data,
            "acknowledged": ev.acknowledged,
            **state,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        })
    return {
        "events": result,
        "unacknowledged_count": sum(
            1 for event in result
            if not event["acknowledged"] and not event["resolved"]
        ),
    }


@router.post("/alerts/evaluate")
def trigger_evaluation():
    """手动触发一次告警评估。"""
    new_events = evaluate_all_rules()
    return {"evaluated": True, "new_events": len(new_events), "events": new_events}


@router.get("/alerts/active")
def list_active_alerts(limit: int = Query(default=50, ge=1, le=200)):
    """获取未确认的告警。"""
    events = get_active_alerts(limit)
    return {
        "events": events,
        "count": sum(1 for event in events if not event.get("resolved", False)),
    }


@router.post("/alerts/acknowledge/{event_id}")
def ack_alert(event_id: int):
    """确认一条告警。"""
    ok = acknowledge_alert(event_id)
    if not ok:
        raise HTTPException(404, "Alert event not found")
    return {"acknowledged": event_id}


@router.post("/alerts/acknowledge-all")
def ack_all_alerts():
    """确认所有告警。"""
    count = acknowledge_all_alerts()
    return {"acknowledged_count": count}
