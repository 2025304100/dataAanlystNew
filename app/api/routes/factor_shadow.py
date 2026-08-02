"""WP6 Shadow 观测与审批 API 路由。

端点：
- POST /factor-shadow/observations：记录每日观测（幂等）
- GET /factor-shadow/observations：查询观测历史
- GET /factor-shadow/observations/summary：观察期汇总（有效天数、门禁状态）
- GET /factor-shadow/health/{factor_version_id}：健康检查报告
- POST /factor-shadow/activation/request：申请 Shadow → Active
- POST /factor-shadow/activation/approve：批准激活（人工审批）
- POST /factor-shadow/activation/reject：驳回申请
- POST /factor-shadow/quarantine/{factor_id}：数据硬错误自动隔离
- POST /factor-shadow/correlation-governance：相关性治理报告
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.session import SessionLocal
from app.services.factors.factor_correlation import run_correlation_governance
from app.services.factors.factor_shadow import (
    ShadowObservationInput,
    approve_activation,
    auto_quarantine,
    count_valid_shadow_days,
    get_shadow_observations,
    is_shadow_observation_complete,
    record_shadow_observation,
    reject_activation,
    request_activation,
    run_shadow_health_check,
    ActivationRequest,
)


router = APIRouter()


# ── 请求模型 ──────────────────────────────────────────────


class ShadowObservationCreate(BaseModel):
    """记录 Shadow 观测请求。"""

    factor_id: int
    factor_version_id: int
    trade_date: str = Field(..., description="交易日 YYYY-MM-DD")
    observed_symbols: int | None = None
    expected_symbols: int | None = None
    completeness_ratio: float | None = None
    ic_value: float | None = None
    coverage: float | None = None
    turnover: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class ActivationRequestPayload(BaseModel):
    """激活申请请求。"""

    factor_id: int
    factor_version_id: int
    evidence_run_id: str | None = None
    actor: str = "local_user"
    reason: str | None = None


class ActivationApprovePayload(BaseModel):
    """激活批准请求。"""

    factor_id: int
    factor_version_id: int
    approver: str = "local_user"
    reason: str
    evidence_run_id: str | None = None
    request_id: str | None = None


class ActivationRejectPayload(BaseModel):
    """激活驳回请求。"""

    factor_id: int
    reviewer: str = "local_user"
    reason: str
    request_id: str | None = None


class QuarantinePayload(BaseModel):
    """自动隔离请求。"""

    factor_id: int
    factor_version_id: int
    reason: str
    request_id: str | None = None


class CorrelationGovernancePayload(BaseModel):
    """相关性治理请求。"""

    factor_values: dict[str, list[float | None]]
    target: list[float | None]
    index: list[list[str]] = Field(..., description="[[trade_date, symbol], ...]")
    method: str = "spearman"
    cluster_threshold: float = 0.60
    representative_scores: dict[str, float] | None = None


# ── Shadow 观测端点 ───────────────────────────────────────


@router.post("/factor-shadow/observations")
def create_shadow_observation(payload: ShadowObservationCreate):
    """记录 Shadow 每日观测（幂等）。"""
    with SessionLocal() as db:
        try:
            inp = ShadowObservationInput(
                factor_id=payload.factor_id,
                factor_version_id=payload.factor_version_id,
                trade_date=payload.trade_date,
                observed_symbols=payload.observed_symbols,
                expected_symbols=payload.expected_symbols,
                completeness_ratio=payload.completeness_ratio,
                ic_value=payload.ic_value,
                coverage=payload.coverage,
                turnover=payload.turnover,
                metrics=payload.metrics,
            )
            result = record_shadow_observation(db, inp=inp)
            db.commit()
            obs = result.observation
            return {
                "id": obs.id,
                "factor_id": obs.factor_id,
                "factor_version_id": obs.factor_version_id,
                "trade_date": obs.trade_date,
                "is_valid_day": obs.is_valid_day,
                "invalid_reason": obs.invalid_reason,
                "is_new": result.is_new,
                "ic_value": obs.ic_value,
                "coverage": obs.coverage,
                "completeness_ratio": obs.completeness_ratio,
                "health_status": obs.health_status,
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/factor-shadow/observations")
def list_shadow_observations(
    factor_version_id: int = Query(..., description="因子版本 ID"),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    valid_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
):
    """查询 Shadow 观测历史。"""
    with SessionLocal() as db:
        obs_list = get_shadow_observations(
            db,
            factor_version_id=factor_version_id,
            start_date=start_date,
            end_date=end_date,
            valid_only=valid_only,
            limit=limit,
        )
        return [
            {
                "id": o.id,
                "trade_date": o.trade_date,
                "is_valid_day": o.is_valid_day,
                "invalid_reason": o.invalid_reason,
                "ic_value": o.ic_value,
                "coverage": o.coverage,
                "turnover": o.turnover,
                "completeness_ratio": o.completeness_ratio,
                "observed_symbols": o.observed_symbols,
                "expected_symbols": o.expected_symbols,
                "health_status": o.health_status,
                "health_reason": o.health_reason,
                "metrics": json.loads(o.metrics_json) if o.metrics_json else {},
            }
            for o in obs_list
        ]


@router.get("/factor-shadow/observations/summary")
def get_shadow_observation_summary(
    factor_version_id: int = Query(..., description="因子版本 ID"),
):
    """观察期汇总（有效天数、门禁状态）。"""
    with SessionLocal() as db:
        valid_days = count_valid_shadow_days(db, factor_version_id=factor_version_id)
        is_complete, _, reason = is_shadow_observation_complete(
            db, factor_version_id=factor_version_id
        )
        return {
            "factor_version_id": factor_version_id,
            "valid_days": valid_days,
            "min_required_days": 20,
            "is_complete": is_complete,
            "reason": reason,
        }


# ── 健康检查端点 ──────────────────────────────────────────


@router.get("/factor-shadow/health/{factor_version_id}")
def get_shadow_health(factor_version_id: int):
    """获取 Shadow 因子健康报告。"""
    with SessionLocal() as db:
        report = run_shadow_health_check(db, factor_version_id=factor_version_id)
        db.commit()
        return report.to_dict()


# ── 审批流程端点 ──────────────────────────────────────────


@router.post("/factor-shadow/activation/request")
def post_activation_request(payload: ActivationRequestPayload):
    """申请 Shadow → Active（门禁检查）。"""
    with SessionLocal() as db:
        req = ActivationRequest(
            factor_id=payload.factor_id,
            factor_version_id=payload.factor_version_id,
            evidence_run_id=payload.evidence_run_id,
            observation_start="",
            observation_end="",
            valid_days=0,
            actor=payload.actor,
            reason=payload.reason,
        )
        result = request_activation(db, req=req)
        return {
            "success": result.success,
            "factor_id": result.factor_id,
            "from_status": result.from_status,
            "to_status": result.to_status,
            "error": result.error,
            "valid_days": result.valid_days,
            "min_required_days": result.min_required_days,
        }


@router.post("/factor-shadow/activation/approve")
def post_activation_approve(payload: ActivationApprovePayload):
    """批准 Shadow → Active（人工审批）。"""
    with SessionLocal() as db:
        result = approve_activation(
            db,
            factor_id=payload.factor_id,
            factor_version_id=payload.factor_version_id,
            approver=payload.approver,
            reason=payload.reason,
            evidence_run_id=payload.evidence_run_id,
            request_id=payload.request_id,
        )
        db.commit()
        return {
            "success": result.success,
            "factor_id": result.factor_id,
            "from_status": result.from_status,
            "to_status": result.to_status,
            "actor": result.actor,
            "audit_id": result.audit_id,
            "error": result.error,
            "valid_days": result.valid_days,
        }


@router.post("/factor-shadow/activation/reject")
def post_activation_reject(payload: ActivationRejectPayload):
    """驳回 Shadow → Active 申请。"""
    with SessionLocal() as db:
        result = reject_activation(
            db,
            factor_id=payload.factor_id,
            reviewer=payload.reviewer,
            reason=payload.reason,
            request_id=payload.request_id,
        )
        db.commit()
        return {
            "success": result.success,
            "factor_id": result.factor_id,
            "from_status": result.from_status,
            "to_status": result.to_status,
            "actor": result.actor,
            "audit_id": result.audit_id,
            "error": result.error,
        }


@router.post("/factor-shadow/quarantine/{factor_id}")
def post_quarantine(factor_id: int, payload: QuarantinePayload):
    """数据硬错误自动隔离。"""
    with SessionLocal() as db:
        result = auto_quarantine(
            db,
            factor_id=factor_id,
            factor_version_id=payload.factor_version_id,
            reason=payload.reason,
            request_id=payload.request_id,
        )
        db.commit()
        return {
            "success": result.success,
            "factor_id": result.factor_id,
            "from_status": result.from_status,
            "to_status": result.to_status,
            "actor": result.actor,
            "audit_id": result.audit_id,
            "error": result.error,
        }


# ── 相关性治理端点 ────────────────────────────────────────


@router.post("/factor-shadow/correlation-governance")
def post_correlation_governance(payload: CorrelationGovernancePayload):
    """运行相关性治理（相关矩阵 + 聚类 + 残差增量评估）。"""
    # 重建 DataFrame
    index_tuples = [(t, s) for t, s in payload.index]
    multi_index = pd.MultiIndex.from_tuples(index_tuples, names=["trade_date", "symbol"])

    factor_df = pd.DataFrame(
        {code: values for code, values in payload.factor_values.items()},
        index=multi_index,
    )
    target_series = pd.Series(payload.target, index=multi_index, name="target")

    report = run_correlation_governance(
        factor_df,
        target_series,
        method=payload.method,
        cluster_threshold=payload.cluster_threshold,
        representative_scores=payload.representative_scores,
    )
    return report.to_dict()
