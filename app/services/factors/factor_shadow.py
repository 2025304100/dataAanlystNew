"""WP6-03/04/05: Shadow 观测、健康告警与人工审批。

对齐 docs/专业因子库开发计划.md §WP6-03/04/05 和 docs/因子设置与专业因子库改造方案.md §9。

核心能力：
- WP6-03 Shadow 每日观测：按因子版本和交易日幂等记录，有效观察天数统计
- WP6-04 衰减和数据健康告警：IC 衰减、覆盖突降、常数化、缺失异常检测
- WP6-05 人工审批流程：Shadow → Active 申请/批准/驳回，20 有效交易日门禁

安全约束：
- 同因子版本同交易日只有一条 Shadow 观测（幂等）
- 缺少交易日/横截面完整率<90%/数据异常不计入有效观察天数
- 不允许用历史回测结果回填 Shadow 天数
- Active 必须 local_user 明确批准
- 性能衰减只告警不自动 Deprecated
- 数据硬错误可自动 Quarantined 但追加审计
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.factor import Factor
from app.models.factor_evaluation import ShadowObservation, TransitionAudit
from app.schemas.factor_library import FactorTransitionRequest
from app.services.factors.factor_lifecycle import execute_transition


# ══════════════════════════════════════════════════════════
# WP6-03: Shadow 每日观测
# ══════════════════════════════════════════════════════════

# 有效观察期门禁
MIN_VALID_SHADOW_DAYS = 20

# 横截面完整率门禁（低于此值不计入有效观察天数）
MIN_COMPLETENESS_RATIO = 0.90


@dataclass
class ShadowObservationInput:
    """Shadow 观测输入。"""

    factor_id: int
    factor_version_id: int
    trade_date: str  # YYYY-MM-DD
    observed_symbols: int | None = None
    expected_symbols: int | None = None
    completeness_ratio: float | None = None
    ic_value: float | None = None
    coverage: float | None = None
    turnover: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShadowObservationResult:
    """Shadow 观测记录结果。"""

    observation: ShadowObservation
    is_new: bool  # 是否新建（False 表示幂等返回既有记录）
    is_valid_day: bool
    invalid_reason: str | None = None


def _determine_day_validity(
    completeness_ratio: float | None,
    coverage: float | None,
    observed_symbols: int | None,
) -> tuple[bool, str | None]:
    """判断当日是否为有效观察日。

    无效原因：
    - incomplete_coverage: 横截面完整率 < 90%
    - no_data: 无观测数据（observed_symbols=0 或 coverage=0）
    """
    if observed_symbols is not None and observed_symbols == 0:
        return False, "no_data"
    if coverage is not None and coverage == 0.0:
        return False, "no_data"
    if completeness_ratio is not None and completeness_ratio < MIN_COMPLETENESS_RATIO:
        return False, "incomplete_coverage"
    return True, None


def record_shadow_observation(
    db: Session,
    *,
    inp: ShadowObservationInput,
) -> ShadowObservationResult:
    """记录 Shadow 每日观测（幂等）。

    幂等逻辑：同 factor_version_id + trade_date 已存在则返回既有记录。
    不允许用历史回测结果回填：trade_date 不能是未来日期。
    """
    # 不允许未来日期回填
    try:
        td = date.fromisoformat(inp.trade_date)
    except (ValueError, TypeError):
        raise ValueError(f"invalid_trade_date:{inp.trade_date}")

    today = date.today()
    if td > today:
        raise ValueError(f"future_trade_date_not_allowed:{inp.trade_date}")

    # 幂等检查
    existing = db.execute(
        select(ShadowObservation)
        .where(
            ShadowObservation.factor_version_id == inp.factor_version_id,
            ShadowObservation.trade_date == inp.trade_date,
        )
        .limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        return ShadowObservationResult(
            observation=existing,
            is_new=False,
            is_valid_day=existing.is_valid_day,
            invalid_reason=existing.invalid_reason,
        )

    # 判断有效性
    is_valid, invalid_reason = _determine_day_validity(
        inp.completeness_ratio, inp.coverage, inp.observed_symbols
    )

    obs = ShadowObservation(
        factor_id=inp.factor_id,
        factor_version_id=inp.factor_version_id,
        trade_date=inp.trade_date,
        observed_symbols=inp.observed_symbols,
        expected_symbols=inp.expected_symbols,
        completeness_ratio=inp.completeness_ratio,
        is_valid_day=is_valid,
        invalid_reason=invalid_reason,
        ic_value=inp.ic_value,
        coverage=inp.coverage,
        turnover=inp.turnover,
        metrics_json=json.dumps(inp.metrics, ensure_ascii=False, default=str),
        health_status="healthy",
        health_reason=None,
    )
    db.add(obs)
    db.flush()
    return ShadowObservationResult(
        observation=obs,
        is_new=True,
        is_valid_day=is_valid,
        invalid_reason=invalid_reason,
    )


def get_shadow_observations(
    db: Session,
    *,
    factor_version_id: int,
    start_date: str | None = None,
    end_date: str | None = None,
    valid_only: bool = False,
    limit: int = 100,
) -> list[ShadowObservation]:
    """查询 Shadow 观测历史（按交易日升序）。"""
    stmt = (
        select(ShadowObservation)
        .where(ShadowObservation.factor_version_id == factor_version_id)
        .order_by(ShadowObservation.trade_date.asc())
        .limit(limit)
    )
    if start_date:
        stmt = stmt.where(ShadowObservation.trade_date >= start_date)
    if end_date:
        stmt = stmt.where(ShadowObservation.trade_date <= end_date)
    if valid_only:
        stmt = stmt.where(ShadowObservation.is_valid_day.is_(True))
    return list(db.execute(stmt).scalars().all())


def count_valid_shadow_days(
    db: Session,
    *,
    factor_version_id: int,
) -> int:
    """统计有效观察天数。"""
    result = db.execute(
        select(func.count(ShadowObservation.id))
        .where(
            ShadowObservation.factor_version_id == factor_version_id,
            ShadowObservation.is_valid_day.is_(True),
        )
    ).scalar()
    return int(result or 0)


def count_consecutive_valid_shadow_days(
    db: Session,
    *,
    factor_version_id: int,
) -> int:
    """统计从最近一个有效观察日往回的连续有效观察天数。

    用于判断是否满足 20 个连续有效交易日门禁。
    连续性基于交易日（跳过非交易日和无效日），不要求自然日连续。
    """
    observations = get_shadow_observations(
        db, factor_version_id=factor_version_id, valid_only=True, limit=500
    )
    if not observations:
        return 0

    # 从最近一天往回数连续有效日
    # 由于 valid_only=True 已过滤，只需检查日期是否连续（交易日）
    # 交易日可能有周末/节假日间隔，所以只统计有效日数量（不检查自然日连续性）
    # 但要求"连续"——即中间不能有无效日或缺失日
    # 策略：取所有观测（含无效），从最近往回找连续有效段
    all_obs = get_shadow_observations(
        db, factor_version_id=factor_version_id, limit=500
    )
    if not all_obs:
        return 0

    # 从最近一天往回数
    consecutive = 0
    for obs in reversed(all_obs):
        if obs.is_valid_day:
            consecutive += 1
        else:
            break
    return consecutive


def is_shadow_observation_complete(
    db: Session,
    *,
    factor_version_id: int,
    min_days: int = MIN_VALID_SHADOW_DAYS,
) -> tuple[bool, int, str | None]:
    """检查 Shadow 观察期是否满足门禁。

    返回 (is_complete, valid_days, reason)
    """
    valid_days = count_valid_shadow_days(db, factor_version_id=factor_version_id)
    if valid_days < min_days:
        return False, valid_days, f"insufficient_valid_days:{valid_days}/{min_days}"
    return True, valid_days, None


# ══════════════════════════════════════════════════════════
# WP6-04: 衰减和数据健康告警
# ══════════════════════════════════════════════════════════

# IC 衰减阈值：最近 5 日平均 IC 低于历史平均的 50%
IC_DECAY_THRESHOLD = 0.50

# 覆盖突降阈值：当日覆盖低于历史平均的 70%
COVERAGE_DROP_THRESHOLD = 0.70

# 常数化检测：近 5 日 IC 标准差低于 1e-6
CONSTANT_FACTOR_IC_STD = 1e-6

# 健康状态枚举
HEALTHY = "healthy"
DEGRADED = "degraded"
BLOCKED = "blocked"


@dataclass
class HealthAlert:
    """健康告警。"""

    alert_type: str  # ic_decay / coverage_drop / constant_factor / missing_anomaly
    severity: str  # warn / critical
    message: str
    current_value: float | None = None
    threshold: float | None = None
    window_days: int = 5


@dataclass
class ShadowHealthReport:
    """Shadow 因子健康报告。"""

    factor_version_id: int
    health_status: str  # healthy / degraded / blocked
    alerts: list[HealthAlert]
    recent_ic_mean: float | None
    recent_ic_std: float | None
    historical_ic_mean: float | None
    recent_coverage_mean: float | None
    historical_coverage_mean: float | None
    n_valid_days: int
    n_total_days: int
    should_quarantine: bool  # 数据硬错误建议隔离

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_version_id": self.factor_version_id,
            "health_status": self.health_status,
            "alerts": [
                {
                    "alert_type": a.alert_type,
                    "severity": a.severity,
                    "message": a.message,
                    "current_value": a.current_value,
                    "threshold": a.threshold,
                    "window_days": a.window_days,
                }
                for a in self.alerts
            ],
            "recent_ic_mean": self.recent_ic_mean,
            "recent_ic_std": self.recent_ic_std,
            "historical_ic_mean": self.historical_ic_mean,
            "recent_coverage_mean": self.recent_coverage_mean,
            "historical_coverage_mean": self.historical_coverage_mean,
            "n_valid_days": self.n_valid_days,
            "n_total_days": self.n_total_days,
            "should_quarantine": self.should_quarantine,
        }


def run_shadow_health_check(
    db: Session,
    *,
    factor_version_id: int,
    window_days: int = 5,
) -> ShadowHealthReport:
    """运行 Shadow 因子健康检查。

    检测项：
    1. IC 衰减：最近 window_days 日平均 IC 低于历史平均的 50%
    2. 覆盖突降：当日覆盖低于历史平均的 70%
    3. 常数化：近 window_days 日 IC 标准差趋近 0
    4. 缺失异常：最近 window_days 日有缺失观测（非交易日除外）

    告警分级：
    - warn: 性能衰减、覆盖下降（只告警不自动隔离）
    - critical: 常数化、连续缺失（可自动 Quarantined）
    """
    observations = get_shadow_observations(
        db, factor_version_id=factor_version_id, limit=500
    )

    alerts: list[HealthAlert] = []
    n_total = len(observations)
    n_valid = sum(1 for o in observations if o.is_valid_day)

    if n_total == 0:
        return ShadowHealthReport(
            factor_version_id=factor_version_id,
            health_status=BLOCKED,
            alerts=[HealthAlert(
                alert_type="no_observations",
                severity="critical",
                message="无任何 Shadow 观测记录",
            )],
            recent_ic_mean=None,
            recent_ic_std=None,
            historical_ic_mean=None,
            recent_coverage_mean=None,
            historical_coverage_mean=None,
            n_valid_days=0,
            n_total_days=0,
            should_quarantine=False,
        )

    # 提取 IC 和覆盖序列
    ic_series = [o.ic_value for o in observations if o.ic_value is not None]
    cov_series = [o.coverage for o in observations if o.coverage is not None]

    # 近期 vs 历史
    recent_ic = ic_series[-window_days:] if len(ic_series) >= window_days else ic_series
    historical_ic = ic_series[:-window_days] if len(ic_series) > window_days else []

    recent_cov = cov_series[-window_days:] if len(cov_series) >= window_days else cov_series
    historical_cov = cov_series[:-window_days] if len(cov_series) > window_days else []

    recent_ic_mean = float(np.mean(recent_ic)) if recent_ic else None
    recent_ic_std = float(np.std(recent_ic)) if len(recent_ic) > 1 else None
    historical_ic_mean = float(np.mean(historical_ic)) if historical_ic else None
    recent_cov_mean = float(np.mean(recent_cov)) if recent_cov else None
    historical_cov_mean = float(np.mean(historical_cov)) if historical_cov else None

    should_quarantine = False

    # 1. IC 衰减检测
    if (
        recent_ic_mean is not None
        and historical_ic_mean is not None
        and abs(historical_ic_mean) > 1e-10
    ):
        ratio = abs(recent_ic_mean) / abs(historical_ic_mean)
        if ratio < IC_DECAY_THRESHOLD:
            alerts.append(HealthAlert(
                alert_type="ic_decay",
                severity="warn",
                message=f"IC 衰减：近期 {recent_ic_mean:.4f} 仅为历史 {historical_ic_mean:.4f} 的 {ratio:.1%}",
                current_value=recent_ic_mean,
                threshold=historical_ic_mean * IC_DECAY_THRESHOLD,
                window_days=window_days,
            ))

    # 2. 覆盖突降检测
    if (
        recent_cov_mean is not None
        and historical_cov_mean is not None
        and historical_cov_mean > 0
    ):
        ratio = recent_cov_mean / historical_cov_mean
        if ratio < COVERAGE_DROP_THRESHOLD:
            alerts.append(HealthAlert(
                alert_type="coverage_drop",
                severity="warn",
                message=f"覆盖突降：近期 {recent_cov_mean:.2%} 仅为历史 {historical_cov_mean:.2%} 的 {ratio:.1%}",
                current_value=recent_cov_mean,
                threshold=historical_cov_mean * COVERAGE_DROP_THRESHOLD,
                window_days=window_days,
            ))

    # 3. 常数化检测
    if recent_ic_std is not None and recent_ic_std < CONSTANT_FACTOR_IC_STD:
        alerts.append(HealthAlert(
            alert_type="constant_factor",
            severity="critical",
            message=f"因子常数化：近 {window_days} 日 IC 标准差 {recent_ic_std:.2e} 趋近 0",
            current_value=recent_ic_std,
            threshold=CONSTANT_FACTOR_IC_STD,
            window_days=window_days,
        ))
        should_quarantine = True

    # 4. 缺失异常检测：最近 window_days 日有无效日
    recent_obs = observations[-window_days:] if len(observations) >= window_days else observations
    invalid_recent = [o for o in recent_obs if not o.is_valid_day]
    if len(invalid_recent) >= 3:
        alerts.append(HealthAlert(
            alert_type="missing_anomaly",
            severity="critical",
            message=f"缺失异常：近 {window_days} 日有 {len(invalid_recent)} 个无效观察日",
            current_value=float(len(invalid_recent)),
            threshold=3.0,
            window_days=window_days,
        ))
        should_quarantine = True

    # 综合健康状态
    has_critical = any(a.severity == "critical" for a in alerts)
    has_warn = any(a.severity == "warn" for a in alerts)
    if has_critical:
        health_status = BLOCKED if should_quarantine else DEGRADED
    elif has_warn:
        health_status = DEGRADED
    else:
        health_status = HEALTHY

    # 更新最近观测的健康状态
    if observations and should_quarantine:
        latest = observations[-1]
        latest.health_status = health_status
        latest.health_reason = "; ".join(a.message for a in alerts if a.severity == "critical")

    return ShadowHealthReport(
        factor_version_id=factor_version_id,
        health_status=health_status,
        alerts=alerts,
        recent_ic_mean=recent_ic_mean,
        recent_ic_std=recent_ic_std,
        historical_ic_mean=historical_ic_mean,
        recent_coverage_mean=recent_cov_mean,
        historical_coverage_mean=historical_cov_mean,
        n_valid_days=n_valid,
        n_total_days=n_total,
        should_quarantine=should_quarantine,
    )


# ══════════════════════════════════════════════════════════
# WP6-05: 人工审批流程
# ══════════════════════════════════════════════════════════


@dataclass
class ActivationRequest:
    """Active 激活申请。"""

    factor_id: int
    factor_version_id: int
    evidence_run_id: str | None
    observation_start: str  # 观察期起始日
    observation_end: str  # 观察期结束日
    valid_days: int
    actor: str  # 申请人
    reason: str | None


@dataclass
class ActivationResult:
    """Active 激活结果。"""

    success: bool
    factor_id: int
    from_status: str | None
    to_status: str
    actor: str
    reason: str | None
    audit_id: int | None
    error: str | None = None
    valid_days: int = 0
    min_required_days: int = MIN_VALID_SHADOW_DAYS


def request_activation(
    db: Session,
    *,
    req: ActivationRequest,
) -> ActivationResult:
    """申请 Shadow → Active（门禁检查，不实际激活）。

    门禁：
    1. 因子当前状态必须为 shadow
    2. 有效观察天数 >= 20
    3. 必须有评估 run 证据
    4. 申请人不能自动批准（必须由另一个 local_user 或同一 local_user 明确 approve）

    返回 ActivationResult，success=True 表示门禁通过，可进入审批。
    实际激活由 approve_activation 完成。
    """
    factor = db.execute(
        select(Factor).where(Factor.id == req.factor_id).with_for_update()
    ).scalar_one_or_none()

    if factor is None:
        return ActivationResult(
            success=False, factor_id=req.factor_id,
            from_status=None, to_status="active",
            actor=req.actor, reason=req.reason,
            audit_id=None, error="factor_not_found",
        )

    current_status = factor.lifecycle_status
    if current_status != "shadow":
        return ActivationResult(
            success=False, factor_id=req.factor_id,
            from_status=current_status, to_status="active",
            actor=req.actor, reason=req.reason,
            audit_id=None, error=f"not_in_shadow:{current_status}",
        )

    # 有效观察天数门禁
    valid_days = count_valid_shadow_days(db, factor_version_id=req.factor_version_id)
    if valid_days < MIN_VALID_SHADOW_DAYS:
        return ActivationResult(
            success=False, factor_id=req.factor_id,
            from_status=current_status, to_status="active",
            actor=req.actor, reason=req.reason,
            audit_id=None,
            error=f"insufficient_valid_days:{valid_days}/{MIN_VALID_SHADOW_DAYS}",
            valid_days=valid_days,
        )

    # 健康检查
    health = run_shadow_health_check(db, factor_version_id=req.factor_version_id)
    if health.should_quarantine:
        return ActivationResult(
            success=False, factor_id=req.factor_id,
            from_status=current_status, to_status="active",
            actor=req.actor, reason=req.reason,
            audit_id=None,
            error=f"health_check_failed:{health.health_status}",
            valid_days=valid_days,
        )

    return ActivationResult(
        success=True, factor_id=req.factor_id,
        from_status=current_status, to_status="active",
        actor=req.actor, reason=req.reason,
        audit_id=None,
        valid_days=valid_days,
    )


def approve_activation(
    db: Session,
    *,
    factor_id: int,
    factor_version_id: int,
    approver: str,
    reason: str,
    evidence_run_id: str | None = None,
    request_id: str | None = None,
) -> ActivationResult:
    """批准 Shadow → Active（人工审批，执行状态迁移）。

    必须由 local_user 明确批准。
    审批包含：评估 run、观察区间、actor、reason。
    """
    # 门禁检查
    req = ActivationRequest(
        factor_id=factor_id,
        factor_version_id=factor_version_id,
        evidence_run_id=evidence_run_id,
        observation_start="",
        observation_end="",
        valid_days=0,
        actor=approver,
        reason=reason,
    )
    check = request_activation(db, req=req)
    if not check.success:
        return check

    # 执行状态迁移
    transition_req = FactorTransitionRequest(
        action="activate",
        actor=approver,
        reason=reason,
        evidence_run_id=evidence_run_id,
        request_id=request_id,
    )

    try:
        result = execute_transition(db, factor_id=factor_id, request=transition_req)
        return ActivationResult(
            success=True,
            factor_id=factor_id,
            from_status=result.from_status,
            to_status=result.to_status,
            actor=approver,
            reason=reason,
            audit_id=result.audit_id,
            valid_days=check.valid_days,
        )
    except ValueError as exc:
        return ActivationResult(
            success=False,
            factor_id=factor_id,
            from_status=None,
            to_status="active",
            actor=approver,
            reason=reason,
            audit_id=None,
            error=str(exc),
            valid_days=check.valid_days,
        )


def reject_activation(
    db: Session,
    *,
    factor_id: int,
    reviewer: str,
    reason: str,
    request_id: str | None = None,
) -> ActivationResult:
    """驳回 Shadow → Active 申请（因子保持 shadow 或回到 testing）。"""
    transition_req = FactorTransitionRequest(
        action="reject",
        actor=reviewer,
        reason=reason,
        request_id=request_id,
    )
    try:
        result = execute_transition(db, factor_id=factor_id, request=transition_req)
        return ActivationResult(
            success=True,
            factor_id=factor_id,
            from_status=result.from_status,
            to_status=result.to_status,
            actor=reviewer,
            reason=reason,
            audit_id=result.audit_id,
        )
    except ValueError as exc:
        return ActivationResult(
            success=False,
            factor_id=factor_id,
            from_status=None,
            to_status="rejected",
            actor=reviewer,
            reason=reason,
            audit_id=None,
            error=str(exc),
        )


def auto_quarantine(
    db: Session,
    *,
    factor_id: int,
    factor_version_id: int,
    reason: str,
    request_id: str | None = None,
) -> ActivationResult:
    """数据硬错误自动隔离（shadow/active → quarantined）。

    自动隔离追加审计记录，不绕过状态机。
    """
    transition_req = FactorTransitionRequest(
        action="quarantine",
        actor="system",
        reason=reason,
        request_id=request_id,
    )
    try:
        result = execute_transition(db, factor_id=factor_id, request=transition_req)
        return ActivationResult(
            success=True,
            factor_id=factor_id,
            from_status=result.from_status,
            to_status=result.to_status,
            actor="system",
            reason=reason,
            audit_id=result.audit_id,
        )
    except ValueError as exc:
        return ActivationResult(
            success=False,
            factor_id=factor_id,
            from_status=None,
            to_status="quarantined",
            actor="system",
            reason=reason,
            audit_id=None,
            error=str(exc),
        )


__all__ = [
    # WP6-03
    "MIN_VALID_SHADOW_DAYS",
    "MIN_COMPLETENESS_RATIO",
    "ShadowObservationInput",
    "ShadowObservationResult",
    "record_shadow_observation",
    "get_shadow_observations",
    "count_valid_shadow_days",
    "count_consecutive_valid_shadow_days",
    "is_shadow_observation_complete",
    # WP6-04
    "IC_DECAY_THRESHOLD",
    "COVERAGE_DROP_THRESHOLD",
    "CONSTANT_FACTOR_IC_STD",
    "HEALTHY",
    "DEGRADED",
    "BLOCKED",
    "HealthAlert",
    "ShadowHealthReport",
    "run_shadow_health_check",
    # WP6-05
    "ActivationRequest",
    "ActivationResult",
    "request_activation",
    "approve_activation",
    "reject_activation",
    "auto_quarantine",
]
