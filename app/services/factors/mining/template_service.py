"""实验模板管理（B4：25 系统模板 seed + CRUD/复制/启停）。

表：`factor_mining_templates`（主题）+ `factor_mining_template_versions`（版本）。
25 经典模板源：`initial_population.CLASSIC_TEMPLATES`（与 GA 初始种群同源，
保证「模板库」与「实际展开」一致）。

- `seed_system_templates`：幂等，`scope=system` 按 name 去重；
- 复制：`scope=personal` 的一份新拷贝（name 后缀「(副本)」）；
- 启停：`enabled` 开关（初始种群展开时模板须可用）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.hash_utils import content_hash
from app.models.factor_mining import FactorMiningTemplate, FactorMiningTemplateVersion


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rule_config(tpl: Any) -> str:
    return json.dumps(
        {
            "name": tpl.name, "category": tpl.category, "formula": tpl.formula,
            "params": {k: list(v) for k, v in (tpl.params or {}).items()},
            "required_fields": list(tpl.required_fields or ()),
            "economy_logic_zh": tpl.economy_logic_zh,
            "priority": int(getattr(tpl, "priority", 5) or 5),
            "enabled": bool(getattr(tpl, "enabled", True)),
        },
        ensure_ascii=False, sort_keys=True)


def _to_view(row: FactorMiningTemplate, version: FactorMiningTemplateVersion | None
            ) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    if version is not None and version.rule_config_json:
        try:
            rule = json.loads(version.rule_config_json)
        except ValueError:
            rule = {}
    return {
        "template_id": row.id,
        "name": row.name,
        "description": row.description,
        "scope": row.scope,
        "owner": row.owner,
        "enabled": int(row.enabled or 0),
        "version": version.version if version else None,
        "rule_config": rule,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _latest_version(db: Session, template_id: str) -> FactorMiningTemplateVersion | None:
    return db.execute(
        select(FactorMiningTemplateVersion)
        .where(FactorMiningTemplateVersion.template_id == template_id)
        .order_by(FactorMiningTemplateVersion.created_at.desc())
    ).scalars().first()


def seed_system_templates(db: Session) -> dict[str, Any]:
    """25 经典模板（幂等，`scope=system` 按 name 去重）→ factor_mining_templates。"""
    from app.services.factors.mining import initial_population as IP

    existing = {
        r.name for r in db.execute(
            select(FactorMiningTemplate).where(
                FactorMiningTemplate.scope == "system")
        ).scalars().all()
    }
    created = 0
    for tpl in IP.CLASSIC_TEMPLATES:
        if tpl.name in existing:
            continue
        tpl_id = uuid.uuid4().hex
        rule = _rule_config(tpl)
        row = FactorMiningTemplate(
            id=tpl_id, name=tpl.name,
            description=str(getattr(tpl, "economy_logic_zh", "")),
            scope="system", owner="system", enabled=int(tpl.enabled),
            created_at=_now(), updated_at=_now(),
        )
        db.add(row)
        db.add(FactorMiningTemplateVersion(
            template_id=tpl_id, version="v1",
            change_note="系统种子（25 经典模板）",
            rule_config_json=rule,
            rule_hash=content_hash("mtpl", f"{tpl.name}:{tpl.formula}"),
            created_by="system", created_at=_now(),
        ))
        existing.add(tpl.name)
        created += 1
    db.commit()
    return {"created": created, "total_system": len(existing)}


def list_templates(
    db: Session, *, scope: str | None = None, enabled: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    q = select(FactorMiningTemplate)
    cq = select(func.count()).select_from(FactorMiningTemplate)
    if scope:
        q = q.where(FactorMiningTemplate.scope == str(scope))
        cq = cq.where(FactorMiningTemplate.scope == str(scope))
    if enabled is not None:
        v = int(bool(enabled))
        q = q.where(FactorMiningTemplate.enabled == v)
        cq = cq.where(FactorMiningTemplate.enabled == v)
    total = int(db.execute(cq).scalar_one() or 0)
    rows = db.execute(
        q.order_by(FactorMiningTemplate.scope.desc(),
                   FactorMiningTemplate.name.asc())
        .limit(max(1, int(limit)))
    ).scalars().all()
    return {"items": [_to_view(r, _latest_version(db, r.id)) for r in rows],
            "total": total}


def get_template(db: Session, template_id: str) -> dict[str, Any] | None:
    row = db.get(FactorMiningTemplate, str(template_id))
    if row is None:
        return None
    return _to_view(row, _latest_version(db, row.id))


def create_personal_template(
    db: Session, *, name: str, description: str | None,
    rule_config: Mapping[str, Any], owner: str,
) -> dict[str, Any]:
    if not name or not str(name).strip():
        raise ValueError("template_name_required")
    if not isinstance(rule_config, Mapping) or \
            not rule_config.get("formula"):
        raise ValueError("template_rule_required: 缺少 formula 配置。")
    tpl_id = uuid.uuid4().hex
    rule = json.dumps(dict(rule_config), ensure_ascii=False, sort_keys=True)
    row = FactorMiningTemplate(
        id=tpl_id, name=str(name).strip(),
        description=description or "", scope="personal",
        owner=str(owner or "local_user"), enabled=1,
        created_at=_now(), updated_at=_now(),
    )
    db.add(row)
    db.add(FactorMiningTemplateVersion(
        template_id=tpl_id, version="v1", change_note="个人模板",
        rule_config_json=rule,
        rule_hash=content_hash("mtpl", f"{name}:{rule_config.get('formula')}"),
        created_by=owner, created_at=_now(),
    ))
    db.commit()
    view = get_template(db, tpl_id)
    return view or {"template_id": tpl_id}


def copy_template(
    db: Session, *, template_id: str, owner: str,
) -> dict[str, Any]:
    """复制为个人模板（启停/版本独立，不影响被复制源）。"""
    src = db.get(FactorMiningTemplate, str(template_id))
    if src is None:
        raise ValueError(f"template_not_found:{template_id}")
    src_version = _latest_version(db, src.id)
    tpl_id = uuid.uuid4().hex
    row = FactorMiningTemplate(
        id=tpl_id, name=f"{src.name}（副本）",
        description=src.description, scope="personal",
        owner=str(owner or "local_user"), enabled=1,
        created_at=_now(), updated_at=_now(),
    )
    db.add(row)
    db.add(FactorMiningTemplateVersion(
        template_id=tpl_id, version="v1", change_note="复制而来",
        rule_config_json=src_version.rule_config_json if src_version else "{}",
        rule_hash=content_hash("mtpl", f"{tpl_id}:{src.name}"),
        created_by=owner, created_at=_now(),
    ))
    db.commit()
    return get_template(db, tpl_id) or {"template_id": tpl_id}


def set_template_enabled(
    db: Session, *, template_id: str, enabled: int,
) -> dict[str, Any]:
    row = db.get(FactorMiningTemplate, str(template_id))
    if row is None:
        raise ValueError(f"template_not_found:{template_id}")
    row.enabled = 1 if int(bool(enabled)) else 0
    db.commit()
    return get_template(db, row.id) or {"template_id": row.id}


__all__ = [
    "seed_system_templates", "list_templates", "get_template",
    "create_personal_template", "copy_template", "set_template_enabled",
]