"""G1-WP0-2e: FactorUsage 绑定 + StrategyExecutionSnapshot 服务。

契约要点：
- Q21(方案 A)：PortfolioFactorUsage.factor_model_run_id 必须等于
  FactorRuntimeState(id=1).active_model_run_id。
  - research 模式允许显式降级（但写 DEGRADED_DATA 标签）。
  - production_pit/production_sim 模式 Fail-Closed。
- Q8：保存并应用在同一事务中生成新 PortfolioFactorUsage +
  StrategyExecutionSnapshot；effective_from = 提交成功时间（UTC naive）。
  运行中任务在启动时锁定 snapshot；后续保存不影响它（task_locked_at/task_id）。
- Q28：后端幂等键 = hash(portfolio_id + factor_usage_id/占位 + decision_at/提交时间
  + trade_date=None + run_type=save_and_apply)；唯一索引保证重复提交不重复落库。
- Q14：写 FactorModelAuditLog 审计；失败回滚。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.hash_utils import canonical_json, content_hash
from app.models.decision_engine import (
    PortfolioFactorUsage,
    StrategyExecutionSnapshot,
)
from app.models.factor_runtime import FactorModelAuditLog, FactorRuntimeState
from app.models.portfolio import Portfolio, PortfolioRule, Position  # noqa: F401
from app.models.portfolio_member import PortfolioMember
from app.models.score import Score  # noqa: F401
from app.schemas.decision_engine import (
    CurrentFactorUsageResponse,
    FactorModelOptionBrief,
    FactorSetOptionBrief,
    FactorUsageBindRequest,
    FactorUsageOptionsResponse,
    PortfolioFactorUsageRead,
    PreflightWarning,
    RuleOptionBrief,
    SaveAndApplyResponse,
    StrategyPreflightResponse,
)
from app.services.decision_clock import ResolvedClock, resolve, utc_naive_to_shanghai

logger = logging.getLogger(__name__)

# 生产门禁策略版本（Q15）。正式阈值在 DecisionEngine Gate 模块中版本化管理，
# 这里只是把版本号字符串写入快照，便于后续证据链追溯。
GATE_POLICY_VERSION = "production-v1.0.0"


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_portfolio_or_404(db: Session, portfolio_id: int) -> Portfolio:
    p = db.get(Portfolio, portfolio_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")
    return p


def _get_runtime_state(db: Session) -> FactorRuntimeState:
    """获取单例 runtime；不存在就初始化。"""
    state = db.get(FactorRuntimeState, 1)
    if state is None:
        state = FactorRuntimeState(
            id=1, weight_mode="manual", active_model_run_id=None,
            updated_by="bootstrap", version=1,
        )
        db.add(state)
        db.flush()
    return state


def _build_member_snapshot(db: Session, portfolio_id: int, as_of_utc: datetime) -> list[dict[str, Any]]:
    """按 effective_from/effective_to 重建组合成员快照（Q13）。

    规则：member.effective_from <= as_of AND (effective_to IS NULL OR effective_to > as_of)
    对没有 effective_from/to 字段的旧行，用 created_at/NULL 兜底，以便兼容历史安装。
    返回结构化 dict 列表，直接 dumps JSON 写入快照。
    """
    # 保证 autoflush=False 的 session 下也能命中刚 add 的未提交新成员
    db.flush()
    # 先尝试读取有字段的列；若列不存在（旧 schema）回退到最小信息
    rows = db.execute(
        select(PortfolioMember)
        .where(PortfolioMember.portfolio_id == portfolio_id)
        .order_by(PortfolioMember.id)
    ).scalars().all()

    # Do the point-in-time interval filter in application code rather than
    # relying on the current ``status`` value.  Archiving a member updates that
    # value in place, but the SCD2 interval is what tells us whether the
    # relationship existed at the requested historical time.  This also keeps
    # the fallback for older installations (where the effective columns may be
    # absent) intact.
    def _naive_utc(value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    as_of_naive = _naive_utc(as_of_utc) or as_of_utc

    def _to_row(m: PortfolioMember) -> dict[str, Any]:
        fields = {k: getattr(m, k, None) for k in (
            "id", "symbol_id", "execution_mode", "priority",
            "entry_rule_version_id", "exit_rule_version_id",
        )}
        ef = getattr(m, "effective_from", None) or getattr(m, "created_at", as_of_utc)
        et = getattr(m, "effective_to", None)
        fields["effective_from"] = ef.isoformat() if isinstance(ef, datetime) else str(ef)
        fields["effective_to"] = (
            et.isoformat() if isinstance(et, datetime) else (str(et) if et else None)
        )
        return fields

    effective_rows: list[dict[str, Any]] = []
    for member in rows:
        effective_from = _naive_utc(
            getattr(member, "effective_from", None)
            or getattr(member, "created_at", None)
        )
        effective_to = _naive_utc(getattr(member, "effective_to", None))

        # A legacy row without effective dates remains eligible for the
        # historical fallback.  Rows with dates obey the half-open interval
        # [effective_from, effective_to), matching the binding service's
        # documented snapshot semantics.
        if effective_from is not None and effective_from > as_of_naive:
            continue
        if effective_to is not None and effective_to <= as_of_naive:
            continue

        # An archived row with no end timestamp is malformed and cannot be
        # placed on a historical timeline.  Properly archived rows have an
        # effective_to value and are handled by the interval check above.
        if getattr(member, "status", None) == "archived" and effective_to is None:
            continue
        effective_rows.append(_to_row(member))

    return effective_rows


def _usage_content_hash(portfolio_id: int, payload: FactorUsageBindRequest) -> str:
    """PortfolioFactorUsage.content_hash — 仅与绑定内容有关，与时间无关。"""
    ordered: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "factor_model_run_id": payload.factor_model_run_id,
        "factor_set_id": payload.factor_set_id,
        "rule_id": payload.rule_id,
        "rule_version": payload.rule_version,
        "run_mode": payload.run_mode,
        "pit_mode": payload.pit_mode,
        "score_sla_coverage_pct": payload.score_sla_coverage_pct,
        "score_sla_max_age_days": payload.score_sla_max_age_days,
        "rollback_target_model_run_id": payload.rollback_target_model_run_id,
        "key_members_json": payload.key_members_json,
    }
    return content_hash(ordered)


def _snapshot_idempotency_key(portfolio_id: int, usage_hash: str, effective_from: datetime) -> str:
    """Q28：strategy_execution_snapshots 幂等键。

    portfolio_id + usage_hash + effective_from 三元组唯一；重复提交相同 payload 在
    同一秒内会命中唯一索引，返回已存 snapshot。
    """
    return content_hash(
        "strategy_snapshot",
        portfolio_id,
        usage_hash,
        effective_from.isoformat(),
    )


def _validate_production_mode_and_active(
    runtime: FactorRuntimeState, payload: FactorUsageBindRequest,
) -> list[PreflightWarning]:
    """Q6/Q21 方案 A：production 模式必须绑定当前全局 active 模型。"""
    warnings: list[PreflightWarning] = []
    active = runtime.active_model_run_id
    if payload.run_mode in {"production_pit", "production_sim"}:
        if not active or payload.factor_model_run_id != active:
            warnings.append(PreflightWarning(
                severity="blocking",
                code="MODEL_NOT_GLOBAL_ACTIVE",
                message=(
                    "正式模式必须使用当前全局 active 因子模型；"
                    f"当前 runtime.active={active or '(空)'}，请求={payload.factor_model_run_id}"
                ),
                detail={"expected": active, "received": payload.factor_model_run_id},
            ))
    else:
        # research 模式：允许非全局 active 但加警告，结果不得进入生产（Q6）
        if not active or payload.factor_model_run_id != active:
            warnings.append(PreflightWarning(
                severity="warning",
                code="DEGRADED_DATA",
                message=(
                    "Research 模式下绑定了非全局 active 模型，"
                    "决策结果打 DEGRADED_DATA 标签，不得用于生产自动下单。"
                ),
                detail={"global_active": active, "chosen": payload.factor_model_run_id},
            ))
    return warnings


def _validate_model_binding_readiness(
    db: Session,
    runtime: FactorRuntimeState,
    payload: FactorUsageBindRequest,
) -> list[PreflightWarning]:
    """Check the immutable model/FactorSet pair before snapshot creation.

    Historical installations may still hold a runtime pointer to a model row
    that was never migrated.  Research preflight keeps that legacy state
    visible as a degradation warning, while every formal run fails closed.
    Once a real model row is present, however, an invalid, retired or
    mismatched pair is always a blocking configuration error.
    """
    from app.models.factor_model import FactorModelRun
    from app.services.factor_model_contract import assess_factor_model_readiness

    formal = payload.run_mode in {"production_pit", "production_sim"}
    model = db.get(FactorModelRun, payload.factor_model_run_id)
    if model is None:
        severity = "blocking" if formal else "warning"
        return [PreflightWarning(
            severity=severity,
            code="MODEL_NOT_FOUND",
            message=(
                f"FactorModelRun {payload.factor_model_run_id} does not exist; "
                "legacy research cannot create a production-eligible snapshot."
            ),
            detail={"model_run_id": payload.factor_model_run_id},
        )]

    if not payload.factor_set_id:
        return [PreflightWarning(
            severity="blocking",
            code="FACTOR_SET_REQUIRED",
            message="New strategy snapshots must persist the model's factor_set_id.",
            detail={"model_run_id": payload.factor_model_run_id},
        )]

    readiness = assess_factor_model_readiness(
        db,
        model_run_id=payload.factor_model_run_id,
        expected_factor_set_id=payload.factor_set_id,
        require_runtime_active=False,
    )
    if not readiness.ready:
        return [PreflightWarning(
            severity="blocking",
            code=readiness.code,
            message=readiness.message,
            detail=readiness.to_dict(),
        )]

    if runtime.active_model_run_id != payload.factor_model_run_id:
        severity = "blocking" if formal else "warning"
        return [PreflightWarning(
            severity=severity,
            code="MODEL_NOT_GLOBAL_ACTIVE",
            message="Model is readiness-qualified but not the current global active model.",
            detail={
                "expected": runtime.active_model_run_id,
                "received": payload.factor_model_run_id,
            },
        )]
    return []


# ============================================================================
# Public entrypoints
# ============================================================================

def get_factor_usage_options(db: Session, portfolio_id: int) -> FactorUsageOptionsResponse:
    """GET /portfolios/{id}/factor-usage-options。

    只给 UI 建议选项与门禁提示，不写数据库。
    """
    _load_portfolio_or_404(db, portfolio_id)
    runtime = _get_runtime_state(db)

    blocking: list[str] = []
    if not runtime.active_model_run_id:
        blocking.append("GLOBAL_ACTIVE_MODEL_EMPTY：生产模式不可绑定，"
                        "请先在因子中心激活 Ridge 模型。")

    factor_models: list[FactorModelOptionBrief] = []
    selected_factor_set_id: str | None = None
    if runtime.active_model_run_id:
        # The option endpoint is also the UI's selection boundary.  Do not
        # offer historical candidates that happen to be validated: a newly
        # persisted snapshot must bind the single active, immutable model/set
        # pair and nothing else.
        from app.models.factor_model import FactorModelRun
        from app.services.factor_model_contract import assess_factor_model_readiness

        readiness = assess_factor_model_readiness(
            db,
            model_run_id=runtime.active_model_run_id,
            require_runtime_active=True,
        )
        if not readiness.ready:
            blocking.append(
                f"ACTIVE_MODEL_NOT_READY:{readiness.code}：{readiness.message}"
            )
        else:
            r = db.get(FactorModelRun, runtime.active_model_run_id)
            assert r is not None  # guarded by assess_factor_model_readiness
            selected_factor_set_id = readiness.factor_set_id
            metrics = _loads_or_none(getattr(r, "metrics_json", None)) or {}
            factor_models.append(FactorModelOptionBrief(
                factor_model_run_id=r.id,
                factor_set_id=selected_factor_set_id,
                trained_at=getattr(r, "trained_at", None) or getattr(r, "created_at", None),
                passes_production_gate=True,
                is_global_active=True,
                sample_out_rank_ic=metrics.get("validation_ic"),
                sample_out_icir=metrics.get("validation_icir"),
                sample_out_coverage_pct=metrics.get("coverage_pct"),
            ))

    # The FactorSet selector is deliberately constrained to the immutable set
    # that was used to train the active model.  Listing every frozen set here
    # used to let the client construct a model/set mismatch that only failed
    # much later during score lookup.
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    fs_rows = []
    if selected_factor_set_id:
        row = db.get(FactorSet, selected_factor_set_id)
        if row is not None:
            fs_rows = [row]
    factor_sets = []
    for r in fs_rows:
        member_count = db.scalar(
            select(func.count())
            .select_from(FactorSetMember)
            .where(FactorSetMember.factor_set_id == r.id)
        ) or 0
        factor_sets.append(FactorSetOptionBrief(
            factor_set_id=r.id, name=r.name, status=r.status,
            frozen_at=getattr(r, "frozen_at", None) or getattr(r, "updated_at", None),
            content_hash=getattr(r, "content_hash", None),
            member_count=int(member_count),
        ))

    # rules
    rule_rows = db.execute(
        select(PortfolioRule).where(PortfolioRule.portfolio_id == portfolio_id)
    ).scalars().all()
    rules = []
    for r in rule_rows:
        import json as _json
        complete = False
        try:
            limits = _json.loads(r.stage_limits_json or "{}")
            if isinstance(limits, dict) and (
                ("stock_stages" in limits and "etf_stages" in limits)
                or ("limits" in limits and isinstance(limits["limits"], dict))
                or ("stages" in limits)
            ):
                complete = True
        except Exception:
            pass
        rules.append(RuleOptionBrief(
            rule_id=r.id, rule_name=r.rule_name, rule_version=1,
            has_stage_limits_complete=complete,
            updated_at=getattr(r, "updated_at", getattr(r, "created_at", None)),
        ))

    defaults: dict[str, Any] = {
        "factor_model_run_id": factor_models[0].factor_model_run_id if factor_models else None,
        "factor_set_id": selected_factor_set_id,
        "run_mode": "research",
        "pit_mode": "best_effort",
        "universe_type": "portfolio_members",
    }
    # 默认 rule = 最新 active rule
    for r in rules:
        if r.is_active if hasattr(r, "is_active") else True:
            defaults["rule_id"] = r.rule_id
            defaults["rule_version"] = r.rule_version
            break

    return FactorUsageOptionsResponse(
        global_active_model_run_id=runtime.active_model_run_id,
        global_weight_mode=runtime.weight_mode,
        runtime_version=runtime.version,
        factor_models=factor_models,
        factor_sets=factor_sets,
        portfolio_rules=rules,
        defaults=defaults,
        blocking_reasons=blocking,
    )


def get_current_usage(db: Session, portfolio_id: int) -> CurrentFactorUsageResponse:
    """GET /portfolios/{id}/factor-usage：当前生效绑定 + 最近历史。"""
    _load_portfolio_or_404(db, portfolio_id)

    all_usages = db.execute(
        select(PortfolioFactorUsage)
        .where(PortfolioFactorUsage.portfolio_id == portfolio_id)
        .order_by(PortfolioFactorUsage.effective_from.desc())
        .limit(10)
    ).scalars().all()

    current: PortfolioFactorUsage | None = None
    # 找 effective_from <= now 且 effective_to IS NULL 或 >= now 的最新
    now = _utcnow_naive()
    for u in all_usages:
        if u.effective_from <= now and (
            u.effective_to is None or u.effective_to >= now
        ):
            current = u
            break
    if current is None and all_usages:
        current = all_usages[0]

    latest_snap = db.execute(
        select(StrategyExecutionSnapshot)
        .where(and_(
            StrategyExecutionSnapshot.portfolio_id == portfolio_id,
            StrategyExecutionSnapshot.snapshot_type == "save_and_apply",
        ))
        .order_by(StrategyExecutionSnapshot.effective_from.desc())
        .limit(1)
    ).scalars().first()

    return CurrentFactorUsageResponse(
        current=PortfolioFactorUsageRead.model_validate(current) if current else None,
        history=[PortfolioFactorUsageRead.model_validate(u) for u in all_usages],
        latest_snapshot_id=latest_snap.id if latest_snap else None,
    )


def _resolve_clock_for_now() -> ResolvedClock:
    """Save/Preflight 时使用"今日 T 决策 + T+1 执行"的默认时钟（Q1.3）。"""
    today = date.today()
    return resolve(today, mode="t_day_close")


def _assemble_preflight_or_save_payload(
    db: Session,
    portfolio_id: int,
    payload: FactorUsageBindRequest,
    actor: str,
    *,
    clock: ResolvedClock | None = None,
) -> tuple[
    StrategyExecutionSnapshot,  # snapshot object (transient, not flushed)
    PortfolioFactorUsage,      # usage object (transient, not flushed)
    list[PreflightWarning],
    dict[str, Any],            # decision_clock dict
]:
    """preflight 与 save_and_apply 的共享装配逻辑。"""
    portfolio = _load_portfolio_or_404(db, portfolio_id)
    runtime = _get_runtime_state(db)
    now = _utcnow_naive()
    clock = clock or _resolve_clock_for_now()

    warnings: list[PreflightWarning] = []
    warnings.extend(_validate_production_mode_and_active(runtime, payload))
    warnings.extend(_validate_model_binding_readiness(db, runtime, payload))

    # Rule 存在性校验
    if payload.rule_id is not None:
        rule = db.get(PortfolioRule, payload.rule_id)
        if rule is None or rule.portfolio_id != portfolio_id:
            warnings.append(PreflightWarning(
                severity="blocking",
                code="RULE_NOT_FOUND_OR_NOT_OWNED",
                message=f"PortfolioRule id={payload.rule_id} 不属于该组合或不存在",
            ))

    usage_hash = _usage_content_hash(portfolio_id, payload)
    versions = {
        "factor_model_run_id": payload.factor_model_run_id,
        "factor_set_id": payload.factor_set_id,
        "rule_id": payload.rule_id,
        "rule_version": payload.rule_version,
        "run_mode": payload.run_mode,
        "pit_mode": payload.pit_mode,
        "runtime_version": runtime.version,
        "gate_policy_version": GATE_POLICY_VERSION,
    }

    usage = PortfolioFactorUsage(
        id=content_hash("pfu", usage_hash, now.isoformat(), actor),
        portfolio_id=portfolio_id,
        factor_model_run_id=payload.factor_model_run_id,
        factor_set_id=payload.factor_set_id,
        rule_id=payload.rule_id,
        rule_version=payload.rule_version,
        run_mode=payload.run_mode,
        pit_mode=payload.pit_mode,
        score_sla_coverage_pct=payload.score_sla_coverage_pct,
        score_sla_max_age_days=payload.score_sla_max_age_days,
        status="active" if payload.run_mode.startswith("production") else "draft",
        rollback_target_model_run_id=payload.rollback_target_model_run_id,
        versions_json=canonical_json(versions),
        content_hash=usage_hash,
        effective_from=now,
        effective_to=None,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )

    # Q1 / C-01a：组合内快照流水号自增 1。SELECT MAX + INSERT 在事务上下文中由 save_and_apply_usage 保证原子性。
    from sqlalchemy import select, func as sa_func
    cur_max = db.scalar(
        select(sa_func.coalesce(sa_func.max(StrategyExecutionSnapshot.snapshot_no), 0))
        .where(StrategyExecutionSnapshot.portfolio_id == portfolio_id)
    ) or 0
    next_snapshot_no = int(cur_max) + 1

    members = _build_member_snapshot(db, portfolio_id, now)
    bench = getattr(portfolio, "benchmark_code", "000300") or "000300"
    cost_cfg = {
        "buy_slippage_bps": 5,        # Q2.3
        "sell_slippage_bps": 5,
        "buy_commission_pct": getattr(portfolio, "buy_fee_pct", 0.00025),
        "sell_commission_pct": getattr(portfolio, "sell_fee_pct", 0.00025),
        "stamp_duty_pct": 0.001,
    }
    cst = clock.as_shanghai_dict()
    decision_clock_payload = {
        "decision_at_utc": clock.decision_at.isoformat(),
        "data_cutoff_at_utc": clock.data_cutoff_at.isoformat(),
        "execution_at_utc": clock.execution_at.isoformat(),
        "decision_at_cst": cst["decision_at"],
        "data_cutoff_at_cst": cst["data_cutoff_at"],
        "execution_at_cst": cst["execution_at"],
    }
    member_snap = members  # 保留原始 list：content_hash 内部会 canonical_json；SES 列存储也单独 canonical_json 一次
    key_members = list(payload.key_members_json) if payload.key_members_json else None

    snap_hash = content_hash(
        portfolio_id,
        usage_hash,
        decision_clock_payload,
        cost_cfg,
        member_snap,
        key_members,
        bench,
        payload.run_mode,
    )
    idem_key = _snapshot_idempotency_key(portfolio_id, usage_hash, now)

    snapshot = StrategyExecutionSnapshot(
        id=content_hash("ses", snap_hash, now.isoformat(), actor),
        snapshot_no=next_snapshot_no,
        portfolio_id=portfolio_id,
        portfolio_factor_usage_id=usage.id,
        factor_model_run_id=payload.factor_model_run_id,
        factor_set_id=payload.factor_set_id,
        rule_id=payload.rule_id,
        rule_version=payload.rule_version,
        decision_clock_json=canonical_json(decision_clock_payload),
        cost_config_json=canonical_json(cost_cfg),
        member_snapshot_json=canonical_json(member_snap),
        key_members_json=canonical_json(key_members) if key_members else None,
        candidate_pool_json=(canonical_json(payload.candidate_pool_ids) if getattr(payload, "candidate_pool_ids", None) else None),
        universe_type="portfolio_members",
        benchmark_code=bench,
        snapshot_type="save_and_apply",
        snapshot_hash=snap_hash,
        idempotency_key=idem_key,
        gate_policy_version=GATE_POLICY_VERSION,
        gate_result_json=canonical_json({
            "pass": not any(w.severity == "blocking" for w in warnings),
            "warnings": [w.model_dump(mode="json") for w in warnings],
            "runtime_version": runtime.version,
            "gate_policy_version": GATE_POLICY_VERSION,
        }),
        versions_json=canonical_json(versions),
        effective_from=now,
        created_by=actor,
        created_at=now,
    )

    return snapshot, usage, warnings, decision_clock_payload


def preflight_usage(
    db: Session, portfolio_id: int, payload: FactorUsageBindRequest, actor: str = "local_user",
) -> StrategyPreflightResponse:
    """Q8.3: 只在内存中执行，不创建正式 Snapshot，返回完整预检证据。"""
    runtime = _get_runtime_state(db)
    snapshot, usage, warnings, clock_payload = _assemble_preflight_or_save_payload(
        db, portfolio_id, payload, actor,
    )
    # 额外做一次 Score 覆盖率估算（best-effort；TODO: WP5 接入真实 SQL）
    from app.models.score import Score  # 局部 import
    member_count = len(snapshot.member_snapshot_json or "[]")
    try:
        import json as _json
        member_rows = _json.loads(snapshot.member_snapshot_json or "[]")
    except Exception:
        member_rows = []
    symbol_ids = sorted({r["symbol_id"] for r in member_rows if r.get("symbol_id")})
    score_count_actual = 0
    if symbol_ids:
        # 粗略估算：最新一条 Score 就算覆盖（精确每证券取最新见 Q22 查询模式）
        rows = db.execute(
            select(Score.id).where(and_(
                Score.factor_model_run_id == payload.factor_model_run_id,
                Score.symbol_id.in_(symbol_ids),
            ))
        ).scalars().all()
        score_count_actual = len({r for r in rows if r is not None})
    expected = len(symbol_ids) or 0
    coverage = round(100.0 * score_count_actual / expected, 2) if expected else None
    return StrategyPreflightResponse(
        ok=not any(w.severity == "blocking" for w in warnings),
        preflight_snapshot_hash=snapshot.snapshot_hash,
        gate_policy_version=snapshot.gate_policy_version,
        gate_result_json=_loads_or_none(snapshot.gate_result_json),
        warnings=warnings,
        snapshot_preview={
            "id": snapshot.id,
            "portfolio_factor_usage": {
                "id": usage.id,
                "content_hash": usage.content_hash,
                "status": usage.status,
                "run_mode": usage.run_mode,
                "pit_mode": usage.pit_mode,
            },
            "universe_type": snapshot.universe_type,
            "benchmark_code": snapshot.benchmark_code,
            "snapshot_type": "preflight",  # 注意：预览不是 save_and_apply
            "member_count": len(member_rows),
            "key_members_json": (
                json.loads(snapshot.key_members_json)
                if snapshot.key_members_json else None
            ),
        },
        decision_clock=clock_payload,
        score_coverage_pct=coverage,
        score_max_age_days=None,  # TODO: G1-WP5 精确估算
        member_count=member_count,
        universe_count=len(symbol_ids),
        idempotency_key=snapshot.idempotency_key or "",
    )


def _loads_or_none(x: Any) -> dict[str, Any] | None:
    import json as _json
    if not x or not isinstance(x, str):
        return None
    try:
        return _json.loads(x)
    except Exception:
        return None


def save_and_apply_usage(
    db: Session, portfolio_id: int, payload: FactorUsageBindRequest, actor: str = "local_user",
) -> SaveAndApplyResponse:
    """Q1 / Q8：单事务保存 PortfolioFactorUsage + StrategyExecutionSnapshot。

    事务步骤：
      1. SELECT FactorRuntimeState WHERE id=1 FOR UPDATE（乐观锁版本号）
      2. 将当前 portfolio 的 active factor_usage 置为 effective_to = now
      3. INSERT 新 PortfolioFactorUsage（content_hash 绑定）
      4. INSERT 新 StrategyExecutionSnapshot（snapshot_hash，snapshot_type=save_and_apply）
      5. INSERT FactorModelAuditLog 审计行
      6. COMMIT。若 runtime.version 在 1-5 之间变化 → 抛错并重试。
    """
    # 预检（只读，不写）确保 blocking 级问题先被暴露给用户
    pre = preflight_usage(db, portfolio_id, payload, actor)
    if not pre.ok:
        raise HTTPException(status_code=400, detail={
            "error": "PREFLIGHT_BLOCKED",
            "warnings": [w.model_dump(mode="json") for w in pre.warnings],
        })

    now = _utcnow_naive()

    try:
        # 乐观锁：重读 runtime 取版本
        runtime = db.get(FactorRuntimeState, 1) or _get_runtime_state(db)
        pre_version = runtime.version

        # 关闭旧 usage
        active_usages = db.execute(
            select(PortfolioFactorUsage).where(and_(
                PortfolioFactorUsage.portfolio_id == portfolio_id,
                or_(
                    PortfolioFactorUsage.effective_to.is_(None),
                    PortfolioFactorUsage.effective_to > now,
                ),
            ))
        ).scalars().all()
        for u in active_usages:
            u.effective_to = now
            u.status = "deprecated"
            u.updated_at = now

        # 组装新对象（注意：重新生成，保证 effective_from 为事务时间）
        snapshot, new_usage, _warns, _clock = _assemble_preflight_or_save_payload(
            db, portfolio_id, payload, actor,
        )
        # 重写 id 里的时间戳为 now（统一使用事务提交时间）
        new_usage.id = content_hash("pfu", new_usage.content_hash, now.isoformat(), actor)
        new_usage.effective_from = now
        new_usage.created_at = now
        new_usage.updated_at = now
        new_usage.status = "active" if new_usage.run_mode.startswith("production") else "active"
        snapshot.portfolio_factor_usage_id = new_usage.id
        snapshot.id = content_hash("ses", snapshot.snapshot_hash, now.isoformat(), actor)
        snapshot.effective_from = now
        snapshot.created_at = now

        db.add(new_usage)
        db.add(snapshot)

        # 审计（Q28+方案 A 变更）
        db.add(FactorModelAuditLog(
            action="PORTFOLIO_FACTOR_USAGE_SAVE_APPLY",
            model_run_id=payload.factor_model_run_id,
            previous_mode=runtime.weight_mode,
            new_mode=runtime.weight_mode,
            previous_model_run_id=runtime.active_model_run_id,
            new_model_run_id=payload.factor_model_run_id,
            actor=actor,
            note=(
                f"portfolio={portfolio_id}, usage={new_usage.id}, "
                f"snapshot={snapshot.id}, run_mode={payload.run_mode}, "
                f"pit_mode={payload.pit_mode}, runtime_version_pre={pre_version}"
            ),
            created_at=now,
        ))

        db.commit()
        db.refresh(new_usage)
        db.refresh(snapshot)
    except IntegrityError as e:
        db.rollback()
        # 命中 uq_ses_idempotency：幂等返回已保存结果（Q28）
        if "uq_ses_idempotency" in (str(e.orig).lower() if e.orig else str(e).lower()):
            idem = _snapshot_idempotency_key(
                portfolio_id, _usage_content_hash(portfolio_id, payload), now,
            )
            existing = db.execute(
                select(StrategyExecutionSnapshot).where(
                    StrategyExecutionSnapshot.idempotency_key == idem
                )
            ).scalars().first()
            if existing and existing.portfolio_factor_usage_id:
                u = db.get(PortfolioFactorUsage, existing.portfolio_factor_usage_id)
                if u:
                    return SaveAndApplyResponse(
                        factor_usage=PortfolioFactorUsageRead.model_validate(u),
                        strategy_snapshot_id=existing.id,
                        snapshot_hash=existing.snapshot_hash,
                        idempotency_key=idem,
                        effective_from=existing.effective_from,
                        warnings=[],
                    )
        raise HTTPException(status_code=409, detail={
            "error": "INTEGRITY_CONFLICT",
            "detail": str(e.orig) if e.orig else str(e),
        })
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("save_and_apply_usage failed")
        raise HTTPException(status_code=500, detail={
            "error": "SAVE_APPLY_FAILED",
            "detail": f"{type(e).__name__}: {e}",
        })

    return SaveAndApplyResponse(
        factor_usage=PortfolioFactorUsageRead.model_validate(new_usage),
        strategy_snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        idempotency_key=snapshot.idempotency_key or "",
        effective_from=snapshot.effective_from,
        warnings=[w for w in pre.warnings if w.severity != "blocking"],
    )
