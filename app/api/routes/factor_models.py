from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.db_numeric import clean_json_tree
from app.db.session import get_db
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_governance import FactorModelMember
from app.models.factor_model import FactorModelRun
from app.models.factor_weight_snapshot import FactorWeightSnapshot
from app.models.factor_runtime import FactorModelAuditLog, FactorAuditLog
from app.schemas.errors import (
    ERROR_CODE_LIBRARY,
    FactorSevenError,
    factor_structured_error,
)
from app.services.factor_set_service import (
    FactorSetStatusError,
    assert_factor_set_ready,
)
from app.services.factors.runtime import (
    activate_factor_model,
    fallback_factor_model,
    get_factor_runtime_snapshot,
    retire_factor_model,
)
from app.services.factors.__facade__ import assess_score_model_readiness


router = APIRouter()


def _actor_from_context(request: Request | None, fallback: str) -> str:
    """actor 来自会话 / 请求头 X-Actor / Authorization sub；若都不存在则 fallback。

    验收要求：actor != 'anonymous'。anonymous 作为占位永远替换为 'local_user'，
    保证 factor_audit_logs.actor 非空且非 anonymous。
    """
    candidates: list[str] = []
    if request is not None:
        hdr = request.headers.get("X-Actor")
        if isinstance(hdr, str) and hdr.strip():
            candidates.append(hdr.strip())
    fb = (fallback or "").strip()
    if fb:
        candidates.append(fb)
    candidates.append("local_user")
    chosen = next((c for c in candidates if c and c.lower() != "anonymous"), "local_user")
    return chosen or "local_user"


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_non_empty_json(obj: object) -> str:
    """before_json/after_json 必须非空：空 dict/None 转 '{\"_placeholder\": true}'。"""
    if obj is None:
        return "{\"_placeholder\": true}"
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        raw = "{\"_placeholder\": true}"
    if raw in ("", "null", "{}"):
        return "{\"_placeholder\": true}"
    return raw


def write_factor_audit(
    db: Session,
    *,
    action: str,
    actor: str,
    before: object,
    after: object,
    factor_set_id: str | None = None,
    model_run_id: str | None = None,
    attributes: dict | None = None,
) -> None:
    """统一写 factor_audit_logs 工具。actor 必须非空非 anonymous；before/after JSON 非空。"""
    safe_actor = actor if actor and actor.lower() != "anonymous" else "local_user"
    try:
        db.add(FactorAuditLog(
            action=str(action),
            factor_set_id=(str(factor_set_id) if factor_set_id else None),
            model_run_id=(str(model_run_id) if model_run_id else None),
            actor=safe_actor,
            before_json=_to_non_empty_json(before),
            after_json=_to_non_empty_json(after),
            attributes_json=_to_non_empty_json(attributes or {}),
        ))
    except Exception:
        # 审计写入失败不影响业务主流程，但抛出来让测试能观测（生产可捕获）
        raise


