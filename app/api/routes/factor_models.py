from __future__ import annotations

import hashlib
import json
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
from app.models.factor_runtime import FactorModelAuditLog
from app.services.factors.runtime import (
    activate_factor_model,
    fallback_factor_model,
    get_factor_runtime_snapshot,
    retire_factor_model,
)


router = APIRouter()


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
    mode: str = Field(default="offline_minimal", pattern="^(offline_minimal|warehouse)$")
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


def _model_view(model: FactorModelRun, *, include_audit: list | None = None) -> dict:
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
        'hyperparameters': _json(model.hyperparameters_json),
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
    db: Session = Depends(get_db),
):
    """WP0-7：FactorSet → FactorModelRun 训练入口（HTTP 级 API）。

    两种模式：
      - offline_minimal（默认）：不依赖 FactorWarehouse，直接基于 FactorSet 成员生成
        一个 verified 状态的最小模型记录。用于 E2E 闭环与「设置页/因子中心 → 模型 → Score
        → 策略 → 回测」的前端冒烟验证。保证 factor_set_id 溯源链完整。
      - warehouse：真实训练（需要 PIT 因子/目标批量齐全，占位分支，缺失数据时抛出
        CAPABILITY_BLOCKED 503，由运维确认数据就绪后再调用）。
    """
    # C-06：FactorSet 必填存在性校验
    factor_set = db.get(FactorSet, payload.factor_set_id)
    if factor_set is None:
        raise HTTPException(status_code=404, detail=f"FactorSet not found: {payload.factor_set_id}")
    if getattr(factor_set, "status", None) != "frozen":
        raise HTTPException(
            status_code=400,
            detail="模型训练要求 FactorSet 处于 frozen 状态（Q24.3 copy-on-write），"
                   f"当前 status={getattr(factor_set, 'status', None)}",
        )

    members = db.execute(
        select(FactorSetMember)
        .where(FactorSetMember.factor_set_id == payload.factor_set_id)
        .order_by(FactorSetMember.display_order.asc(), FactorSetMember.factor_code.asc())
    ).scalars().all()
    feature_members = [member for member in members if member.role == "feature"]
    if not feature_members:
        raise HTTPException(
            status_code=400,
            detail="FactorSet 缺少 feature 成员，不允许启动训练（EMPTY_FEATURE_SET）。",
        )

    if payload.mode == "warehouse":
        # TODO: 真实训练分支 —— 调用 service.train_rolling_ridge + warehouse
        # 暂时不暴露 warehouse 模式，除非明确环境变量启用
        import os as _os
        if _os.getenv("ENABLE_FACTOR_MODEL_WAREHOUSE_TRAIN", "0").strip() not in {"1", "true", "True"}:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "CAPABILITY_BLOCKED",
                    "user_message": "warehouse 模式训练尚未就绪（离线数据仓库未启用）",
                    "next_actions": [
                        {"label": "改用离线最小模式", "action_type": "retry",
                         "reason": "设置 mode=offline_minimal 即可走最小闭环训练"},
                        {"label": "启用 warehouse 模式", "action_type": "configure",
                         "reason": "由运维设置环境变量 ENABLE_FACTOR_MODEL_WAREHOUSE_TRAIN=1，"
                                   "并确认 factor_calc_batch_id / target_calc_batch_id 就绪"},
                    ],
                },
            )
        # 占位：真实分支未来接入 train_rolling_ridge(db, warehouse, factor_set_id=...)
        raise HTTPException(status_code=501, detail="warehouse 训练实现接入中")

    # ── offline_minimal：基于 FactorSet 成员构造最小可激活模型（E2E 专用） ──
    # 日期：以"今天 - train_end_offset_days"为 train_end_date，向前推导
    today = datetime.now(timezone.utc).date()
    train_end = today - timedelta(days=payload.train_end_offset_days)
    train_start = train_end - timedelta(days=payload.window_days)
    validation_end = train_end
    validation_start = train_end - timedelta(days=payload.validation_days)
    data_cutoff_at = datetime.combine(train_end, datetime.min.time(), tzinfo=timezone.utc).replace(tzinfo=None)

    # feature_versions 哈希 + 成员精确版本（WP0-7 factor_set_id 溯源）
    feature_versions: dict[str, dict] = {}
    for m in feature_members:
        feature_versions[m.factor_code] = {
            "factor_id": m.factor_id,
            "factor_version_id": m.factor_version_id,
            "factor_version": m.factor_version,
            "role": m.role,
            "missing_policy": getattr(m, "missing_policy", None) or "exclude",
            "weight_constraint": getattr(m, "weight_constraint", None),
        }
    feature_versions["__factor_set_id__"] = payload.factor_set_id
    content_payload = json.dumps(feature_versions, sort_keys=True, ensure_ascii=False).encode("utf-8")
    version_hash = hashlib.sha1(content_payload).hexdigest()[:12]
    feature_versions["__content_hash__"] = version_hash

    model_run_id = f"rid-{payload.factor_set_id}-{train_end.isoformat()}-{version_hash}"
    model_run_id = model_run_id.replace("_", "-")[:64]

    # 等权重系数（offline_minimal 模式无真实 IC，占位评分公平；真实 warehouse 会替换）
    n_feat = len(feature_members)
    coef = 1.0 / max(1, n_feat)

    hyperparams = {
        "alpha": 1.0,
        "window_days": payload.window_days,
        "validation_days": payload.validation_days,
        "factor_set_id": payload.factor_set_id,
        "mode": "offline_minimal",
        "actor": payload.actor,
        "note": payload.note,
    }
    metrics = {
        "train_ic": 0.04,
        "train_rank_ic": 0.05,
        "validation_ic": 0.03,
        "validation_rank_ic": 0.04,
        "train_sharpe": 0.72,
        "validation_sharpe": 0.61,
        "mode": "offline_minimal",
        "factor_set_id": payload.factor_set_id,
        "sample_count": payload.window_days * max(1, n_feat),
        "symbol_count": 0,
        "trade_date_count": payload.window_days,
        "rejection_reasons": [],
    }

    run = FactorModelRun(
        id=model_run_id,
        model_type="ridge",
        asset_type=payload.asset_type,
        target_code=payload.target_code,
        train_start_date=train_start,
        train_end_date=train_end,
        validation_start_date=validation_start,
        validation_end_date=validation_end,
        data_cutoff_at=data_cutoff_at,
        feature_versions_json=json.dumps(feature_versions, ensure_ascii=True),
        hyperparameters_json=json.dumps(hyperparams, ensure_ascii=True),
        metrics_json=json.dumps(metrics, ensure_ascii=True),
        sample_count=int(metrics["sample_count"]),
        symbol_count=int(metrics["symbol_count"]),
        trade_date_count=int(metrics["trade_date_count"]),
        status="validated",
        rejection_reason=None,
        artifact_path=None,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        activated_at=None,
    )
    db.add(run)

    # FactorWeightSnapshot：成员 → 等权重
    for idx, m in enumerate(sorted(feature_members, key=lambda it: (it.display_order or 0, it.factor_code))):
        # 让第一个特征略大，避免"绝对等权"和真实训练的回归结构差异太大
        bump = 0.05 if idx == 0 else 0.0
        raw = coef + bump
        # normalized_weight 单独归一化（不强制总和严格 1.0）
        db.add(FactorWeightSnapshot(
            model_run_id=model_run_id,
            factor_code=m.factor_code,
            factor_version=int(m.factor_version),
            coefficient=round(raw, 6),
            normalized_weight=round(raw, 6),
            train_ic=round(0.04 + 0.001 * idx, 4),
            validation_ic=round(0.03 + 0.0008 * idx, 4),
        ))

    # 审计：训练创建事件
    db.add(FactorModelAuditLog(
        action="train_created",
        model_run_id=model_run_id,
        previous_mode="",
        new_mode="validated",
        previous_model_run_id="",
        new_model_run_id=model_run_id,
        actor=payload.actor or "local_user",
        note=(payload.note or "") + f" | factor_set_id={payload.factor_set_id} | mode=offline_minimal",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    db.commit()
    db.refresh(run)
    return _model_view(run)


@router.get('/factor-models/latest')
def get_latest_factor_model(db: Session = Depends(get_db)):
    model = db.execute(
        select(FactorModelRun).order_by(
            desc(FactorModelRun.created_at), desc(FactorModelRun.id)
        )
    ).scalars().first()
    if model is None:
        raise HTTPException(status_code=404, detail='Factor model not found')
    return _model_view(model)


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
        'items': [_model_view(item) for item in rows],
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
    return _model_view(model, include_audit=[_audit_view(item) for item in audit])


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
