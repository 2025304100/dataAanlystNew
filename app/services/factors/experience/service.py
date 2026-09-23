"""F1 历史经验库服务（设计文档 §6.14 / 开发文档 §3.12，模块 M14·T26）。

公开 API（供路由与未来 mining 经 HTTP 网关调用；**mining 域不得 import
本包直连 F1 表 —— 红线 C5**）：

  - store_experience(db, *, payload) -> (experience_id, status)
      status ∈ {"created", "duplicate"}（duplicate = 三层指纹命中已存在经验）
  - sample_experiences(db, *, stock_pool, field_scope, market_env, count,
                       related_experiences=None, rng=None) -> list[dict]
      字段硬过滤 → 场景匹配打分 → 加权随机抽取（不放回）→ 参数实例化
      **is_negative_sample=1 与 archived 永不参与抽取**（负样本规避硬要求）
  - related_experiences(db, *, experience_id, limit=10) -> list[dict]

数值纪律（P0）：metrics.value / avg_icir 一律过 `to_db_float`
（NaN/±Inf → None，不可归 0；IC=0 是「无预测力」不是「没算出来」）。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db_numeric import to_db_float
from app.core.hash_utils import content_hash
from app.models.factor_experience import (
    FactorExperience,
    FactorExperienceFieldDep,
    FactorExperienceMetric,
    FactorExperienceTag,
)
from app.services.factors.experience import fingerprint as fp
from app.services.factors.experience import generalization as gen

#: category 6 类白名单（设计文档 §3.12）
CATEGORY_WHITELIST = (
    "trend", "reversal", "volatility", "valuation", "quality", "volume_price",
)
#: source 白名单
SOURCE_WHITELIST = ("ai_generated", "enumerated", "random", "manual")
#: metric_type 白名单（开发文档 §3.12）
METRIC_TYPE_WHITELIST = ("icir", "coverage", "turnover", "ic")

_STATUS_ARCHIVED = "archived"

#: 场景匹配打分权重（docstring 契约，测试按此断言）
SCORE_ENV_MATCH = 2.0
SCORE_POOL_MATCH = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 dict")
    ast = payload.get("formula_ast")
    if not isinstance(ast, dict) or "type" not in ast:
        raise ValueError("payload.formula_ast 必须是序列化 AST dict（含 type 键）")
    source = payload.get("source")
    if source not in SOURCE_WHITELIST:
        raise ValueError(
            f"payload.source 必须是 {SOURCE_WHITELIST} 之一，收到 {source!r}")
    category = payload.get("category")
    if category is not None and category not in CATEGORY_WHITELIST:
        raise ValueError(
            f"payload.category 必须是 {CATEGORY_WHITELIST} 之一，收到 {category!r}")


def _build_tags(
    formula_ast: dict[str, Any],
    complexity: dict[str, int],
    task_context: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """自动标签：op:<算子> / field:<字段> / complexity:<桶> / 场景上下文。"""
    ops: list[str] = []
    fields: list[str] = []
    fp._collect_ops_and_fields(formula_ast, ops, fields)  # noqa: SLF001 - 包内复用
    tags: list[dict[str, str]] = []
    for op_name in sorted(set(ops)):
        tags.append({"tag_key": "op", "tag_value": op_name, "source": "auto"})
    for field_code in sorted(set(fields)):
        tags.append({"tag_key": "field", "tag_value": field_code, "source": "auto"})
    n_ops = complexity["operators"]
    bucket = "low" if n_ops <= 2 else ("mid" if n_ops <= 5 else "high")
    tags.append({"tag_key": "complexity", "tag_value": bucket, "source": "auto"})
    for key in ("market_env", "stock_pool"):
        value = (task_context or {}).get(key)
        if value:
            tags.append(
                {"tag_key": key, "tag_value": str(value), "source": "auto"})
    return tags


def store_experience(db: Session, *, payload: dict[str, Any]) -> tuple[str, str]:
    """存回一条经验：泛化 → 指纹去重 → 分类 → 打标签 → 同事务写 4 表。

    Returns:
        (experience_id, status) —— status="created" 或 "duplicate"。
        duplicate 时返回已存在经验的 id（幂等，不重复落库）。
    """
    _validate_payload(payload)
    formula_ast: dict[str, Any] = payload["formula_ast"]

    template, placeholders = gen.generalize(formula_ast)
    fingerprint = fp.three_layer_fingerprint(formula_ast)

    existing = db.execute(
        select(FactorExperience).where(FactorExperience.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id, "duplicate"

    category = payload.get("category") or fp.infer_category(formula_ast)
    complexity = fp.complexity_profile(formula_ast)
    fields = gen.collect_fields(formula_ast)
    field_layers: dict[str, str] = payload.get("field_layers") or {}
    task_context = payload.get("task_context")

    exp_id = content_hash("fexp", fingerprint)

    # metrics：value 过 db_numeric（NaN/±Inf → None，不可归 0）
    metric_rows: list[FactorExperienceMetric] = []
    icir_values: list[float] = []
    for m in payload.get("metrics") or []:
        metric_type = m.get("metric_type")
        if metric_type not in METRIC_TYPE_WHITELIST:
            raise ValueError(
                f"metric_type 必须是 {METRIC_TYPE_WHITELIST} 之一，收到 {metric_type!r}")
        value = to_db_float(m.get("value"))
        metric_rows.append(FactorExperienceMetric(
            experience_id=exp_id,
            metric_type=metric_type,
            value=value,
            period=m.get("period"),
            is_oos=int(bool(m.get("is_oos", 0))),
            recorded_at=_now(),
        ))
        if metric_type == "icir" and value is not None:
            icir_values.append(value)
    avg_icir = (
        to_db_float(sum(icir_values) / len(icir_values)) if icir_values else None)

    exp = FactorExperience(
        id=exp_id,
        formula_template=template,
        formula_ast=json.dumps(formula_ast, ensure_ascii=False, sort_keys=True),
        category=category,
        complexity_json=json.dumps(complexity, ensure_ascii=False, sort_keys=True),
        source=payload["source"],
        fingerprint=fingerprint,
        param_placeholders_json=(
            json.dumps(placeholders, ensure_ascii=False) if placeholders else None),
        use_count=0,
        success_count=0,
        success_rate=0.0,
        avg_icir=avg_icir,
        status=payload.get("status") or "normal",
        is_negative_sample=int(bool(payload.get("is_negative_sample", 0))),
        origin_project_id=payload.get("origin_project_id"),
        schema_version=payload.get("schema_version") or "v1",
        created_at=_now(),
        updated_at=_now(),
    )

    tag_rows = [
        FactorExperienceTag(
            experience_id=exp_id, tag_key=t["tag_key"],
            tag_value=t["tag_value"], source=t["source"])
        for t in _build_tags(formula_ast, complexity, task_context)
    ]
    dep_rows = [
        FactorExperienceFieldDep(
            experience_id=exp_id,
            field_code=code,
            field_layer=field_layers.get(code, "A"),
            is_required=int(bool((payload.get("field_required") or {}).get(code, 1))),
        )
        for code in fields
    ]

    db.add_all([exp, *metric_rows, *tag_rows, *dep_rows])
    db.commit()
    return exp.id, "created"


def _load_candidates(db: Session) -> list[FactorExperience]:
    """抽取候选基线：排除 archived 与负样本（负样本规避硬要求）。"""
    return list(db.execute(
        select(FactorExperience)
        .where(FactorExperience.is_negative_sample == 0)
        .where(FactorExperience.status != _STATUS_ARCHIVED)
    ).scalars().all())


def _group_deps(
    db: Session, exp_ids: list[str]
) -> dict[str, list[str]]:
    """experience_id -> [field_code]（抽取的字段硬过滤输入）。"""
    if not exp_ids:
        return {}
    rows = db.execute(
        select(FactorExperienceFieldDep.experience_id,
               FactorExperienceFieldDep.field_code)
        .where(FactorExperienceFieldDep.experience_id.in_(exp_ids))
    ).all()
    grouped: dict[str, list[str]] = {}
    for exp_id, code in rows:
        grouped.setdefault(exp_id, []).append(code)
    return grouped


def _group_tags(db: Session, exp_ids: list[str]) -> dict[str, dict[str, str]]:
    """experience_id -> {tag_key: tag_value}（场景匹配输入）。"""
    if not exp_ids:
        return {}
    rows = db.execute(
        select(FactorExperienceTag.experience_id, FactorExperienceTag.tag_key,
               FactorExperienceTag.tag_value)
        .where(FactorExperienceTag.experience_id.in_(exp_ids))
    ).all()
    grouped: dict[str, dict[str, str]] = {}
    for exp_id, key, value in rows:
        grouped.setdefault(exp_id, {})[key] = value
    return grouped


def _match_score(
    exp: FactorExperience,
    tags: dict[str, str],
    *,
    stock_pool: str | None,
    market_env: str | None,
) -> float:
    """场景匹配打分（确定性契约，测试按此断言）：

    score = 2*market_env 命中 + 2*stock_pool 命中 + success_rate + max(0, avg_icir)
    """
    score = 0.0
    if market_env and tags.get("market_env") == market_env:
        score += SCORE_ENV_MATCH
    if stock_pool and tags.get("stock_pool") == stock_pool:
        score += SCORE_POOL_MATCH
    score += float(exp.success_rate or 0.0)
    if exp.avg_icir is not None:
        score += max(0.0, float(exp.avg_icir))
    return score


def _instantiate_value(rng: random.Random, ph: dict[str, Any]) -> Any:
    """占位符在 range 内取值：整型 → randint；浮点 → uniform。"""
    lo, hi = ph.get("range") or [ph.get("value"), ph.get("value")]
    if isinstance(ph.get("value"), int) and not isinstance(ph["value"], bool):
        return int(rng.randint(int(lo), int(hi)))
    return float(rng.uniform(float(lo), float(hi)))


def sample_experiences(
    db: Session,
    *,
    stock_pool: str | None = None,
    field_scope: list[str] | None = None,
    market_env: str | None = None,
    count: int = 5,
    related_experiences: list[str] | None = None,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """抽取初始种群经验：硬过滤 → 打分 → 加权不放回随机 → 实例化。

    Args:
        field_scope: 允许字段集合；经验依赖字段任一超界即**硬排除**。
        related_experiences: 排除集（已给定的经验 id 不重复抽取；M1 预留入参）。
        rng: 随机源注入（可测性）；缺省用模块级 random.Random()。
        count: 抽取数量，可用候选不足时全量返回。

    Returns:
        [{"experience_id", "formula", "template", "placeholders",
          "category", "source", "score"}]
    """
    rng = rng or random.Random()
    candidates = _load_candidates(db)
    if related_experiences:
        exclude = set(related_experiences)
        candidates = [c for c in candidates if c.id not in exclude]

    grouped_deps = _group_deps(db, [c.id for c in candidates])
    grouped_tags = _group_tags(db, [c.id for c in candidates])
    scope = set(field_scope) if field_scope else None

    scored: list[tuple[float, FactorExperience, list[dict[str, Any]]]] = []
    for exp in candidates:
        deps = grouped_deps.get(exp.id, [])
        if scope and any(code not in scope for code in deps):
            continue  # 字段硬过滤
        placeholders = json.loads(exp.param_placeholders_json or "[]")
        score = _match_score(
            exp, grouped_tags.get(exp.id, {}),
            stock_pool=stock_pool, market_env=market_env)
        scored.append((score, exp, placeholders))

    if not scored:
        return []

    # 加权不放回抽取：weight = max(0.01, score)
    picked: list[tuple[float, FactorExperience, list[dict[str, Any]]]] = []
    pool = list(scored)
    while pool and len(picked) < max(0, count):
        weights = [max(0.01, s) for s, _e, _p in pool]
        idx = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        picked.append(pool.pop(idx))

    now = _now()
    results: list[dict[str, Any]] = []
    for score, exp, placeholders in picked:
        instantiated = []
        for ph in placeholders:
            value = _instantiate_value(rng, ph)
            instantiated.append({**ph, "value": value})
        formula = gen.instantiate(exp.formula_template, instantiated)
        exp.use_count = int(exp.use_count or 0) + 1
        exp.last_used_at = now
        results.append({
            "experience_id": exp.id,
            "formula": formula,
            "template": exp.formula_template,
            "placeholders": instantiated,
            "category": exp.category,
            "source": exp.source,
            "score": round(score, 6),
        })
    db.commit()
    return results


def related_experiences(
    db: Session, *, experience_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """相关经验查询（M1 实现，F2 二期才调用）：同 category 非自身，按
    success_rate / use_count 降序；archived 与负样本永不返回。"""
    self_row = db.get(FactorExperience, experience_id)
    if self_row is None:
        return []
    rows = db.execute(
        select(FactorExperience)
        .where(FactorExperience.category == self_row.category)
        .where(FactorExperience.id != experience_id)
        .where(FactorExperience.is_negative_sample == 0)
        .where(FactorExperience.status != _STATUS_ARCHIVED)
        .order_by(FactorExperience.success_rate.desc(),
                  FactorExperience.use_count.desc())
        .limit(max(0, int(limit)))
    ).scalars().all()
    return [
        {
            "experience_id": r.id,
            "template": r.formula_template,
            "category": r.category,
            "source": r.source,
            "success_rate": float(r.success_rate or 0.0),
            "avg_icir": r.avg_icir,
            "use_count": int(r.use_count or 0),
        }
        for r in rows
    ]


def _experience_dict(db: Session, row: FactorExperience) -> dict[str, Any]:
    """经验详情序列化（含指标历史；数值过 to_db_float 纪律）。"""
    metrics = db.execute(
        select(FactorExperienceMetric)
        .where(FactorExperienceMetric.experience_id == row.id)
        .order_by(FactorExperienceMetric.recorded_at.asc())
    ).scalars().all()
    complexity: dict[str, Any] = {}
    try:
        complexity = json.loads(row.complexity_json or "{}")
    except ValueError:
        complexity = {}
    return {
        "experience_id": row.id,
        "formula_template": row.formula_template,
        "category": row.category,
        "source": row.source,
        "complexity": complexity,
        "avg_icir": to_db_float(row.avg_icir),
        "use_count": int(row.use_count or 0),
        "success_count": int(row.success_count or 0),
        "success_rate": to_db_float(row.success_rate),
        "status": row.status,
        "is_negative_sample": int(row.is_negative_sample or 0),
        "metrics": [
            {
                "metric_type": m.metric_type,
                "value": to_db_float(m.value),
                "period": m.period,
                "is_oos": int(m.is_oos or 0),
            }
            for m in metrics
        ],
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_experiences(
    db: Session, *, page: int = 1, page_size: int = 20,
    category: str | None = None, source: str | None = None,
    is_negative_sample: int | None = None,
) -> dict[str, Any]:
    """分页经验列表（B3；archived 也返回，供前端归档管理）。"""
    q = select(FactorExperience)
    cq = select(func.count()).select_from(FactorExperience)
    if category:
        q = q.where(FactorExperience.category == str(category))
        cq = cq.where(FactorExperience.category == str(category))
    if source:
        q = q.where(FactorExperience.source == str(source))
        cq = cq.where(FactorExperience.source == str(source))
    if is_negative_sample is not None:
        q = q.where(FactorExperience.is_negative_sample == int(bool(is_negative_sample)))
        cq = cq.where(FactorExperience.is_negative_sample == int(bool(is_negative_sample)))
    total = int(db.execute(cq).scalar_one() or 0)
    rows = db.execute(
        q.order_by(FactorExperience.created_at.desc())
        .offset((max(1, page) - 1) * max(1, page_size))
        .limit(max(1, page_size))
    ).scalars().all()
    return {"items": [_experience_dict(db, r) for r in rows],
            "total": total, "page": max(1, page),
            "page_size": max(1, page_size)}


def get_experience(db: Session, experience_id: str) -> dict[str, Any] | None:
    """经验详情（含指标历史）。"""
    row = db.get(FactorExperience, str(experience_id))
    if row is None:
        return None
    return _experience_dict(db, row)


def archive_experience(db: Session, experience_id: str) -> dict[str, Any]:
    """归档经验：status → archived（抽取与抽样硬化排除：负样本规避 + archived）。"""
    row = db.get(FactorExperience, str(experience_id))
    if row is None:
        raise ValueError(f"experience_not_found:{experience_id}")
    row.status = _STATUS_ARCHIVED
    db.commit()
    return {"experience_id": row.id, "status": row.status}


__all__ = [
    "CATEGORY_WHITELIST",
    "SOURCE_WHITELIST",
    "METRIC_TYPE_WHITELIST",
    "store_experience",
    "sample_experiences",
    "related_experiences",
    "list_experiences",
    "get_experience",
    "archive_experience",
]