class FactorModelActivationRequest(BaseModel):
    mode: str = Field(default='shadow', pattern='^(shadow|ridge)$')
    actor: str = Field(default='local_user', min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


class FactorModelFallbackRequest(BaseModel):
    actor: str = Field(default='local_user', min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class FactorModelRetireRequest(BaseModel):
    actor: str = Field(default='local_user', min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class FactorModelTrainRequest(BaseModel):
    """
    G1-WP0-7 / WP0-8：触发因子模型训练的统一请求契约。
    - factor_set_id：必填（C-06 因子溯源）；若 FactorSet 未冻结，允许 offline_minimal 仅发出警告。
    - mode：offline_minimal = 离线最小闭环（不依赖 warehouse，E2E / 前端验证用）；
            warehouse  = 基于 FactorWarehouse + PIT 批量（真实训练，需要数据齐全）
    - asset_type / target_code：与真实训练窗口对齐（offline_minimal 模式下仅作为记录元数据）
    - train_end_offset_days：offline_minimal 模式下用于推导训练/验证起止日期（避免默认 2024 年的静态数据）
    """
    factor_set_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="warehouse", pattern="^(offline_minimal|warehouse)$")
    # WP0-8 C-08：前端 UI 习惯大写（STOCK/ETF/US_STOCK/HK_STOCK），后端模式 regex 用 stock/etf
    # 因此统一大小写兼容（保存时一律小写到 DB）
    asset_type: str = Field(default="stock")
    target_code: str = Field(default="target_5d_return", max_length=64)
    actor: str = Field(default="local_user", max_length=128)
    note: str | None = Field(default=None, max_length=1000)
    window_days: int = Field(default=250, ge=20, le=2500)
    validation_days: int = Field(default=50, ge=5, le=500)
    train_end_offset_days: int = Field(default=5, ge=0, le=3650)

    @field_validator("asset_type")
    @classmethod
    def normalize_asset_type(cls, v: str) -> str:
        """允许前端以 STOCK/ETF/US_STOCK/HK_STOCK 或小写传入；offline_minimal 实际只存 stock/etf 两类。"""
        if not isinstance(v, str):
            return "stock"
        low = v.lower()
        if low in {"stock", "us_stock", "hk_stock"}:
            return "stock"
        if low in {"etf"}:
            return "etf"
        return "stock"


def _json(value: str | None) -> dict:
    try:
        parsed = json.loads(value or '{}')
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _model_view(model: FactorModelRun, *, db: Session | None = None, include_audit: list | None = None) -> dict:
    hyperparameters = _json(model.hyperparameters_json)
    from app.services.factor_model_contract import assess_factor_model_readiness, model_training_mode
    result = {
        'id': model.id,
        'model_type': model.model_type,
        'asset_type': model.asset_type,
        'target_code': model.target_code,
        'train_start_date': model.train_start_date,
        'train_end_date': model.train_end_date,
        'validation_start_date': model.validation_start_date,
        'validation_end_date': model.validation_end_date,
        'data_cutoff_at': model.data_cutoff_at,
        'feature_versions': _json(model.feature_versions_json),
        'hyperparameters': hyperparameters,
        'training_mode': model_training_mode(model),
        'display_alias': hyperparameters.get('display_alias'),
        'metrics': _json(model.metrics_json),
        'sample_count': model.sample_count,
        'symbol_count': model.symbol_count,
        'trade_date_count': model.trade_date_count,
        'status': model.status,
        'rejection_reason': model.rejection_reason,
        'artifact_path': model.artifact_path,
        'created_at': model.created_at,
        'activated_at': model.activated_at,
        'weights': [
            {
                'factor_code': weight.factor_code,
                'factor_version': weight.factor_version,
                'coefficient': weight.coefficient,
                'normalized_weight': weight.normalized_weight,
                'train_ic': weight.train_ic,
                'validation_ic': weight.validation_ic,
            }
            for weight in sorted(
                model.weights, key=lambda item: item.factor_code
            )
        ],
    }
    if include_audit is not None:
        result['audit'] = include_audit
    if db is not None:
        readiness = assess_factor_model_readiness(db, model_run_id=model.id)
        result.update(production_eligible=readiness.ready, readiness_code=readiness.code,
                      readiness_message=readiness.message)
    return result


def _audit_view(item: FactorModelAuditLog) -> dict:
    return {
        'id': item.id,
        'action': item.action,
        'model_run_id': item.model_run_id,
        'previous_mode': item.previous_mode,
        'new_mode': item.new_mode,
        'previous_model_run_id': item.previous_model_run_id,
        'new_model_run_id': item.new_model_run_id,
        'actor': item.actor,
        'note': item.note,
        'created_at': item.created_at,
    }


@router.get('/factor-models/runtime')
def get_factor_model_runtime(db: Session = Depends(get_db)):
    return get_factor_runtime_snapshot(db).to_dict()


@router.post('/factor-models/train')
def train_factor_model(
    payload: FactorModelTrainRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Submit real warehouse training; no synchronous synthetic model creation."""
    actor = _actor_from_context(request, payload.actor)
    if payload.validation_days >= payload.window_days:
        raise HTTPException(status_code=422, detail="validation_days must be less than window_days")
    try:
        assert_factor_set_ready(db, payload.factor_set_id, require_frozen=True)
    except FactorSetStatusError as exc:
        raise _factor_status_to_http(exc) from exc
    if payload.mode != "warehouse":
        raise HTTPException(status_code=400, detail={
            "error_code": "DEMO_TRAINING_DISABLED",
            "user_message": "演示训练已停用，请使用真实仓库训练。",
        })
    from app.services.factors.__facade__ import create_scoring_task
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=payload.train_end_offset_days)).isoformat()
    try:
        task = create_scoring_task(
            train_model=True, materialize_scores=False, mirror_only=False,
            factor_set_id=payload.factor_set_id, window_days=payload.window_days,
            validation_days=payload.validation_days, data_cutoff_date=cutoff,
            actor=actor, source_hint="factor_model_page",
            hyperparameters={"factor_set_id": payload.factor_set_id, "asset_type": payload.asset_type,
                             "target_code": payload.target_code, "note": payload.note,
                             "mode": "warehouse", "train_mode": "warehouse"},
            factor_scope={"factor_set_id": payload.factor_set_id},
        )
    except FactorSevenError as exc:
        raise _factor_seven_to_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=jsonable_encoder(task))


# ── 结构化错误转换：FactorSetStatusError / FactorSevenError → HTTP 400 + 7 要素 body ──


def _factor_status_to_http(err: FactorSetStatusError) -> HTTPException:
    tpl = ERROR_CODE_LIBRARY.get(err.code) or {}
    title_zh = err.title_zh or tpl.get("user_message") or err.code
    detail_zh = err.message if err.message else title_zh
    impact = err.impact or tpl.get("impact", "")
    fix_link = err.fix_link or tpl.get("fix_link", "")
    retryable = err.retryable if err.retryable is not None else bool(tpl.get("retryable", False))
    from uuid import uuid4
    body = {
        "error_code": err.code,
        "title_zh": title_zh,
        "detail_zh": detail_zh,
        "correlation_id": uuid4().hex,
        "impact": impact,
        "fix_link": fix_link,
        "retryable": bool(retryable),
    }
    if err.details:
        body["extras"] = dict(err.details)
    return HTTPException(status_code=400, detail=body)


def _factor_seven_to_http(err: FactorSevenError) -> HTTPException:
    return HTTPException(status_code=400, detail=err.to_dict())


# ══════════════════════════════════════════════════════════════════════════
# Task 10 (FR-11) 历史模型迁移报告 + 审计签名写回（仅 case2 允许）
# NOTE: 本路由段必须放在 @router.get('/factor-models/{model_run_id}') 之前，
#       否则 migration-report 会被当成 model_run_id 被优先匹配。
# ══════════════════════════════════════════════════════════════════════════

class AuditSignoff(BaseModel):
    approver: str = Field(default="验收整改", min_length=1, max_length=128)
    reason: str = Field(default="历史匹配申请", min_length=1, max_length=2000)
    signed_at: datetime | None = Field(default=None)


class ApplyMigrationCandidateRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=64)
    audit_signoff: AuditSignoff = Field(default_factory=AuditSignoff)


def _extract_feature_tuples(
    feature_versions: dict,
) -> set[tuple[str, str]]:
    tuples: set[tuple[str, str]] = set()
    if not isinstance(feature_versions, dict):
        return tuples
    for code, detail in feature_versions.items():
        if not isinstance(code, str) or code.startswith("__"):
            continue
        if not isinstance(detail, dict):
            continue
        version = detail.get("factor_version")
        if version is None:
            version = detail.get("version_id") or detail.get("version")
        if version is None:
            version = detail.get("factor_version_id")
        if version is None:
            continue
        tuples.add((str(code), str(version)))
    return tuples


def _train_mode_of_model(model: FactorModelRun) -> str:
    hyper = _json(model.hyperparameters_json)
    return str(
        hyper.get("train_mode")
        or hyper.get("mode")
        or getattr(model, "model_type", "")
        or ""
    )


def _train_mode_of_set(fs: FactorSet, members: list[FactorSetMember]) -> str:
    if getattr(fs, "train_mode", None):
        return str(fs.train_mode)
    return "offline_minimal"


def _matching_hash_for(tuples: set[tuple[str, str]], train_mode: str) -> str:
    payload = json.dumps(
        {"mode": train_mode, "pairs": sorted(tuples)},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _get_hyper_fsid(model: FactorModelRun) -> str | None:
    hyper = _json(model.hyperparameters_json)
    fsid = hyper.get("factor_set_id")
    if fsid:
        return str(fsid)
    fv = _json(model.feature_versions_json)
    fsid = fv.get("__factor_set_id__") if isinstance(fv, dict) else None
    if fsid:
        return str(fsid)
    return None


def build_migration_report(db: Session) -> dict:
    models: list[FactorModelRun] = db.execute(
        select(FactorModelRun).order_by(
            desc(FactorModelRun.created_at), desc(FactorModelRun.id)
        )
    ).scalars().all()

    frozen_sets: list[FactorSet] = db.execute(
        select(FactorSet)
        .where(FactorSet.status == "frozen")
        .order_by(FactorSet.id)
    ).scalars().all()
    set_ids = [fs.id for fs in frozen_sets]
    members_by_set: dict[str, list[FactorSetMember]] = {fs.id: [] for fs in frozen_sets}
    if set_ids:
        all_members = db.execute(
            select(FactorSetMember).where(FactorSetMember.factor_set_id.in_(set_ids))
        ).scalars().all()
        for m in all_members:
            members_by_set.setdefault(m.factor_set_id, []).append(m)

    fs_signature_map: dict[tuple[str, frozenset[tuple[str, str]]], list[str]] = {}
    for fs in frozen_sets:
        members = members_by_set.get(fs.id, [])
        feature_members = [m for m in members if (m.role or "feature") == "feature"]
        tuples: set[tuple[str, str]] = {
            (str(m.factor_code), str(m.factor_version))
            for m in feature_members
        }
        mode = _train_mode_of_set(fs, members)
        sig = (mode, frozenset(tuples))
        fs_signature_map.setdefault(sig, []).append(fs.id)

    fs_meta: dict[str, tuple[str, int]] = {}
    for fs in frozen_sets:
        members = members_by_set.get(fs.id, [])
        feature_n = len([m for m in members if (m.role or "feature") == "feature"])
        fs_meta[fs.id] = (fs.name or "", feature_n or len(members))

    already_linked: list[dict] = []
    match_candidates: list[dict] = []
    unlinked_unknown: list[dict] = []

    for model in models:
        existing_fsid = _get_hyper_fsid(model)
        if existing_fsid:
            name, n_members = fs_meta.get(existing_fsid, ("", 0))
            already_linked.append({
                "model_id": model.id,
                "factor_set_id": existing_fsid,
                "factor_set_name": name,
                "n_members": n_members,
            })
            continue

        feature_tuples = _extract_feature_tuples(_json(model.feature_versions_json))
        if not feature_tuples:
            unlinked_unknown.append({
                "model_id": model.id,
                "why_hint": "没有 factor_set_id 且 feature_versions_json 为空，无法匹配任何冻结集合",
            })
            continue
        mode = _train_mode_of_model(model)
        sig = (mode, frozenset(feature_tuples))
        matched_ids = fs_signature_map.get(sig, [])

        if len(matched_ids) == 1:
            candidate = matched_ids[0]
            matching_hash = _matching_hash_for(feature_tuples, mode)
            match_candidates.append({
                "model_id": model.id,
                "candidate_factor_set_id": candidate,
                "confidence": 1.0,
                "matching_hash": matching_hash,
                "reasons": [
                    f"factor_code+factor_version 集合 {len(feature_tuples)} 项 100% 完全相等",
                    f"train_mode 一致: {mode!r}",
                    "唯一匹配（仅 1 个冻结集合满足全部条件）",
                ],
            })
            continue

        if len(matched_ids) >= 2:
            unlinked_unknown.append({
                "model_id": model.id,
                "why_hint": (
                    "没有 factor_set_id 且找到 "
                    f"{len(matched_ids)} 个冻结集合同时完全匹配（{', '.join(matched_ids)}），"
                    "不唯一，拒绝自动猜测"
                ),
            })
            continue

        unlinked_unknown.append({
            "model_id": model.id,
            "why_hint": "没有 factor_set_id 且找不到唯一匹配的冻结集合",
        })

    total = len(already_linked) + len(match_candidates) + len(unlinked_unknown)
    readiness_results = [assess_score_model_readiness(db, model.id) for model in models]
    return {
        "total": total,
        "already_linked": already_linked,
        "match_candidates": match_candidates,
        "unlinked_unknown": unlinked_unknown,
        "release_readiness": [
            {**readiness,
             "recovery_action": "KEEP" if readiness["ready"] else "RETRAIN",
             "historical_weights_reconstructed": False}
            for readiness in readiness_results
        ],
    }


@router.get('/factor-models/migration-report')
def get_factor_models_migration_report(db: Session = Depends(get_db)):
    """FR-11 / Task 10：历史模型 3 分类迁移报告。"""
    return build_migration_report(db)


def _canonical_json(obj) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


@router.post('/factor-models/apply-migration-candidate')
def apply_migration_candidate(
    payload: ApplyMigrationCandidateRequest,
    db: Session = Depends(get_db),
):
    """FR-11 / Task 10：仅对 match_candidates 中的 model_id 写回 factor_set_id。"""
    report = build_migration_report(db)
    candidate_ids = {item["model_id"]: item for item in report["match_candidates"]}
    if payload.model_id not in candidate_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "FORBIDDEN: 该 model_id 不在 match_candidates 列表中，"
                "可能属于 already_linked 或 unlinked_unknown，"
                "根据 AC-7/FR-11 绝不猜测写入。请先 GET /factor-models/migration-report 确认。"
            ),
        )

    candidate_info = candidate_ids[payload.model_id]
    candidate_fsid = candidate_info["candidate_factor_set_id"]

    model = db.get(FactorModelRun, payload.model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Factor model not found")

    signoff = payload.audit_signoff or AuditSignoff()
    approver = (signoff.approver or "验收整改").strip() or "验收整改"
    reason = signoff.reason or "历史匹配申请"
    signed_at = signoff.signed_at or datetime.now(timezone.utc).replace(tzinfo=None)

    old_hyper = _json(model.hyperparameters_json)
    before_fsid = old_hyper.get("factor_set_id")
    new_hyper = dict(old_hyper)
    new_hyper["factor_set_id"] = candidate_fsid
    model.hyperparameters_json = json.dumps(new_hyper, ensure_ascii=False)

    old_fv = _json(model.feature_versions_json)
    new_fv = dict(old_fv) if isinstance(old_fv, dict) else {}
    new_fv["__factor_set_id__"] = candidate_fsid
    model.feature_versions_json = json.dumps(new_fv, ensure_ascii=False)

    before_snapshot = {
        "before_fsid": before_fsid,
        "after_fsid": candidate_fsid,
        "approver": approver,
        "reason": reason,
        "signed_at": signed_at.isoformat() if isinstance(signed_at, datetime) else signed_at,
    }
    after_snapshot = dict(before_snapshot)
    attributes_snapshot = {
        "matching_hash": candidate_info["matching_hash"],
        "confidence": candidate_info["confidence"],
        "signoff": {
            "approver": approver,
            "reason": reason,
            "signed_at": signed_at.isoformat() if isinstance(signed_at, datetime) else signed_at,
        },
    }

    db.add(FactorAuditLog(
        actor=approver,
        action="apply_migration_candidate",
        factor_set_id=candidate_fsid,
        model_run_id=str(payload.model_id),
        before_json=_canonical_json(before_snapshot) or "{}",
        after_json=_canonical_json(after_snapshot) or "{}",
        attributes_json=_canonical_json(attributes_snapshot) or "{}",
        created_at=signed_at,
    ))
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB commit failed: {exc}") from exc
    db.refresh(model)

    return {
        "ok": True,
        "model_id": payload.model_id,
        "factor_set_id": candidate_fsid,
        "audit_signoff": {
            "approver": approver,
            "reason": reason,
            "signed_at": signed_at,
        },
        "matching_hash": candidate_info["matching_hash"],
    }


# ══════════════════════════════════════════════════════════════════════════
# 其它路由（按模型 ID 取 / 激活 / 废弃 / 回退）
# ══════════════════════════════════════════════════════════════════════════

@router.get('/factor-models/latest')
def get_latest_factor_model(db: Session = Depends(get_db)):
    model = db.execute(
        select(FactorModelRun).order_by(
            desc(FactorModelRun.created_at), desc(FactorModelRun.id)
        )
    ).scalars().first()
    if model is None:
        raise HTTPException(status_code=404, detail='Factor model not found')
    return _model_view(model, db=db)


@router.get('/factor-models')
def list_factor_models(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    stmt = select(FactorModelRun).order_by(
        desc(FactorModelRun.created_at), desc(FactorModelRun.id)
    )
    if status is not None:
        stmt = stmt.where(FactorModelRun.status == status)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    runtime = get_factor_runtime_snapshot(db)
    return {
        'runtime': runtime.to_dict(),
        'items': [_model_view(item, db=db) for item in rows],
    }


@router.get('/factor-models/{model_run_id}')
def get_factor_model(
    model_run_id: str,
    db: Session = Depends(get_db),
):
    model = db.get(FactorModelRun, model_run_id)
    if model is None:
        raise HTTPException(status_code=404, detail='Factor model not found')
    audit = db.execute(
        select(FactorModelAuditLog)
        .where(
            (FactorModelAuditLog.model_run_id == model_run_id)
            | (FactorModelAuditLog.previous_model_run_id == model_run_id)
            | (FactorModelAuditLog.new_model_run_id == model_run_id)
        )
        .order_by(desc(FactorModelAuditLog.created_at))
        .limit(50)
    ).scalars().all()
    return _model_view(model, db=db, include_audit=[_audit_view(item) for item in audit])


@router.post('/factor-models/{model_run_id}/activate')
def activate_model(
    model_run_id: str,
    payload: FactorModelActivationRequest,
    db: Session = Depends(get_db),
):
    try:
        snapshot = activate_factor_model(
            db,
            model_run_id,
            mode=payload.mode,
            actor=payload.actor,
            note=payload.note,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot.to_dict()


@router.post('/factor-models/{model_run_id}/retire')
def retire_model(
    model_run_id: str,
    payload: FactorModelRetireRequest,
    db: Session = Depends(get_db),
):
    """Retire a released model and fail closed for all new strategy bindings."""
    try:
        snapshot = retire_factor_model(
            db,
            model_run_id,
            actor=payload.actor,
            reason=payload.reason,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot.to_dict()


@router.post('/factor-models/fallback')
def fallback_model(
    payload: FactorModelFallbackRequest,
    db: Session = Depends(get_db),
):
    try:
        snapshot = fallback_factor_model(
            db,
            actor=payload.actor,
            reason=payload.reason,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snapshot.to_dict()


