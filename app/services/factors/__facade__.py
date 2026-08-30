"""Factor Domain Public Facade (P0 Anti-Corruption Layer).

All non-factor-domain code MUST call this module (or the HTTP mirror
``scoring_facade.py``) instead of directly importing
``app.services.factors.*`` / ``app.models.factor_*``.

This file intentionally exposes only DTOs (plain Python dicts / dataclasses
with no ORM / DuckDB / SQLAlchemy dependencies leaking out).

The implementation simply delegates to the existing services. Once the
abstraction is stable (P1), callers will not change when internals are
refactored / extracted into a separate process.

The following INBOUND rules apply (factor domain owns its own mutation
surface):
- Mutations on FactorModelRun / FactorSet / FactorDefinition are EXCLUSIVE to
  the factor-domain pages. External modules never create / freeze / train /
  activate models. They only READ via the APIs below.
- ``submit_factor_draft_from_external`` is the ONLY approved path for an
  external module (e.g. custom indicator promote) to inject data INTO the
  factor domain. It returns an opaque audit id and the draft will be
  reviewed through the factor-domain governance flow.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

# ── DTOs ──────────────────────────────────────────────────────────────

ScoreModelStatus = Literal["validated", "rejected", "training", "any"]
ScoreModelScope = Literal["production", "shadow", "validated", "any"]


@dataclass(frozen=True)
class ScoreModelBriefDTO:
    """Opaque summary consumed by strategy / backtest / scoring config UIs."""

    id: str                       # FactorModelRun.id (opaque; use it where you used model_id)
    name: str                     # Human readable label, never None
    status: str                   # validated | rejected etc.
    validation_ic: float | None
    sample_count: int
    data_cutoff_at: str | None
    factorset_id: str | None      # Stable frozen-ref id (FactorSet.id is immutable-safe for display/mapping)
    factorset_label: str | None   # Display name for the factor set
    factorset_member_count: int | None
    created_at: str | None
    backtest_support: bool = False  # P2-3-C: True when the model run carries enough data/metrics to drive backtests directly.
    # P2-3 DTO schema version: first introduced=2; bump 2->3 when added backtest_support field.
    # New contract fields MUST bump this int and sync frontend ScoringModelBrief schema_version drift thresholds.
    schema_version: int = 3


@dataclass(frozen=True)
class ScoreScopeDTO:
    """Active production scoring context used by alerts / candidate / discovery.

    Backward-compat: exposes ``model_run_id`` for existing consumers that were
    written against the raw ``ActiveScoreScope`` returned by the factor-internal
    module. ``active_model_id`` is the preferred P1+ opaque name.
    """

    weight_mode: Literal["manual", "ridge", "shadow"]
    model_ready: bool
    active_model_id: str | None
    fallback_reason: str | None

    @property
    def model_run_id(self) -> str | None:
        """Alias for legacy callers (alerts / candidate_promote / discovery)."""
        return self.active_model_id


@dataclass(frozen=True)
class ScoreRuntimeOverviewDTO:
    """Lightweight overview shown on TodayDecision / dashboards."""

    feature_enabled: bool
    weight_mode: Literal["manual", "ridge", "shadow"]
    active_model_id: str | None
    runtime_version: int
    updated_at: str | None
    warehouse_available: bool
    warehouse_path: str
    latest_trade_date: str | None
    warehouse_error: str | None
    health_status: str               # healthy | degraded | unavailable
    active_factor_coverage_count: int
    active_factor_avg_coverage: float


@dataclass(frozen=True)
class FeatureInputStatusDTO:
    """Returned from ensure_feature_inputs_ready() to the data feed layer."""

    triggered: bool
    task_id: str | None
    eta_seconds: float | None
    reason: str


@dataclass(frozen=True)
class ExternalFactorDraftSubmission:
    """The only certified path for a non-factor-domain module to seed a new
    factor. The factor-domain governance pipeline takes it from there."""

    draft_id: str
    factor_code: str
    review_status: Literal["submitted", "duplicate", "rejected_draft"]
    audit_url: str | None
    message: str


# ── P2-Governance DTOs：5 张治理表 + 准入门禁 ──────────────────────


FactorDraftStatus = Literal[
    "submitted", "approved", "rejected", "applied", "deleted",
]


@dataclass(frozen=True)
class FactorDraftDTO:
    """Factor draft row for governance list UI / review API."""
    id: int
    draft_no: str
    source_module: str
    source_ref_id: str | None
    suggested_code: str
    suggested_name: str | None
    suggested_category: str | None
    payload: dict[str, Any]
    review_status: FactorDraftStatus
    submitted_by: str | None
    reviewer: str | None
    review_msg: str | None
    promoted_factor_id: int | None
    promoted_factor_code: str | None     # P2.2c.1：正式因子 code（用于 UI 跳转到因子库卡）
    promoted_factor_version: int | None
    submitted_at: str | None
    reviewed_at: str | None
    applied_at: str | None
    audit_url: str | None
    # P2-3 DTO schema 版本号（首次引入 = 2）。新增契约字段必须升级并同步前端 ScoringFactorDraft。
    schema_version: int = 2


@dataclass(frozen=True)
class GovernanceGateResultDTO:
    """统一门禁返回结构（2 道训练准入门 + 后续扩展门禁）。"""
    gate_name: str
    passed: bool
    score: float | None                 # 原始可比分数（如覆盖率/IC）
    threshold_min: float | None
    threshold_max: float | None
    reasons: list[str]                  # 失败原因 / 说明
    detail: dict[str, Any]              # 因子级明细 / 调试信息


# ── P1 UI Stitching DTOs: FactorSet / Model detail / Factor usage ────


@dataclass(frozen=True)
class ScoreFactorSetDTO:
    """FactorSet digest shown on model/strategy/factor-hub pages.

    ``id`` is intentionally exposed here because FactorSet IS a stable
    domain reference (consumers need ``id`` to e.g. show "all models on
    this FactorSet"). FactorSet content never changes once frozen, so
    leaking this id is safe (it's effectively an immutable content id).
    """

    id: str
    label: str                         # FactorSet.name (display string)
    status: str                        # draft | frozen | deprecated
    member_count: int                  # Feature members only
    created_at: str | None
    frozen_at: str | None
    is_active_for_model_run_ids: list[str]  # Which (validated) models actively reference this set
    description: str | None
    # P2-3 DTO schema 版本号（首次引入 = 2）。新增契约字段必须升级并同步前端 ScoringFactorSetBrief。
    schema_version: int = 2


@dataclass(frozen=True)
class ScoreModelFactorMemberDTO:
    """One factor inside a model. Rendered directly by the model expand UI."""

    factor_code: str
    factor_name: str | None
    factor_version: int
    coefficient: float                 # Raw Ridge coefficient (before any L1 rescale)
    normalized_weight: float           # Std. weight; abs() == 1.0 across features for 1-Ridge-model display
    train_ic: float | None
    validation_ic: float | None
    coverage: float | None             # 0..1 coverage ratio (best-effort from health snapshot when available)
    side: Literal["long", "short", "neutral"]  # derived from sign(normalized_weight)


@dataclass(frozen=True)
class ScoreModelDetailDTO:
    """The big model-detail payload that makes the factor-model page no longer
    feel "disconnected" from the factor hub. Includes:
        • top-level run info
        • factor set label/id pair
        • per-factor weights + IC with factor name merged from Factor registry
        • parsed metrics / hyperparameters surfaces with stable keys
    """

    id: str
    name: str
    status: str                       # validated / rejected / training …
    model_type: str                   # ridge / …
    weight_mode: Literal["manual", "ridge", "shadow"]
    validation_ic: float | None
    validation_icir: float | None
    validation_r2: float | None
    train_ic: float | None
    sample_count: int
    symbol_count: int
    trade_date_count: int
    train_start_date: str | None
    train_end_date: str | None
    validation_start_date: str | None
    validation_end_date: str | None
    data_cutoff_at: str | None
    created_at: str | None
    activated_at: str | None
    rejection_reason: str | None
    factorset_id: str | None
    factorset_label: str | None
    factorset_member_count: int
    factors: list[ScoreModelFactorMemberDTO]  # sorted by abs(normalized_weight) desc


@dataclass(frozen=True)
class FactorUsageSetDTO:
    """FactorSet membership row for a single factor."""

    factor_set_id: str
    label: str
    status: str                       # draft/frozen/deprecated
    role: str                         # feature / target / regime
    version: int                      # factor_version inside this set


@dataclass(frozen=True)
class FactorUsageModelDTO:
    """Model usage row for a single factor."""

    model_id: str
    model_name: str
    status: str                       # validated / rejected / …
    normalized_weight: float
    validation_ic: float | None
    model_validation_ic: float | None  # Overall model-level IC (contrast with factor-in-model IC)
    activated_at: str | None


@dataclass(frozen=True)
class FactorUsageDTO:
    """The full "factor→where used" digest rendered inside the FactorHub drawer.

    All quality fields are best-effort (filled when factor_health / shadow_obs
    have recent data); if missing the UI just shows "-".
    """

    code: str
    name: str
    status: str                       # Factor.status (active / disabled …)
    lifecycle_status: str | None      # draft / candidate / shadow / active / deprecated …
    origin: str | None
    category: str | None
    is_active: bool
    description: str | None
    active_version: int | None
    coverage_30d: float | None        # 0..1 coverage over last 30 trading days (best-effort)
    ic_mean_30d: float | None         # Best-effort recent IC
    days_in_production: int | None    # Since activation; may be None
    in_factor_sets: list[FactorUsageSetDTO]
    in_models: list[FactorUsageModelDTO]


# ── 1. Scoring models / Strategy & Backtest consumers ────────────────

def list_score_models(
    scope: ScoreModelScope = "any",
    limit: int = 50,
) -> list[ScoreModelBriefDTO]:
    """List validated / shadow-included factor models for strategy
    configuration / backtest dropdowns. The consumer sees DTOs, not ORMs.
    """
    from sqlalchemy import select, desc
    from app.models.factor_model import FactorModelRun
    from app.services.factor_model_contract import model_factor_set_id
    from app.models.factor_evaluation import FactorSet

    status: str | None = None
    if scope == "validated":
        status = "validated"

    db, owned = _open_session()
    try:
        stmt = select(FactorModelRun).order_by(
            desc(FactorModelRun.created_at), desc(FactorModelRun.id)
        )
        if status:
            stmt = stmt.where(FactorModelRun.status == status)
        rows = db.execute(stmt.limit(limit)).scalars().all()

        # Pre-load FactorSet for display label (never expose factor_set_id).
        set_cache: dict[str, tuple[str, int]] = {}

        def _lookup_label_and_count(fsid: str | None) -> tuple[str | None, int | None]:
            if not fsid:
                return None, None
            if fsid in set_cache:
                return set_cache[fsid]
            fs = db.get(FactorSet, fsid)
            result = (
                (fs.name if fs else fsid),
                (getattr(fs, "n_members", None) if fs else None),
            )
            set_cache[fsid] = result
            return result

        items: list[ScoreModelBriefDTO] = []
        for run in rows:
            metrics = _json_object(run.metrics_json)
            fsid = model_factor_set_id(run)
            label, n = _lookup_label_and_count(fsid)
            _vic = metrics.get("validation_ic")
            _dco = run.data_cutoff_at
            items.append(ScoreModelBriefDTO(
                id=run.id,
                name=_model_display_name(run, fsid),
                status=run.status or "unknown",
                validation_ic=_vic,
                sample_count=int(run.sample_count or 0),
                data_cutoff_at=(_dco.isoformat() if _dco else None),
                factorset_id=fsid,
                factorset_label=label,
                factorset_member_count=n,
                created_at=(run.created_at.isoformat() if run.created_at else None),
                backtest_support=bool(_vic is not None and _vic != 0 and _dco and (run.sample_count or 0) >= 100),
            ))
        return items
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


# ── 2. Active scoring scope ──────────────────────────────────────────

def get_active_score_scope(*, db: Any | None = None) -> ScoreScopeDTO:
    """Replacement for factors.score_scope.get_active_score_scope().

    Parameters
    ----------
    db:
        Optional pre-opened SQLAlchemy Session. When provided by the caller
        (e.g. alerts / candidate_promote already inside a request scoped
        session) we reuse it and DO NOT close it. When ``None`` a fresh
        session is opened & closed internally.
    """
    from sqlalchemy.orm import Session as _SASession
    from app.services.factors.score_scope import (
        get_active_score_scope as _inner,
    )

    session, owned = _open_session(db)
    try:
        inner = _inner(session)
        return ScoreScopeDTO(
            weight_mode=_coerce_mode(inner.weight_mode),
            model_ready=bool(inner.model_ready),
            active_model_id=inner.model_run_id,
            fallback_reason=None,
        )
    finally:
        if owned:
            try:
                session.close()
            except Exception:
                pass


def get_active_runtime_with_fallback_reason(*, db: Any | None = None) -> ScoreScopeDTO:
    """Same as above, but also reads fallback_reason from runtime state."""
    from app.services.factors.runtime import get_factor_runtime_snapshot

    session, owned = _open_session(db)
    try:
        snap = get_factor_runtime_snapshot(session)
        mode = _coerce_mode(snap.score_weight_mode)
        ready = mode == "manual" or (mode == "ridge" and bool(snap.active_model_run_id))
        return ScoreScopeDTO(
            weight_mode=mode,
            model_ready=ready,
            active_model_id=snap.active_model_run_id,
            fallback_reason=snap.fallback_reason,
        )
    finally:
        if owned:
            try:
                session.close()
            except Exception:
                pass


def apply_active_score_scope_to_scores_select(
    statement: Any,
    db: Any | None = None,
    *,
    _db: Any | None = None,
) -> Any:
    """Replacement for factors.score_scope.apply_active_score_scope().

    Keeps the exact 2-positional-arg call contract ``(stmt, db)`` used by
    dashboard / custom_indicators callers. When no session is provided a
    fresh one is opened & closed internally. Internally the call is routed
    to the original factor-internal implementation.
    """
    from app.services.factors.score_scope import (
        apply_active_score_scope as _inner,
    )

    session_arg = db if db is not None else _db
    session, owned = _open_session(session_arg)
    try:
        return _inner(statement, session)
    finally:
        if owned:
            try:
                session.close()
            except Exception:
                pass


# ── 3. Runtime overview for dashboards / today decision ──────────────

def get_score_runtime_overview() -> ScoreRuntimeOverviewDTO:
    """Replacement for api.getFactorOverview() on non-factor pages."""
    from app.services.factors.config import get_factor_system_config
    from app.services.factors.health import get_factor_health
    from app.services.factors.runtime import get_factor_runtime_snapshot
    from app.services.factors.store import FactorWarehouse

    db, owned = _open_session()
    try:
        runtime = get_factor_runtime_snapshot(db)
        cfg = get_factor_system_config(db)
        try:
            health = get_factor_health(FactorWarehouse(cfg.warehouse_path))
            reasons = health.reasons or []
            avg_cov = (
                sum(f.coverage for f in health.factors) / len(health.factors)
                if health.factors else 0.0
            )
        except Exception as exc:  # noqa: BLE001 — warehouse truly broken
            health = _SENTINEL_HEALTH
            reasons = [f"warehouse_unavailable: {type(exc).__name__}"]
            avg_cov = 0.0
        mode = _coerce_mode(runtime.score_weight_mode)
        return ScoreRuntimeOverviewDTO(
            feature_enabled=bool(cfg.feature_enabled),
            weight_mode=mode,
            active_model_id=runtime.active_model_run_id if mode == "ridge" else None,
            runtime_version=int(runtime.version or 0),
            updated_at=(
                runtime.updated_at.isoformat() if runtime.updated_at else None
            ),
            warehouse_available=bool(getattr(health, "warehouse_available", False)),
            warehouse_path=cfg.warehouse_path,
            latest_trade_date=getattr(health, "latest_bar_date", None),
            warehouse_error=reasons[0] if reasons else None,
            health_status=str(getattr(health, "status", "unavailable")),
            active_factor_coverage_count=len(getattr(health, "factors", [])),
            active_factor_avg_coverage=round(float(avg_cov or 0.0), 6),
        )
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


# ── 4. Opaque async task interface (for TaskCenter / DataSync pages) ──

def cancel_opaque_async_task(task_id: str) -> dict[str, Any]:
    """Cancel any factor-pipeline task without the caller needing to know
    the internal ``factor_pipeline`` URL or ORM table.
    """
    from app.services.async_tasks import cancel_async_task
    # Uses the existing generic cancel helper. Factor pipeline tasks are
    # already indistinguishable at the AsyncTask level from other tasks.
    return cancel_async_task(task_id).model_dump()


def create_scoring_task(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    full_refresh: bool = False,
    train_model: bool = False,
    materialize_scores: bool = True,
    scope: str = "recent",
    actor: str = "external:data_sync",
    source_hint: str = "external_data_sync",
    factor_set_id: str | None = None,
    window_days: int | None = None,
    validation_days: int | None = None,
    batch_size: int = 50000,
    mirror_only: bool | None = None,
    data_cutoff_date: str | None = None,
) -> dict[str, Any]:
    """P0.3 P0-A1: External modules (ExternalDataSync / etc.) create a
    "scoring task" (factor pipeline) via Public Facade.

    Callers do not need to import FactorPipelineCreate schema or to know
    about the ``/factor-pipeline`` internal route.  Returns the same
    opaque task dict (AsyncTaskRead-compatible) as the internal endpoint.
    """
    from app.services.factors.pipeline_task import create_factor_pipeline_task
    from app.schemas.async_task import FactorPipelineCreate

    resolved_mirror_only = (not bool(materialize_scores)) if mirror_only is None else bool(mirror_only)
    payload = FactorPipelineCreate(
        scope=scope,
        train_model=bool(train_model),
        full_refresh=bool(full_refresh),
        start_date=start_date,
        end_date=end_date,
        data_cutoff_date=data_cutoff_date,
        mirror_only=resolved_mirror_only,
        materialize_scores=bool(materialize_scores),
        batch_size=max(1, int(batch_size or 50000)),
        window_days=(None if window_days is None else int(window_days)),
        validation_days=(None if validation_days is None else int(validation_days)),
        factor_set_id=(factor_set_id if factor_set_id else None),
        actor=actor,
        source_hint=source_hint,
    )
    task = create_factor_pipeline_task(payload)
    return dict(task) if isinstance(task, dict) else {}


def get_scoring_task(task_id: str) -> dict[str, Any] | None:
    """P0.3 P0-A1: Get an opaque scoring (factor-pipeline) task by id.

    Returns None if missing.  Caller sees the same AsyncTaskRead-shaped
    dict as the internal route but never needs ``/factor-pipeline`` URL.
    """
    from app.services.async_tasks import get_async_task
    try:
        task = get_async_task(task_id)
    except Exception:
        return None
    if task is None:
        return None
    try:
        return task.model_dump()
    except Exception:
        return {
            "id": getattr(task, "id", task_id),
            "status": getattr(task, "status", "unknown"),
            "task_type": getattr(task, "task_type", None),
            "percent": getattr(task, "percent", None),
            "stage": getattr(task, "stage", None),
            "message": getattr(task, "message", None),
        }


def ensure_feature_inputs_ready(
    *,
    source_hint: str = "external_data_sync",
    actor: str = "system:data_gateway",
    mirror_only: bool = True,
) -> FeatureInputStatusDTO:
    """For ExternalDataSync / DiscoveryDataPrep: request factor feature
    inputs to be mirrored / computed without those modules knowing about
    ``FactorPipelineCreate`` schemas or pipeline routes.
    """
    from app.services.factors.pipeline_task import (
        create_factor_pipeline_task,
        FactorPipelineBindingError,
        get_pipeline_eta,
    )
    from app.schemas.async_task import FactorPipelineCreate

    payload = FactorPipelineCreate(
        scope="recent",                # safe default — never drop historical on behalf of caller
        train_model=False,
        full_refresh=False,
        batch_size=50000,
        mirror_only=mirror_only,
        actor=actor,
        source_hint=source_hint,
    )
    try:
        task = create_factor_pipeline_task(payload)
        tid = task.get("id") if isinstance(task, dict) else getattr(task, "id", None)
    except FactorPipelineBindingError as exc:
        return FeatureInputStatusDTO(
            triggered=False, task_id=None, eta_seconds=None, reason=str(exc),
        )
    except ValueError as exc:
        return FeatureInputStatusDTO(
            triggered=False, task_id=None, eta_seconds=None, reason=str(exc),
        )
    try:
        eta = get_pipeline_eta(train_model=False, full_refresh=False)
        eta_s = float(eta.get("eta_seconds", 0) or 0) if isinstance(eta, dict) else None
    except Exception:
        eta_s = None
    return FeatureInputStatusDTO(
        triggered=True,
        task_id=tid,
        eta_seconds=eta_s,
        reason="factor_pipeline_task_created",
    )


# ── 5. ONLY inbound write path for external modules ───────────────────

def submit_factor_draft_from_external(
    *,
    source_module: str,
    source_ref_id: int | str,
    payload: dict[str, Any],
    actor: str = "external:untrusted",
) -> ExternalFactorDraftSubmission:
    """Certified path: a non-factor-domain module (e.g. custom-indicators)
    wants to promote something into a factor. Always returns a DTO with a
    governance review status; the external module never gets to write
    directly to factor tables.

    Parameters
    ----------
    source_module:
        e.g. ``"custom_indicators"`` — used for audit / dedupe.
    source_ref_id:
        the upstream id (e.g. indicator_id), used for dedupe + audit url.
    payload:
        free-form data understood by the factor pipeline (usually contains
        factor_code, formula_text, display_name, category, value_type …).
    """
    import hashlib
    import json
    import uuid
    from datetime import datetime, timezone
    from app.services.factors.factor_registry import (
        promote_factor_from_indicator,
    )
    from app.models.factor_governance import FactorDraft as _FactorDraft  # P2-G

    def _utc_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _json_safe(obj: Any) -> str:
        try:
            return json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
        except Exception:
            return "{}"

    # ── P2-G：构造 factor_drafts 草稿对象（无论路径都落地，保持可追溯） ──
    _suggested_code = str(payload.get("factor_code") or "") or (
        f"from-{source_module}-{source_ref_id}"
    )
    _draft_no = "fd-" + uuid.uuid4().hex[:12]
    _payload_for_store: dict[str, Any] = dict(payload) if payload else {}
    _source_ref_id_str: str | None = (
        str(source_ref_id) if source_ref_id is not None else None
    )
    _draft_obj = _FactorDraft(
        source_module=source_module,
        source_ref_id=_source_ref_id_str,
        draft_no=_draft_no,
        suggested_code=_suggested_code,
        suggested_name=(
            str(payload.get("display_name"))
            if payload.get("display_name") else None
        ),
        suggested_category=(
            str(payload.get("category"))
            if payload.get("category") else None
        ),
        payload_json="{}",
        review_status="submitted",
        submitted_by=(actor if actor else None),
    )

    if source_module == "custom_indicators":
        indicator_id: int | None = _parse_indicator_id(source_ref_id)
        if indicator_id is None:
            # P2-G：记录失败草稿（rejected），返回一致 DTO
            _fail_msg = "missing_numeric_indicator_id"
            _draft_obj.review_status = "rejected"
            _draft_obj.review_msg = _fail_msg
            _draft_obj.reviewed_at = _utc_naive()
            _draft_obj.payload_json = _json_safe(_payload_for_store)
            db_dft, owned_dft = _open_session()
            try:
                db_dft.add(_draft_obj)
                db_dft.commit()
            except Exception:  # best-effort: 草稿落库失败不影响返回
                db_dft.rollback()
            finally:
                if owned_dft:
                    try:
                        db_dft.close()
                    except Exception:
                        pass
            return ExternalFactorDraftSubmission(
                draft_id=_draft_no,
                factor_code=_suggested_code,
                review_status="rejected_draft",
                audit_url=None,
                message=_fail_msg,
            )
        # P2.2a：不再自动 promote，改为在审批通过时才真正 promote（见 approve_factor_draft）。
        # 这里仅做 3 项低成本前置校验，避免无效草稿进入审批队列：
        #   1) indicator 记录存在 + value_type=number（非 number 无法做因子特征）
        #   2) 因子代码与现有 factors.code 不冲突（promote 时也会再查，先快速拦截）
        # 任一项失败：草稿直接写 rejected + 返回 rejected_draft，调用方按原错误码处理。
        from sqlalchemy import select
        from app.models.custom_indicator import CustomIndicator
        from app.models.factor import Factor as _FactorORM
        db, owned = _open_session()
        _reject_reason: str | None = None
        _reject_label: str = "rejected_draft"
        try:
            # 校验 1) indicator 存在 + value_type=number
            ind_row = db.get(CustomIndicator, indicator_id)
            if ind_row is None:
                _reject_reason = "indicator_not_found"
            elif str(getattr(ind_row, "value_type", "") or "").lower() != "number":
                _reject_reason = "indicator_not_number (only number-type indicators can be promoted to factors)"
            else:
                # 校验 2) factors.code 无冲突
                conflict = db.execute(
                    select(_FactorORM.code).where(_FactorORM.code == _suggested_code)
                ).scalar_one_or_none()
                if conflict is not None:
                    _reject_reason = f"factor_code_conflict:{_suggested_code}"
                    _reject_label = "rejected_draft"
            if _reject_reason is None:
                # 校验通过 → 草稿写 submitted，回写 indicator approval_status
                _draft_obj.review_status = "submitted"
                _draft_obj.payload_json = _json_safe(_payload_for_store)
                db.add(_draft_obj)
                # ── P2.2: 审计事件 FACTOR_DRAFT_SUBMITTED（事务 flush 后） ──
                try:
                    db.flush()
                    _write_draft_audit_best_effort(
                        db,
                        "FACTOR_DRAFT_SUBMITTED",
                        draft_no=_draft_no,
                        operator_id=(actor or "external:precheck"),
                        before=None,
                        after={
                            "draft_no": _draft_no,
                            "source_module": source_module,
                            "source_ref_id": _source_ref_id_str,
                            "suggested_code": _suggested_code,
                            "review_status": "submitted",
                            "submitted_by": actor,
                        },
                        attributes={
                            "precheck": "passed",
                            "suggested_code_len": len(_suggested_code),
                        },
                        note="P2-G 草稿提交：自定义指标提审进入因子治理队列",
                    )
                except Exception:
                    pass
                db.commit()
                # P2.2a：回写 custom_indicators（静默失败，草稿已经 commit 成功）
                _update_custom_indicator(
                    indicator_id_int=indicator_id,
                    approval_status="submitted",
                    approval_review_msg=None,  # 不清空，避免覆盖历史驳回原因时丢信息
                    factor_draft_no=_draft_no,
                )
                return ExternalFactorDraftSubmission(
                    draft_id=_draft_no,
                    factor_code=_suggested_code,
                    review_status="submitted",
                    audit_url=f"/factor-center/drafts/{_draft_no}",
                    message=(
                        "draft_submitted_ok;_will_be_promoted_after_approval"
                    ),
                )
            # 校验失败 → 草稿写 rejected，回写 indicator
            _draft_obj.review_status = "rejected"
            _draft_obj.review_msg = _reject_reason
            _draft_obj.reviewed_at = _utc_naive()
            _draft_obj.reviewer = actor or "auto_precheck"
            _draft_obj.payload_json = _json_safe(_payload_for_store)
            try:
                db.rollback()
            except Exception:
                pass
            db2, owned2 = _open_session()
            try:
                db2.add(_draft_obj)
                try:
                    db2.flush()
                    _write_draft_audit_best_effort(
                        db2,
                        "FACTOR_DRAFT_REJECTED",
                        draft_no=_draft_no,
                        operator_id=(actor or "auto_precheck"),
                        before={
                            "source_module": source_module,
                            "suggested_code": _suggested_code,
                        },
                        after={"review_status": "rejected", "review_msg": _reject_reason},
                        attributes={
                            "precheck": "rejected",
                            "reject_reason": _reject_reason,
                        },
                        note="P2-G 草稿提交前置校验失败，系统自动驳回",
                    )
                except Exception:
                    pass
                db2.commit()
            except Exception:
                db2.rollback()
            finally:
                if owned2:
                    try:
                        db2.close()
                    except Exception:
                        pass
            _update_custom_indicator(
                indicator_id_int=indicator_id,
                approval_status="rejected",
                approval_review_msg=_reject_reason,
                factor_draft_no=_draft_no,
            )
            return ExternalFactorDraftSubmission(
                draft_id=_draft_no,
                factor_code=str(payload.get("factor_code") or _suggested_code),
                review_status=_reject_label,
                audit_url=None,
                message=_reject_reason or "precheck_rejected",
            )
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
            except Exception:
                pass
            _emsg = f"precheck_error:{type(exc).__name__}:{exc}"
            # best-effort 保存 rejected 草稿
            try:
                _draft_obj.review_status = "rejected"
                _draft_obj.review_msg = _emsg
                _draft_obj.reviewed_at = _utc_naive()
                _draft_obj.payload_json = _json_safe(_payload_for_store)
                db3, owned3 = _open_session()
                try:
                    db3.add(_draft_obj)
                    try:
                        db3.flush()
                        _write_draft_audit_best_effort(
                            db3,
                            "FACTOR_DRAFT_REJECTED",
                            draft_no=_draft_no,
                            operator_id=(actor or "auto_precheck"),
                            before={"suggested_code": _suggested_code, "source_module": source_module},
                            after={"review_status": "rejected", "review_msg": _emsg},
                            attributes={"precheck": "exception", "exception_type": type(exc).__name__},
                            note="P2-G 草稿提交前置校验异常，系统自动驳回",
                        )
                    except Exception:
                        pass
                    db3.commit()
                except Exception:
                    db3.rollback()
                finally:
                    if owned3:
                        try:
                            db3.close()
                        except Exception:
                            pass
            except Exception:
                pass
            return ExternalFactorDraftSubmission(
                draft_id=_draft_no,
                factor_code=str(payload.get("factor_code") or _suggested_code),
                review_status="rejected_draft",
                audit_url=None,
                message=_emsg,
            )
        finally:
            if owned:
                try:
                    db.close()
                except Exception:
                    pass

    # Other source modules: fall back to factor-draft generic submission
    raw = f"{source_module}:{source_ref_id}:{actor}:{sorted(payload.items())}"
    draft_id_short = f"ext-dft-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    # P2-G：写入 factor_drafts（状态 submitted，待审批流处理）
    _draft_obj.review_status = "submitted"
    _draft_obj.payload_json = _json_safe(_payload_for_store)
    db_g, owned_g = _open_session()
    try:
        db_g.add(_draft_obj)
        db_g.commit()
    except Exception:
        db_g.rollback()
    finally:
        if owned_g:
            try:
                db_g.close()
            except Exception:
                pass
    return ExternalFactorDraftSubmission(
        draft_id=draft_id_short,
        factor_code=str(payload.get("factor_code") or ""),
        review_status="submitted",
        audit_url=None,
        message=f"submission_recorded:{_draft_no};_awaiting_factor-domain_pipeline_support_for_source={source_module}",
    )


# ── 6. P1 UI Stitching: FactorSets + Model detail + Factor usage ─────

def list_factorsets(
    *,
    scope: Literal["any", "frozen", "active"] = "any",
    limit: int = 100,
) -> list[ScoreFactorSetDTO]:
    """List factor sets for the model-hub stitching UI.

    When ``scope="active"`` only returns sets that are referenced by at
    least one ``validated`` factor model run. ``frozen`` matches sets with
    status=frozen (ready to train on).
    """
    from sqlalchemy import select, desc, func
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    from app.models.factor_model import FactorModelRun
    from app.services.factor_model_contract import model_factor_set_id as _fsid

    db, owned = _open_session()
    try:
        # Count feature members per FactorSet
        member_stmt = (
            select(
                FactorSetMember.factor_set_id,
                func.count(FactorSetMember.id).label("n"),
            )
            .where(FactorSetMember.role == "feature")
            .group_by(FactorSetMember.factor_set_id)
        )
        member_counts = {row.factor_set_id: row.n for row in db.execute(member_stmt).all()}

        # Which validated models reference each set?
        all_runs = db.execute(select(FactorModelRun)).scalars().all()
        set_to_validated_runs: dict[str, list[str]] = {}
        for run in all_runs:
            if run.status != "validated":
                continue
            fsid = _fsid(run)
            if fsid:
                set_to_validated_runs.setdefault(fsid, []).append(run.id)

        stmt = select(FactorSet).order_by(
            desc(FactorSet.updated_at), desc(FactorSet.created_at)
        )
        if scope == "frozen":
            stmt = stmt.where(FactorSet.status == "frozen")
        elif scope == "active":
            ids = list(set_to_validated_runs.keys())
            if not ids:
                return []
            stmt = stmt.where(FactorSet.id.in_(ids))
        rows = db.execute(stmt.limit(limit)).scalars().all()

        out: list[ScoreFactorSetDTO] = []
        for fs in rows:
            if scope == "active" and fs.id not in set_to_validated_runs:
                continue
            out.append(ScoreFactorSetDTO(
                id=fs.id,
                label=fs.name,
                status=fs.status or "draft",
                member_count=int(member_counts.get(fs.id, 0)),
                created_at=fs.created_at.isoformat() if fs.created_at else None,
                frozen_at=fs.frozen_at.isoformat() if fs.frozen_at else None,
                is_active_for_model_run_ids=list(set_to_validated_runs.get(fs.id, [])),
                description=fs.description,
            ))
        return out
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def get_model_detail(model_id: str) -> ScoreModelDetailDTO | None:
    """Deep model payload used by the factor-model "expand" UI.

    Returns ``None`` if the run id does not exist. All factor references
    join in the display name from the Factor registry so the model page
    shows meaningful labels rather than cryptic codes.
    """
    from sqlalchemy import select
    from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
    from app.models.factor_evaluation import FactorSet
    from app.models.factor import Factor
    from app.services.factor_model_contract import model_factor_set_id

    db, owned = _open_session()
    try:
        run = db.get(FactorModelRun, model_id)
        if run is None:
            return None
        metrics = _json_object(run.metrics_json)
        fsid = model_factor_set_id(run)
        fs = db.get(FactorSet, fsid) if fsid else None

        # Preload factor code -> name
        weight_rows = db.execute(
            select(FactorWeightSnapshot).where(
                FactorWeightSnapshot.model_run_id == run.id
            )
        ).scalars().all()
        codes = sorted({w.factor_code for w in weight_rows})
        name_map: dict[str, str] = {}
        if codes:
            for f in db.execute(select(Factor).where(Factor.code.in_(codes))).scalars():
                name_map[f.code] = f.name

        # Build a best-effort coverage map from factor health
        coverage_map: dict[str, float] = {}
        try:
            from app.services.factors.config import get_factor_system_config
            from app.services.factors.health import get_factor_health
            from app.services.factors.store import FactorWarehouse
            cfg = get_factor_system_config(db)
            h = get_factor_health(FactorWarehouse(cfg.warehouse_path))
            for fh in getattr(h, "factors", []) or []:
                coverage_map[str(getattr(fh, "factor_code", ""))] = float(
                    getattr(fh, "coverage", 0) or 0
                )
        except Exception:
            coverage_map = {}

        factors: list[ScoreModelFactorMemberDTO] = []
        for w in weight_rows:
            nw = float(w.normalized_weight) if w.normalized_weight is not None else 0.0
            side: Literal["long", "short", "neutral"] = (
                "long" if nw > 1e-9 else "short" if nw < -1e-9 else "neutral"
            )
            factors.append(ScoreModelFactorMemberDTO(
                factor_code=w.factor_code,
                factor_name=name_map.get(w.factor_code),
                factor_version=int(w.factor_version),
                coefficient=float(w.coefficient or 0.0),
                normalized_weight=nw,
                train_ic=(float(w.train_ic) if w.train_ic is not None else None),
                validation_ic=(float(w.validation_ic) if w.validation_ic is not None else None),
                coverage=coverage_map.get(w.factor_code),
                side=side,
            ))
        factors.sort(key=lambda m: abs(m.normalized_weight), reverse=True)

        return ScoreModelDetailDTO(
            id=run.id,
            name=_model_display_name(run, fsid),
            status=run.status or "unknown",
            model_type=run.model_type or "ridge",
            weight_mode="ridge",
            validation_ic=_as_float(metrics.get("validation_ic")),
            validation_icir=_as_float(metrics.get("validation_icir")),
            validation_r2=_as_float(metrics.get("validation_r2")),
            train_ic=_as_float(metrics.get("train_ic")),
            sample_count=int(run.sample_count or 0),
            symbol_count=int(run.symbol_count or 0),
            trade_date_count=int(run.trade_date_count or 0),
            train_start_date=run.train_start_date.isoformat() if run.train_start_date else None,
            train_end_date=run.train_end_date.isoformat() if run.train_end_date else None,
            validation_start_date=run.validation_start_date.isoformat() if run.validation_start_date else None,
            validation_end_date=run.validation_end_date.isoformat() if run.validation_end_date else None,
            data_cutoff_at=run.data_cutoff_at.isoformat() if run.data_cutoff_at else None,
            created_at=run.created_at.isoformat() if run.created_at else None,
            activated_at=run.activated_at.isoformat() if run.activated_at else None,
            rejection_reason=run.rejection_reason,
            factorset_id=fsid,
            factorset_label=(fs.name if fs else fsid),
            factorset_member_count=(
                len([m for m in (fs.members if fs else []) if getattr(m, "role", "feature") == "feature"])
                if fs else (len(factors))
            ),
            factors=factors,
        )
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def get_factor_usage(factor_code: str) -> FactorUsageDTO | None:
    """Factor → FactorSets + ModelRuns where used. For the factor-hub drawer."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion
    from app.models.factor_evaluation import FactorSet, FactorSetMember
    from app.models.factor_model import FactorModelRun, FactorWeightSnapshot
    from app.services.factor_model_contract import model_factor_set_id as _fsid

    db, owned = _open_session()
    try:
        factor = db.execute(
            select(Factor).where(Factor.code == factor_code)
        ).scalar_one_or_none()
        if factor is None:
            return None

        # Factor sets containing this factor
        set_rows = db.execute(
            select(FactorSet, FactorSetMember)
            .join(FactorSetMember, FactorSetMember.factor_set_id == FactorSet.id)
            .where(FactorSetMember.factor_code == factor_code)
            .order_by(FactorSet.created_at.desc())
        ).all()
        in_factor_sets: list[FactorUsageSetDTO] = [
            FactorUsageSetDTO(
                factor_set_id=fs.id,
                label=fs.name,
                status=fs.status or "draft",
                role=mem.role or "feature",
                version=int(mem.factor_version),
            )
            for fs, mem in set_rows
        ]

        # Model runs that reference the factor via FactorWeightSnapshot
        weight_rows = db.execute(
            select(FactorWeightSnapshot).where(
                FactorWeightSnapshot.factor_code == factor_code
            )
        ).scalars().all()
        model_ids = sorted({w.model_run_id for w in weight_rows})
        runs_by_id: dict[str, FactorModelRun] = {}
        if model_ids:
            for r in db.execute(select(FactorModelRun).where(FactorModelRun.id.in_(model_ids))).scalars():
                runs_by_id[r.id] = r
        w_by_model = {w.model_run_id: w for w in weight_rows}

        in_models: list[FactorUsageModelDTO] = []
        for mid in sorted(runs_by_id.keys(), key=lambda m: (runs_by_id[m].created_at or datetime.min), reverse=True):
            run = runs_by_id[mid]
            w = w_by_model.get(mid)
            m_metrics = _json_object(run.metrics_json)
            in_models.append(FactorUsageModelDTO(
                model_id=run.id,
                model_name=_model_display_name(run, _fsid(run)),
                status=run.status or "unknown",
                normalized_weight=(
                    float(w.normalized_weight) if w and w.normalized_weight is not None else 0.0
                ),
                validation_ic=(
                    float(w.validation_ic) if w and w.validation_ic is not None else None
                ),
                model_validation_ic=_as_float(m_metrics.get("validation_ic")),
                activated_at=run.activated_at.isoformat() if run.activated_at else None,
            ))

        # Best-effort quality fields
        active_version: int | None = None
        if factor.active_version_id:
            fv = db.get(FactorVersion, factor.active_version_id)
            active_version = fv.version if fv else None
        days_in_production: int | None = None
        if factor.archived_at is None and factor.created_at:
            days_in_production = max(0, (datetime.now(timezone.utc).replace(tzinfo=None) - factor.created_at).days)

        coverage_30d: float | None = None
        ic_mean_30d: float | None = None
        try:
            from app.services.factors.config import get_factor_system_config
            from app.services.factors.health import get_factor_health
            from app.services.factors.store import FactorWarehouse
            cfg = get_factor_system_config(db)
            h = get_factor_health(FactorWarehouse(cfg.warehouse_path))
            for fh in getattr(h, "factors", []) or []:
                if str(getattr(fh, "factor_code", "")) == factor_code:
                    coverage_30d = float(getattr(fh, "coverage", 0) or 0)
                    ic_val = getattr(fh, "ic_mean", None) or getattr(fh, "ic", None)
                    ic_mean_30d = float(ic_val) if ic_val is not None else None
                    break
        except Exception:
            coverage_30d = None
            ic_mean_30d = None

        return FactorUsageDTO(
            code=factor.code,
            name=factor.name,
            status=factor.status or "unknown",
            lifecycle_status=factor.lifecycle_status,
            origin=factor.origin,
            category=factor.category,
            is_active=bool(factor.is_active),
            description=factor.description,
            active_version=active_version,
            coverage_30d=coverage_30d,
            ic_mean_30d=ic_mean_30d,
            days_in_production=days_in_production,
            in_factor_sets=in_factor_sets,
            in_models=in_models,
        )
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


# ── P2-G 治理：草稿查询 + 审批/驳回接口 ────────────────────────────

def _draft_to_dto(draft: Any) -> FactorDraftDTO:
    """ORM FactorDraft → public FactorDraftDTO (no ORM refs leak out)."""
    payload_obj = _json_object(getattr(draft, "payload_json", None) or "{}")

    # P2.2c.1：优先从因子表解析 promoted_factor_code（跨外键不可靠，用查询）
    _promoted_code: str | None = getattr(draft, "promoted_factor_code", None)
    if not _promoted_code:
        _prom_id = getattr(draft, "promoted_factor_id", None)
        if _prom_id:
            try:
                from sqlalchemy import select as _sa_select
                from app.models.factor import Factor as _FactorORM
                _db, _owned = _open_session()
                try:
                    _c = _db.execute(
                        _sa_select(_FactorORM.code).where(_FactorORM.id == int(_prom_id))
                    ).scalar_one_or_none()
                    if _c:
                        _promoted_code = str(_c)
                finally:
                    if _owned:
                        try:
                            _db.close()
                        except Exception:
                            pass
            except Exception:
                _promoted_code = None

    return FactorDraftDTO(
        id=int(getattr(draft, "id", 0) or 0),
        draft_no=str(getattr(draft, "draft_no", "") or ""),
        source_module=str(getattr(draft, "source_module", "") or ""),
        source_ref_id=getattr(draft, "source_ref_id", None),
        suggested_code=str(getattr(draft, "suggested_code", "") or ""),
        suggested_name=getattr(draft, "suggested_name", None),
        suggested_category=getattr(draft, "suggested_category", None),
        payload=payload_obj,
        review_status=str(getattr(draft, "review_status", "submitted") or "submitted"),  # type: ignore[arg-type]
        submitted_by=getattr(draft, "submitted_by", None),
        reviewer=getattr(draft, "reviewer", None),
        review_msg=getattr(draft, "review_msg", None),
        promoted_factor_id=getattr(draft, "promoted_factor_id", None),
        promoted_factor_code=_promoted_code,
        promoted_factor_version=getattr(draft, "promoted_factor_version", None),
        submitted_at=(
            draft.submitted_at.isoformat()
            if getattr(draft, "submitted_at", None) and hasattr(draft.submitted_at, "isoformat")
            else None
        ),
        reviewed_at=(
            draft.reviewed_at.isoformat()
            if getattr(draft, "reviewed_at", None) and hasattr(draft.reviewed_at, "isoformat")
            else None
        ),
        applied_at=(
            draft.applied_at.isoformat()
            if getattr(draft, "applied_at", None) and hasattr(draft.applied_at, "isoformat")
            else None
        ),
        audit_url=(
            f"/factor-center/drafts/{draft.draft_no}"
            if getattr(draft, "draft_no", None) else None
        ),
    )


def _write_draft_audit_best_effort(
    db: Any,
    action: str,
    *,
    draft_no: str,
    operator_id: str,
    before: Any = None,
    after: Any = None,
    attributes: dict[str, Any] | None = None,
    note: str | None = None,
) -> None:
    """Best-effort：草稿治理闭环写审计事件。失败仅 warning 不抛异常。"""
    try:
        from app.services.data_governance_audit import write_audit_event  # 延迟导入
        write_audit_event(
            db,
            action,  # allowed set 在 data_governance_audit 中已扩展 FACTOR_DRAFT_*
            business_key=str(draft_no),
            operator_id=str(operator_id or "system"),
            correlation_id=(
                f"factor-draft:{draft_no}" if draft_no else None
            ),
            before=before,
            after=after,
            attributes=attributes,
            note=note,
        )
    except Exception as _audit_err:
        import logging as _log_a
        _log_a.getLogger(__name__).warning(
            "[P2-G][draft] 写审计事件失败（best-effort 跳过）action=%s draft=%s err=%s",
            action, draft_no, _audit_err,
        )


def list_factor_drafts(
    *,
    status: FactorDraftStatus | Literal["any"] = "any",
    source_module: str | None = None,
    limit: int = 100,
) -> list[FactorDraftDTO]:
    """列出因子草稿（治理列表页）。"""
    from datetime import datetime, timezone
    from sqlalchemy import select, desc
    from app.models.factor_governance import FactorDraft as _FactorDraft

    db, owned = _open_session()
    try:
        stmt = select(_FactorDraft).order_by(
            desc(_FactorDraft.submitted_at), desc(_FactorDraft.id),
        )
        if status and status != "any":
            stmt = stmt.where(_FactorDraft.review_status == status)
        if source_module:
            stmt = stmt.where(_FactorDraft.source_module == source_module)
        rows = db.execute(stmt.limit(limit)).scalars().all()
        return [_draft_to_dto(r) for r in rows]
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def get_factor_draft(draft_no: str) -> FactorDraftDTO | None:
    """按 draft_no 获取单条草稿详情。"""
    from sqlalchemy import select
    from app.models.factor_governance import FactorDraft as _FactorDraft

    db, owned = _open_session()
    try:
        row = db.execute(
            select(_FactorDraft).where(_FactorDraft.draft_no == draft_no)
        ).scalar_one_or_none()
        return _draft_to_dto(row) if row is not None else None
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def _transition_draft_status(
    draft_no: str,
    *,
    target_status: FactorDraftStatus,
    reviewer: str,
    review_msg: str | None = None,
) -> FactorDraftDTO:
    """通用状态迁移：submitted → approved / rejected（幂等）。"""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models.factor_governance import FactorDraft as _FactorDraft

    LEGAL_TRANSITIONS: dict[str, set[str]] = {
        "submitted": {"approved", "rejected", "applied"},
        "approved": {"applied", "rejected"},
        "rejected": {"submitted"},  # 允许重新提交
        "applied": set(),            # 终态
        "deleted": set(),
    }

    def _utc_naive() -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    db, owned = _open_session()
    try:
        row = db.execute(
            select(_FactorDraft).where(_FactorDraft.draft_no == draft_no)
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"draft_not_found:{draft_no}")
        current = str(row.review_status or "submitted")
        if current == target_status:
            # 幂等：已处于目标状态，只刷新 reviewer / msg
            row.reviewer = reviewer or row.reviewer
            if review_msg is not None:
                row.review_msg = review_msg
            row.reviewed_at = row.reviewed_at or _utc_naive()
            db.commit()
            return _draft_to_dto(row)
        allowed = LEGAL_TRANSITIONS.get(current, set())
        if target_status not in allowed:
            raise ValueError(
                f"illegal_transition:{current}→{target_status} for draft={draft_no}"
            )
        row.review_status = target_status
        row.reviewer = reviewer or row.reviewer
        if review_msg is not None:
            row.review_msg = review_msg
        row.reviewed_at = _utc_naive()
        if target_status == "applied":
            row.applied_at = _utc_naive()
        db.commit()
        # refresh
        db.refresh(row)
        return _draft_to_dto(row)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def approve_factor_draft(
    draft_no: str,
    *,
    reviewer: str,
    review_msg: str | None = None,
) -> FactorDraftDTO:
    """审批通过草稿（状态：submitted/approved → approved）。

    P2.2b：对于 source_module == "custom_indicators" 且 promote 未完成的草稿，
    在状态迁到 approved 后**立刻执行真正 promote**（调用
    ``promote_factor_from_indicator``）：
      - promote 成功：再次迁到 applied，回填 promoted_factor_id / version
      - promote 失败：状态**回滚到 submitted**，review_msg 追加 promote_err: ...，
        不会吞 promote 异常信息，审批人可以在草稿详情看到原错误后重新审批。
    """
    draft = _transition_draft_status(
        draft_no,
        target_status="approved",
        reviewer=reviewer,
        review_msg=review_msg,
    )
    # ── P2.2b：custom_indicators 审批通过 → 立刻 promote ──
    if (
        draft.source_module == "custom_indicators"
        and draft.review_status == "approved"
        and draft.promoted_factor_id in (None, 0)
    ):
        # Reload ORM row for promote (need mutable row for applied transition + promoted_* fill)
        from sqlalchemy import select
        from datetime import datetime, timezone
        from app.models.factor_governance import FactorDraft as _FactorDraft

        db, owned = _open_session()
        try:
            row = db.execute(
                select(_FactorDraft).where(_FactorDraft.draft_no == draft_no)
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"draft_missing_after_approve:{draft_no}")
            try:
                factor, version = _promote_factor_draft(row)
                # promote 成功 → 迁 applied
                row.review_status = "applied"
                _fid = int(getattr(factor, "id", 0) or 0)
                _vid = int(getattr(version, "id", 0) or 0)
                _vno = int(getattr(version, "version", 0) or 0)
                row.promoted_factor_id = _fid or None
                # promoted_factor_version 存 FactorVersion.id（主表版本行），兼容既有
                row.promoted_factor_version = _vid or None
                row.applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
                _prom_code = str(getattr(factor, "code", "") or "")
                row.review_msg = (
                    str(review_msg)
                    + " | promote_ok:"
                    + f" factor_code={_prom_code} vid={_vid} vno={_vno}"
                ) if review_msg else (
                    f"promote_ok: factor_code={_prom_code} vid={_vid} vno={_vno}"
                )
                # ── P2.2: promote 成功 → 审计 APPROVED（applied）+ VERSION_PROMOTED ──
                try:
                    db.flush()
                    _before = {
                        "draft_no": draft_no,
                        "review_status": "approved",  # _transition 返回的状态
                        "source_module": draft.source_module,
                        "source_ref_id": getattr(row, "source_ref_id", None),
                    }
                    _after = {
                        "review_status": "applied",
                        "promoted_factor_id": _fid or None,
                        "promoted_factor_code": _prom_code,
                        "promoted_factor_version": _vid or None,
                        "promoted_factor_version_no": _vno or None,
                        "review_msg": row.review_msg,
                    }
                    _write_draft_audit_best_effort(
                        db,
                        "FACTOR_DRAFT_APPROVED",
                        draft_no=draft_no,
                        operator_id=reviewer or "system",
                        before=_before,
                        after=_after,
                        attributes={
                            "approve_review_msg": review_msg,
                            "promote_result": "ok",
                            "factor_id": _fid or None,
                            "factor_code": _prom_code,
                            "version": _vid or None,
                            "version_no": _vno or None,
                            "indicator_id": _parse_indicator_id(getattr(row, "source_ref_id", None)),
                        },
                        note="P2-G 草稿审批通过：已 promote 为正式因子并落库 applied",
                    )
                    # 额外一条 FACTOR_VERSION_PROMOTED：用于「因子入库血缘」追溯
                    _write_draft_audit_best_effort(
                        db,
                        "FACTOR_VERSION_PROMOTED",
                        draft_no=draft_no,
                        operator_id=reviewer or "system",
                        before={
                            "source_module": draft.source_module,
                            "source_ref_id": getattr(row, "source_ref_id", None),
                            "suggested_code": draft.suggested_code,
                        },
                        after={
                            "factor_id": _fid or None,
                            "factor_code": _prom_code,
                            "factor_version": _vid or None,
                            "factor_version_no": _vno or None,
                        },
                        attributes={
                            "factor_id": _fid or None,
                            "factor_code": _prom_code,
                            "factor_version": _vid or None,
                            "factor_version_no": _vno or None,
                            "draft_no": draft_no,
                        },
                        note=(
                            "P2-G 因子版本入库：custom_indicator→FactorVersion "
                            f"({_prom_code} vno={_vno})"
                        ),
                    )
                except Exception:
                    pass
                db.commit()
                db.refresh(row)
                # 回写 custom_indicators：promoted + promoted_factor_code
                ind_id = _parse_indicator_id(getattr(row, "source_ref_id", None))
                if ind_id is not None:
                    _update_custom_indicator(
                        indicator_id_int=ind_id,
                        approval_status="promoted",
                        approval_review_msg=None,
                        promoted_factor_code=str(getattr(factor, "code", "") or ""),
                        factor_draft_no=draft_no,
                    )
                return _draft_to_dto(row)
            except Exception as _promote_err:
                # promote 失败：回滚 draft 到 submitted，review_msg 追加错误
                try:
                    db.rollback()
                except Exception:
                    pass
                # 重新用最新 db 读 row（可能因 rollback 已 detached），改状态与 review_msg
                row2 = db.execute(
                    select(_FactorDraft).where(_FactorDraft.draft_no == draft_no)
                ).scalar_one_or_none()
                if row2 is not None:
                    # LEGAL_TRANSITIONS 需要允许 approved→submitted，先手动覆盖
                    row2.review_status = "submitted"
                    _err_suffix = f"promote_err:{type(_promote_err).__name__}:{_promote_err}"
                    row2.review_msg = (
                        (str(row2.review_msg) + " | " + _err_suffix)
                        if row2.review_msg else _err_suffix
                    )
                    # reviewed_at 保留（还是这个审批人的结果），applied_at 清空
                    row2.applied_at = None
                    # ── P2.2: promote 失败 → 审计（best-effort，不阻塞） ──
                    try:
                        db.flush()
                        _write_draft_audit_best_effort(
                            db,
                            "FACTOR_DRAFT_APPROVED",
                            draft_no=draft_no,
                            operator_id=reviewer or "system",
                            before={
                                "review_status": "approved",
                                "source_module": draft.source_module,
                                "suggested_code": draft.suggested_code,
                            },
                            after={
                                "review_status": "submitted",  # 回滚到 submitted
                                "review_msg": row2.review_msg,
                            },
                            attributes={
                                "approve_review_msg": review_msg,
                                "promote_result": "error",
                                "exception_type": type(_promote_err).__name__,
                            },
                            note="P2-G 草稿审批通过：promote 阶段失败，状态回滚 submitted（附错误原因）",
                        )
                    except Exception:
                        pass
                    try:
                        db.commit()
                        db.refresh(row2)
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                    # 回写 indicator：回到 pending（让上游知道需要处理）
                    ind_id = _parse_indicator_id(getattr(row2, "source_ref_id", None))
                    if ind_id is not None:
                        _update_custom_indicator(
                            indicator_id_int=ind_id,
                            approval_status="pending",
                            approval_review_msg=_err_suffix,
                            factor_draft_no=draft_no,
                        )
                    return _draft_to_dto(row2)
                raise
        finally:
            if owned:
                try:
                    db.close()
                except Exception:
                    pass
    # ── P2.2 非 custom_indicators 或已 approved 无 promote 分支：仍写一条 APPROVED 审计 ──
    if (
        draft.source_module != "custom_indicators"
        or draft.promoted_factor_id not in (None, 0)
    ):
        try:
            from sqlalchemy import select as _sa_select
            from app.models.factor_governance import FactorDraft as _FD
            _aud_db, _aud_owned = _open_session()
            try:
                _r = _aud_db.execute(
                    _sa_select(_FD).where(_FD.draft_no == draft_no)
                ).scalar_one_or_none()
                if _r is not None:
                    _aud_db.flush()
                    _write_draft_audit_best_effort(
                        _aud_db,
                        "FACTOR_DRAFT_APPROVED",
                        draft_no=draft_no,
                        operator_id=reviewer or "system",
                        before={"review_status": "submitted"},
                        after={
                            "review_status": draft.review_status,
                            "review_msg": review_msg,
                            "source_module": draft.source_module,
                        },
                        attributes={
                            "approve_review_msg": review_msg,
                            "promote_result": "skipped",
                            "skip_reason": (
                                "already_promoted"
                                if draft.promoted_factor_id not in (None, 0)
                                else f"source_module:{draft.source_module}"
                            ),
                        },
                        note="P2-G 草稿审批通过：无 promote 步骤（非 custom_indicators 或已 promoted）",
                    )
                    _aud_db.commit()
            except Exception:
                try:
                    _aud_db.rollback()
                except Exception:
                    pass
            finally:
                if _aud_owned:
                    try:
                        _aud_db.close()
                    except Exception:
                        pass
        except Exception:
            pass
    return draft


def reject_factor_draft(
    draft_no: str,
    *,
    reviewer: str,
    review_msg: str,
) -> FactorDraftDTO:
    """审批驳回草稿（必须说明原因）。

    P2.2c：驳回后若 source_module == "custom_indicators"，回写
    ``custom_indicators.approval_status=rejected`` + ``approval_review_msg``，
    让自定义指标 UI 能展示驳回原因。
    """
    if not review_msg or not str(review_msg).strip():
        raise ValueError("reject_review_msg_required")
    result = _transition_draft_status(
        draft_no,
        target_status="rejected",
        reviewer=reviewer,
        review_msg=str(review_msg),
    )
    if (
        result.source_module == "custom_indicators"
        and result.review_status == "rejected"
    ):
        ind_id = _parse_indicator_id(result.source_ref_id)
        if ind_id is not None:
            _update_custom_indicator(
                indicator_id_int=ind_id,
                approval_status="rejected",
                approval_review_msg=str(review_msg),
                factor_draft_no=draft_no,
            )
    # ── P2.2 审计：FACTOR_DRAFT_REJECTED（所有 source_module） ──
    try:
        from sqlalchemy import select as _sa_select
        from app.models.factor_governance import FactorDraft as _FD
        _aud_db, _aud_owned = _open_session()
        try:
            _r = _aud_db.execute(
                _sa_select(_FD).where(_FD.draft_no == draft_no)
            ).scalar_one_or_none()
            if _r is not None:
                _aud_db.flush()
                _write_draft_audit_best_effort(
                    _aud_db,
                    "FACTOR_DRAFT_REJECTED",
                    draft_no=draft_no,
                    operator_id=reviewer or "system",
                    before={
                        "review_status": (
                            "submitted" if result.review_status == "rejected"
                            else "unknown"
                        ),
                        "suggested_code": result.suggested_code,
                        "source_module": result.source_module,
                    },
                    after={
                        "review_status": "rejected",
                        "review_msg": str(review_msg),
                    },
                    attributes={
                        "reject_msg": str(review_msg),
                        "reject_reason_len": len(str(review_msg)),
                    },
                    note="P2-G 草稿审批驳回（管理员手动）",
                )
                _aud_db.commit()
        except Exception:
            try:
                _aud_db.rollback()
            except Exception:
                pass
        finally:
            if _aud_owned:
                try:
                    _aud_db.close()
                except Exception:
                    pass
    except Exception:
        pass
    return result


# ── P2-G 治理：2 道训练准入门 + 统一聚合执行 ──────────────────────

def _collect_quality_from_daily(
    db: Any,
    factor_codes: list[str],
    *,
    lookback_days: int,
) -> dict[str, dict[str, float | None]]:
    """从 factor_quality_daily 表中拉取最近 N 天的覆盖率/IC 均值。

    返回 dict[factor_code] → {"coverage": float, "ic_mean": float}。
    如果某因子没有行（表还未开始写入），对应 value 为 None。
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, func, and_
    from app.models.factor_governance import FactorQualityDaily

    if not factor_codes:
        return {}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, lookback_days) * 2)
    # 因为 trade_date 是 date，cutoff 足够宽以覆盖 lookback_days 个交易日
    stmt = (
        select(
            FactorQualityDaily.factor_code,
            func.avg(FactorQualityDaily.coverage).label("avg_cov"),
            func.avg(FactorQualityDaily.ic_mean).label("avg_ic"),
            func.count(FactorQualityDaily.id).label("n_rows"),
        )
        .where(
            and_(
                FactorQualityDaily.factor_code.in_(factor_codes),
                FactorQualityDaily.trade_date >= cutoff.date(),
            )
        )
        .group_by(FactorQualityDaily.factor_code)
    )
    out: dict[str, dict[str, float | None]] = {}
    for row in db.execute(stmt).all():
        code = str(row.factor_code)
        out[code] = {
            "coverage": _as_float(row.avg_cov),
            "ic_mean": _as_float(row.avg_ic),
            "n_rows": float(row.n_rows or 0),
        }
    for c in factor_codes:
        if c not in out:
            out[c] = {"coverage": None, "ic_mean": None, "n_rows": 0.0}
    return out


def _collect_quality_from_warehouse(
    factor_codes: list[str],
) -> dict[str, dict[str, float | None]]:
    """Fallback：从 FactorWarehouse health 快照里取覆盖率 / IC 均值。

    粒度没日级表细，但能保证首次训练前表为空时门禁仍有值可用而不是直接全放行。
    """
    out: dict[str, dict[str, float | None]] = {
        c: {"coverage": None, "ic_mean": None, "n_rows": 0.0} for c in factor_codes
    }
    try:
        from app.services.factors.config import get_factor_system_config
        from app.services.factors.health import get_factor_health
        from app.services.factors.store import FactorWarehouse
        db, owned = _open_session()
        try:
            cfg = get_factor_system_config(db)
        finally:
            if owned:
                try:
                    db.close()
                except Exception:
                    pass
        h = get_factor_health(FactorWarehouse(cfg.warehouse_path))
        for fh in getattr(h, "factors", []) or []:
            code = str(getattr(fh, "factor_code", ""))
            if code not in out:
                continue
            cov = _as_float(getattr(fh, "coverage", None))
            ic_val = getattr(fh, "ic_mean", None) or getattr(fh, "ic", None)
            ic = _as_float(ic_val)
            out[code] = {"coverage": cov, "ic_mean": ic, "n_rows": 1.0}
    except Exception:
        pass
    return out


def _merge_quality(
    primary: dict[str, dict[str, float | None]],
    fallback: dict[str, dict[str, float | None]],
) -> dict[str, dict[str, float | None]]:
    merged: dict[str, dict[str, float | None]] = {}
    all_codes = set(primary.keys()) | set(fallback.keys())
    for c in all_codes:
        p = primary.get(c) or {}
        f = fallback.get(c) or {}
        merged[c] = {
            "coverage": p.get("coverage") if p.get("coverage") is not None else f.get("coverage"),
            "ic_mean": p.get("ic_mean") if p.get("ic_mean") is not None else f.get("ic_mean"),
            "n_rows": p.get("n_rows") if p.get("n_rows") is not None else f.get("n_rows"),
        }
    return merged


def gate_factor_coverage(
    factor_codes: list[str],
    *,
    threshold: float = 0.70,
    lookback_days: int = 30,
) -> GovernanceGateResultDTO:
    """准入门 ①：因子覆盖率门槛（默认 ≥ 70%）。

    对传入的 factor_codes 取最近 lookback_days 的平均覆盖率，
    要求 **每个因子** 都不低于 threshold。
    若某因子无任何观测数据（quality_daily + warehouse 都没值），
    视为 NOT_PASSED（通过未知风险阻止训练）。
    """
    codes = [c for c in factor_codes if c]
    if not codes:
        return GovernanceGateResultDTO(
            gate_name="factor_coverage",
            passed=False,
            score=None,
            threshold_min=float(threshold),
            threshold_max=1.0,
            reasons=["empty_factor_list"],
            detail={},
        )
    db, owned = _open_session()
    try:
        daily = _collect_quality_from_daily(db, codes, lookback_days=lookback_days)
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass
    wh = _collect_quality_from_warehouse(codes)
    merged = _merge_quality(daily, wh)

    reasons: list[str] = []
    detail: dict[str, Any] = {}
    all_pass = True
    min_cov: float | None = None
    for c in codes:
        row = merged.get(c, {})
        cov = row.get("coverage")
        detail[c] = {"coverage": cov, "n_samples": row.get("n_rows")}
        if cov is None:
            reasons.append(f"{c}:no_coverage_data")
            all_pass = False
            continue
        if min_cov is None or cov < min_cov:
            min_cov = cov
        if cov < threshold:
            reasons.append(f"{c}:coverage={cov:.4f}<{threshold}")
            all_pass = False
    return GovernanceGateResultDTO(
        gate_name="factor_coverage",
        passed=all_pass,
        score=min_cov,
        threshold_min=float(threshold),
        threshold_max=1.0,
        reasons=reasons,
        detail=detail,
    )


def gate_factor_ic(
    factor_codes: list[str],
    *,
    min_ic: float = 0.01,
    max_ic: float = 0.10,
    lookback_days: int = 30,
) -> GovernanceGateResultDTO:
    """准入门 ②：IC 合理性范围（默认 abs(IC) ∈ [0.01, 0.10]）。

    - 过小（< 0.01）：和噪声差不多，学不到东西
    - 过大（> 0.10）：疑似未来函数 / 过拟合陷阱，必须人工复核
    - 无观测：视为 NOT_PASSED
    - 对每个因子判断绝对值在区间内；整体 passed = 所有因子 passed
    """
    codes = [c for c in factor_codes if c]
    if not codes:
        return GovernanceGateResultDTO(
            gate_name="factor_ic_range",
            passed=False,
            score=None,
            threshold_min=float(min_ic),
            threshold_max=float(max_ic),
            reasons=["empty_factor_list"],
            detail={},
        )
    db, owned = _open_session()
    try:
        daily = _collect_quality_from_daily(db, codes, lookback_days=lookback_days)
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass
    wh = _collect_quality_from_warehouse(codes)
    merged = _merge_quality(daily, wh)

    reasons: list[str] = []
    detail: dict[str, Any] = {}
    all_pass = True
    worst_deviation: float | None = None
    for c in codes:
        row = merged.get(c, {})
        ic_raw = row.get("ic_mean")
        abs_ic = abs(ic_raw) if ic_raw is not None else None
        detail[c] = {"ic_mean": ic_raw, "abs_ic": abs_ic, "n_samples": row.get("n_rows")}
        if abs_ic is None:
            reasons.append(f"{c}:no_ic_data")
            all_pass = False
            continue
        # 偏离区间的程度（越大越差），用于 score（worst one）
        if abs_ic < min_ic:
            dev = min_ic - abs_ic
            reasons.append(f"{c}:abs_ic={abs_ic:.4f}<{min_ic}")
            all_pass = False
        elif abs_ic > max_ic:
            dev = abs_ic - max_ic
            reasons.append(f"{c}:abs_ic={abs_ic:.4f}>{max_ic}_suspicious_overfit")
            all_pass = False
        else:
            dev = 0.0
        if worst_deviation is None or dev > worst_deviation:
            worst_deviation = dev
    return GovernanceGateResultDTO(
        gate_name="factor_ic_range",
        passed=all_pass,
        score=worst_deviation,
        threshold_min=float(min_ic),
        threshold_max=float(max_ic),
        reasons=reasons,
        detail=detail,
    )


def run_training_eligibility_gates(
    *,
    factor_codes: list[str] | None = None,
    factorset_id: str | None = None,
    model_run_id: str | None = None,
    coverage_threshold: float = 0.70,
    ic_min: float = 0.01,
    ic_max: float = 0.10,
    lookback_days: int = 30,
) -> list[GovernanceGateResultDTO]:
    """聚合执行所有已注册的准入门禁（默认 2 道）。

    可通过 3 种入口调用（任一即可，优先级：factor_codes > factorset_id > model_run_id）：
    - factor_codes：直接传入因子列表（门禁预检查 / 训练前 API）
    - factorset_id：按 FactorSet 查成员再执行（"训练此集合"按钮）
    - model_run_id：按已有 model_run / FactorModelMember 执行（"重校验当前模型"）

    返回值：按 gate_name 排序的结果列表，调用方应检查所有 passed=True。
    """
    codes: list[str] = []
    if factor_codes:
        codes = [c for c in factor_codes if c]
    elif factorset_id:
        from sqlalchemy import select
        from app.models.factor_evaluation import FactorSetMember
        db, owned = _open_session()
        try:
            rows = db.execute(
                select(FactorSetMember).where(
                    FactorSetMember.factor_set_id == factorset_id,
                    FactorSetMember.role == "feature",
                )
            ).scalars().all()
            codes = sorted({r.factor_code for r in rows if r.factor_code})
        finally:
            if owned:
                try:
                    db.close()
                except Exception:
                    pass
    elif model_run_id:
        from sqlalchemy import select
        from app.models.factor_model import FactorWeightSnapshot
        from app.models.factor_governance import FactorModelMember
        db, owned = _open_session()
        try:
            # 优先用治理物化表 FactorModelMember；回退到 FactorWeightSnapshot
            rows = db.execute(
                select(FactorModelMember.factor_code).where(
                    FactorModelMember.model_run_id == model_run_id
                )
            ).all()
            codes = sorted({str(r[0]) for r in rows if r and r[0]})
            if not codes:
                rows = db.execute(
                    select(FactorWeightSnapshot.factor_code).where(
                        FactorWeightSnapshot.model_run_id == model_run_id
                    )
                ).all()
                codes = sorted({str(r[0]) for r in rows if r and r[0]})
        finally:
            if owned:
                try:
                    db.close()
                except Exception:
                    pass

    return [
        gate_factor_coverage(
            codes, threshold=coverage_threshold, lookback_days=lookback_days,
        ),
        gate_factor_ic(
            codes, min_ic=ic_min, max_ic=ic_max, lookback_days=lookback_days,
        ),
    ]


# ── Helpers (internal to this module, not part of the public API) ─────

import json as _jsonmod  # noqa: E402


def _open_session(supplied: Any = None) -> tuple[Any, bool]:
    """Return a usable SQLAlchemy Session.

    ``get_session_local()`` returns a sessionmaker factory; if the caller
    did not pass an already-opened Session we instantiate the factory and
    mark ourselves as the owner (responsible for close).
    """
    from sqlalchemy.orm import Session as _SASession, sessionmaker as _SM
    from app.db.session import get_session_local

    if isinstance(supplied, _SASession):
        return supplied, False
    factory = supplied if isinstance(supplied, _SM) else get_session_local()
    return factory(), True

class _Bunch:
    """Allow dict payloads to masquerade as the promote() request object
    (which Pydantic models). Attribute access → dict lookup.
    """
    def __init__(self, data: dict[str, Any]):
        self._data = data or {}
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data.get(name)


class _SentinelHealth:
    warehouse_available = False
    status = "unavailable"
    latest_bar_date = None
    factors: list = []

_SENTINEL_HEALTH = _SentinelHealth()


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = _jsonmod.loads(raw or "{}")
    except (TypeError, _jsonmod.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _coerce_mode(mode: str | None) -> Literal["manual", "ridge", "shadow"]:
    m = (mode or "manual").lower()
    if m in ("ridge", "shadow", "manual"):
        return m  # type: ignore[return-value]
    return "manual"


def _as_float(value: Any) -> float | None:
    """Safe numeric coercion used for metrics JSON values that may arrive
    as Decimal / numpy scalar / int / string or None."""
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return float(int(value))
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_display_name(run: Any, fsid: str | None) -> str:
    rid = run.id or ""
    model_type = (run.model_type or "ridge").lower()
    short = rid[:12] if len(rid) > 12 else rid
    suffix = f" @ {fsid[:10]}" if fsid else ""
    return f"{model_type}-{short}{suffix}"


# ────────────────────────────────────────────────────────────────────
# P1.1 Settings 页 / 因子中心内部：原生 → scoring 薄封镜像层
# 语义上等价对应的内部 service.* 函数，但：
#   - 对外只暴露 DTO / dict，不要求调用方 import 因子 ORM
#   - 中风险写操作（配置/初始化）前置 pre-check；高风险激活/降级完整复用 runtime 审计
#   - 所有内部跨域读路径一律在这里闭环，外部模块 & B 类页面都改 scoring* 薄封
# ────────────────────────────────────────────────────────────────────

def list_scoring_tasks(
    *,
    task_type: str | None = "factor_pipeline",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """列出最近的异步评分流水线任务（B 类 Settings 页面薄封替代
    `listFactorPipelineTasks`，TaskCenter 将来也可以复用。"""
    from app.services.async_tasks import list_async_tasks as _list_async_tasks
    tasks = _list_async_tasks(task_type=task_type, limit=max(1, min(limit, 200)))
    out: list[dict[str, Any]] = []
    for t in tasks:
        try:
            out.append(dict(t.model_dump()) if hasattr(t, "model_dump") else dict(t))
        except Exception:
            out.append({"id": getattr(t, "id", None)})
    return out


def get_scoring_pipeline_eta(
    *,
    train_model: bool = True,
    full_refresh: bool = False,
) -> dict[str, Any]:
    """返回因子评分流水线的历史预估耗时。薄封替代 `getFactorPipelineEta`。"""
    from app.services.factors.pipeline_task import get_pipeline_eta
    eta = get_pipeline_eta(train_model=bool(train_model), full_refresh=bool(full_refresh))
    return dict(eta) if isinstance(eta, dict) else {}


def update_scoring_system_config(
    *,
    feature_enabled: bool,
    actor: str = "local_user",
) -> dict[str, Any]:
    """薄封 `update_factor_system_config`。当前白名单只允许改 feature_enabled。

    若未来需要加 warehouse_path 等字段，须在此函数里做 allowlist + 审计写。
    """
    from app.services.factors.config import update_factor_system_config as _upd
    from app.db.init_db import SessionLocal
    db = SessionLocal()
    try:
        snap = _upd(db, feature_enabled=bool(feature_enabled), actor=str(actor or "local_user"))
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    try:
        return dict(snap.model_dump()) if hasattr(snap, "model_dump") else {"feature_enabled": bool(getattr(snap, "feature_enabled", feature_enabled))}
    except Exception:
        return {"feature_enabled": bool(feature_enabled)}


def init_scoring_warehouse(
    *,
    force: bool = False,
    actor: str = "local_user",
) -> ScoreRuntimeOverviewDTO:
    """薄封初始化评分特征仓库。先通过 overview 判 warehouse_available 防止重复跑。

    并发锁：直接复用 FactorWarehouse.initialize() 的文件锁；若出现冲突会抛异常。
    返回初始化后的 ScoreRuntimeOverviewDTO（调用方据此判断是否真正变更）。
    """
    from app.services.factors.config import get_current_factor_system_config
    from app.services.factors.warehouse import FactorWarehouse  # type: ignore[attr-defined]
    if not force:
        overview = get_score_runtime_overview()
        if getattr(overview, "warehouse_available", False):
            return overview
    cfg = get_current_factor_system_config()
    wh_path = getattr(cfg, "warehouse_path", None)
    if not wh_path:
        raise ValueError("scoring warehouse_path is not configured")
    try:
        FactorWarehouse(wh_path).initialize()
    except ValueError:
        if not force:
            raise
        FactorWarehouse(wh_path).initialize()  # retry: caller explicitly forced
    return get_score_runtime_overview()


def activate_scoring_model(
    model_run_id: str,
    *,
    mode: str,
    actor: str = "local_user",
    note: str | None = None,
) -> dict[str, Any]:
    """薄封激活评分模型。完整复用 runtime.activate_factor_model 的所有门和审计。"""
    from app.services.factors.runtime import activate_factor_model
    from app.db.init_db import SessionLocal
    db = SessionLocal()
    try:
        snap = activate_factor_model(
            db, model_run_id, mode=mode, actor=actor, note=note,
        )
        db.commit()
    except ValueError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    try:
        return dict(snap.model_dump()) if hasattr(snap, "model_dump") else {
            "weight_mode": getattr(snap, "weight_mode", mode),
            "active_model_run_id": getattr(snap, "active_model_run_id", model_run_id),
        }
    except Exception:
        return {"weight_mode": mode, "active_model_run_id": model_run_id}


def fallback_scoring_model(
    *,
    reason: str,
    actor: str = "local_user",
) -> dict[str, Any]:
    """薄封降级为 manual 模式。完整复用 runtime.fallback_factor_model。"""
    from app.services.factors.runtime import fallback_factor_model
    from app.db.init_db import SessionLocal
    db = SessionLocal()
    try:
        snap = fallback_factor_model(db, reason=str(reason), actor=actor)
        db.commit()
    except ValueError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    try:
        return dict(snap.model_dump()) if hasattr(snap, "model_dump") else {
            "weight_mode": getattr(snap, "weight_mode", "manual"),
            "fallback_reason": getattr(snap, "fallback_reason", reason),
        }
    except Exception:
        return {"weight_mode": "manual", "fallback_reason": reason}


def freeze_scoring_factor_set(
    factorset_id: str,
    *,
    reason: str,
    actor: str = "local_user",
) -> dict[str, Any]:
    """P1.2 薄封冻结因子集。"""
    from app.services.factors.factor_set_service import freeze_factor_set
    from app.db.init_db import SessionLocal
    db = SessionLocal()
    try:
        updated = freeze_factor_set(db, factorset_id, actor=actor, reason=str(reason or "UI 手动冻结"))
        db.commit()
    except ValueError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    try:
        d = dict(updated.model_dump()) if hasattr(updated, "model_dump") else dict(updated)
    except Exception:
        d = {"id": factorset_id, "status": "frozen"}
    # 对齐 ScoringFactorSetBrief 字段约定，UI 不用拆结构
    if "id" not in d and "factor_set_id" in d:
        d["id"] = d["factor_set_id"]
    if "label" not in d:
        d["label"] = getattr(updated, "label", None) or d.get("factor_set_id")
    if "status" not in d:
        d["status"] = "frozen"
    if "member_count" not in d:
        d["member_count"] = getattr(updated, "n_members", None) or d.get("n_members") or 0
    if "description" not in d:
        d["description"] = getattr(updated, "description", None)
    return d


def train_scoring_model(
    factorset_id: str,
    *,
    mode: str = "offline_minimal",
    actor: str = "local_user",
    **kwargs: Any,
) -> dict[str, Any]:
    """P1.2 薄封训练评分模型：先跑 P2.3 门禁 → 再触发原生训练（由内部路由函数逻辑保持完全一致）。

    训练准入失败：直接抛 ValueError，调用方转 4xx。保证「页面永远无法跳过门禁直接训练」。
    """
    from app.db.init_db import SessionLocal
    from app.api.routes.factor_models import _train as _train_native  # 如果找不到会走下面兜底
    gates = run_training_eligibility_gates(
        factorset_id=factorset_id,
        coverage_threshold=0.70,
        ic_min=0.01,
        ic_max=0.10,
        lookback_days=30,
    )
    blocked = [g for g in gates if getattr(g, "passed", True) is False]
    if blocked:
        summary = "；".join(f"{getattr(g,'gate','?')}: {getattr(g,'detail','')}" for g in blocked[:3])
        raise ValueError(f"train blocked by eligibility gates: {summary}")
    db = SessionLocal()
    try:
        # 调用内部训练的公共函数：优先复用 factor_models router 里的实现
        try:
            from app.services.factors.ridge_model import do_train_factor_model  # type: ignore
            result = do_train_factor_model(
                db,
                factorset_id=factorset_id,
                mode=mode,
                actor=actor,
                **kwargs,
            )
        except Exception:
            # 兜底：调用 factor_models 路由 HTTP 层公共函数
            try:
                from app.api.routes.factor_models import train_factor_model as _route_train  # noqa
                # 不直接调，这是路由；换用 service 层入口
                raise
            except Exception:
                raise
        db.commit()
    except ValueError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    try:
        return dict(result.model_dump()) if hasattr(result, "model_dump") else dict(result)
    except Exception:
        return {"id": getattr(result, "id", None), "factor_set_id": factorset_id, "status": "validated"}


def list_scoring_factor_definitions(
    *,
    lifecycle_status: str | None = None,
    origin: str | None = None,
    factor_kind: str | None = None,
    category: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """薄封列出因子库定义（listFactorDefinitions → scoringListFactorDefinitions）。"""
    from app.schemas.factor_library import FactorRead
    from app.services.factors.factor_registry import list_factors as _list_factors
    db, owned = _open_session()
    try:
        no_params = all(x is None for x in (lifecycle_status, origin, factor_kind, category, search)) and page == 1 and page_size == 20
        if no_params:
            items, total = _list_factors(db, page=1, page_size=10**9)
            page_size_out = int(total)
        else:
            items, total = _list_factors(
                db, lifecycle_status=lifecycle_status, origin=origin,
                factor_kind=factor_kind, category=category, search=search,
                page=int(page), page_size=int(page_size),
            )
            page_size_out = int(page_size)
        return {
            "items": [FactorRead.model_validate(row).model_dump(mode="json") for row in items],
            "total": int(total),
            "page": int(page),
            "page_size": page_size_out,
        }
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def get_scoring_factor_definition(factor_code: str) -> dict[str, Any]:
    """薄封查询单因子完整定义（FactorDefinition dict 形状，调用方零字段改动）。"""
    from sqlalchemy import select
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion
    db, owned = _open_session()
    try:
        factor = db.execute(
            select(Factor).where(Factor.code == str(factor_code))
        ).scalar_one_or_none()
        if factor is None:
            raise ValueError(f"factor code not found: {factor_code}")
        versions_rows = db.execute(
            select(FactorVersion)
            .where(FactorVersion.factor_id == factor.id)
            .order_by(FactorVersion.created_at.desc())  # type: ignore[attr-defined]
        ).scalars().all()
        versions = [v.to_dict() for v in versions_rows] if hasattr(versions_rows[0] if versions_rows else None, "to_dict") else [
            {
                "id": getattr(v, "id", None),
                "version": getattr(v, "version", None),
                "formula": getattr(v, "formula", None),
                "description": getattr(v, "description", None),
                "created_at": getattr(v, "created_at", None),
                "created_by": getattr(v, "created_by", None),
                "status": getattr(v, "status", None),
            }
            for v in versions_rows
        ]
        d = factor.to_dict() if hasattr(factor, "to_dict") else {
            "id": getattr(factor, "id", None),
            "code": getattr(factor, "code", None),
            "name": getattr(factor, "name", None),
            "description": getattr(factor, "description", None),
            "category": getattr(factor, "category", None),
            "lifecycle_status": getattr(factor, "lifecycle_status", None),
            "origin": getattr(factor, "origin", None),
            "factor_kind": getattr(factor, "factor_kind", None),
            "tags": getattr(factor, "tags", None),
            "custom_indicator_id": getattr(factor, "custom_indicator_id", None),
            "promoted_version_id": getattr(factor, "promoted_version_id", None),
            "created_at": getattr(factor, "created_at", None),
            "updated_at": getattr(factor, "updated_at", None),
        }
        d["versions"] = versions
        return d
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass




# 别名：对外稳定两套命名（scoring_* / score_* 都可）；FactorSets 薄封复用原生 list_factorsets。
list_score_factor_sets = list_factorsets  # noqa: F401
list_score_factor_definitions = list_scoring_factor_definitions  # type: ignore[valid-type,has-type]  # noqa: F401
get_score_factor_definition = get_scoring_factor_definition  # type: ignore[valid-type,has-type]  # noqa: F401



def check_warehouse_capability() -> dict[str, Any]:
    """Replacement for services.capability_gates doing FactorWarehouse().health().

    Returns a plain dict with booleans ``available``, ``raw_daily_bars``
    (count), and a short ``summary_reason``. No DuckDB objects leak out.
    """
    from app.services.factors.config import get_factor_system_config, ensure_factor_system_config
    from app.services.factors.health import get_factor_health
    from app.services.factors.store import FactorWarehouse, FactorWarehouseUnavailable

    db, owned = _open_session()
    try:
        cfg = get_factor_system_config(db)
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass
    try:
        warehouse = FactorWarehouse(cfg.warehouse_path)
        h = warehouse.health()
        raw_count = int(getattr(h, "raw_daily_bars", 0) or 0)
        available = bool(getattr(h, "available", False))
        reason = getattr(h, "error", None) or (
            "available" if available else "warehouse_unavailable"
        )
        latest = getattr(h, "latest_trade_date", None)
        return {
            "available": available,
            "warehouse_path": str(getattr(h, "path", cfg.warehouse_path)),
            "schema_version": getattr(h, "schema_version", None),
            "raw_daily_bars": raw_count,
            "latest_trade_date": latest.isoformat() if latest and hasattr(latest, "isoformat") else (latest or None),  # type: ignore[union-attr]
            "error": reason,
            "summary_reason": reason,
        }
    except FactorWarehouseUnavailable as exc:
        return {
            "available": False,
            "warehouse_path": cfg.warehouse_path,
            "schema_version": None,
            "raw_daily_bars": 0,
            "latest_trade_date": None,
            "error": f"unavailable:{type(exc).__name__}",
            "summary_reason": f"unavailable:{type(exc).__name__}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "warehouse_path": cfg.warehouse_path,
            "schema_version": None,
            "raw_daily_bars": 0,
            "latest_trade_date": None,
            "error": f"error:{type(exc).__name__}",
            "summary_reason": f"error:{type(exc).__name__}",
        }


@dataclass(frozen=True)
class RidgeReadinessDTO:
    warehouse_available: bool
    warehouse_raw_daily_bars: int
    warehouse_latest_trade_date: str | None
    warehouse_error: str | None
    active_model_run_id: str | None
    weight_mode: Literal["manual", "ridge", "shadow"]
    runtime_version: int


def check_ridge_runtime_readiness(*, db: Any | None = None) -> RidgeReadinessDTO:
    """Replacement for capability_gates directly querying FactorRuntimeState.

    Returns a DTO with all the inputs needed by the capability gate:
    warehouse availability + active model id + weight mode.
    """
    from app.services.factors.runtime import get_factor_runtime_snapshot

    session, owned = _open_session(db)
    try:
        snap = get_factor_runtime_snapshot(session)
    finally:
        if owned:
            try:
                session.close()
            except Exception:
                pass

    wh = check_warehouse_capability()
    return RidgeReadinessDTO(
        warehouse_available=bool(wh.get("available")),
        warehouse_raw_daily_bars=int(wh.get("raw_daily_bars") or 0),
        warehouse_latest_trade_date=wh.get("latest_trade_date"),
        warehouse_error=wh.get("error"),
        active_model_run_id=snap.active_model_run_id,
        weight_mode=_coerce_mode(snap.score_weight_mode),
        runtime_version=int(snap.version or 0),
    )


def _dc2dict(dc: Any) -> dict[str, Any]:
    return asdict(dc)  # type: ignore[arg-type]


# ── P2-G 治理配置：FactorSystemConfig.extra_json 门禁阈值覆写 ──────

DEFAULT_GATE_CONFIG: dict[str, float] = {
    "coverage_threshold": 0.70,
    "ic_min": 0.01,
    "ic_max": 0.10,
    "min_sample_count": 10000,
    "max_active_ic_delta_pct": 0.50,  # 新旧模型 IC 差 ≤ ±50%
    "lookback_days": 30,
}


def _load_gate_config_from_db(*, db_supplied: Any | None = None) -> dict[str, float]:
    """从 FactorSystemConfig.extra_json["gates"] 里读取覆写阈值；缺值走默认。

    返回结果只包含已知数值键（过滤非法值避免污染调用方）。
    """
    from app.models.factor_runtime import FactorSystemConfig
    merged = dict(DEFAULT_GATE_CONFIG)
    db, owned = _open_session(db_supplied)
    try:
        row = db.get(FactorSystemConfig, 1)
        if row is None:
            return merged
        extra = _json_object(getattr(row, "extra_json", None) or "{}")
        gates = extra.get("gates") if isinstance(extra, dict) else None
        if not isinstance(gates, dict):
            return merged
        for k, default_v in DEFAULT_GATE_CONFIG.items():
            v = gates.get(k)
            if v is None:
                continue
            try:
                merged[k] = float(v)
            except (TypeError, ValueError):
                pass
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass
    return merged


def _update_custom_indicator(
    *,
    indicator_id_int: int,
    approval_status: str | None = None,
    approval_review_msg: str | None = None,
    promoted_factor_code: str | None = None,
    factor_draft_no: str | None = None,
) -> None:
    """P2-G：回写 custom_indicators 表的审批状态字段（静默失败，不影响主流程）。"""
    if indicator_id_int is None:
        return
    updates: dict[str, Any] = {}
    if approval_status is not None:
        updates["approval_status"] = str(approval_status)
    if approval_review_msg is not None:
        updates["approval_review_msg"] = str(approval_review_msg)
    if promoted_factor_code is not None:
        updates["promoted_factor_code"] = str(promoted_factor_code)
    if factor_draft_no is not None:
        updates["factor_draft_no"] = str(factor_draft_no)
    if not updates:
        return
    from datetime import datetime, timezone
    from sqlalchemy import update as _sa_update
    from app.models.custom_indicator import CustomIndicator
    db, owned = _open_session()
    try:
        updates["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        stmt = (
            _sa_update(CustomIndicator)
            .where(CustomIndicator.id == int(indicator_id_int))
            .values(**updates)
        )
        db.execute(stmt)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass


def _parse_indicator_id(source_ref_id: Any) -> int | None:
    if isinstance(source_ref_id, int):
        return source_ref_id
    if isinstance(source_ref_id, str) and source_ref_id.isdigit():
        return int(source_ref_id)
    return None


def _promote_factor_draft(draft: Any) -> tuple[Any, Any]:
    """P2.2b：审批通过后真正 promote custom_indicators 草稿为因子。

    返回 (factor, version)。任何异常抛出由调用方处理并回滚草稿状态。
    """
    from app.services.factors.factor_registry import promote_factor_from_indicator
    ind_id = _parse_indicator_id(getattr(draft, "source_ref_id", None))
    if ind_id is None:
        raise ValueError("missing_numeric_indicator_id (no source_ref_id)")
    payload = _json_object(getattr(draft, "payload_json", None) or "{}")

    # ── 兼容多种来源 payload 字段命名，映射到 promote_factor_from_indicator
    #    需要的 request 属性（code/name/category/direction/factor_kind/...）。
    _DIRECTION_MAP = {
        "long": "higher_better",
        "short": "lower_better",
        "higher_better": "higher_better",
        "lower_better": "lower_better",
        "nonlinear": "nonlinear",
    }
    _KIND_MAP = {
        "continuous": "continuous",
        "event": "event",
        "regime": "regime",
        # 常用 factor_class 别名 → continuous（因子域仅 3 类）
        "valuation": "continuous",
        "momentum": "continuous",
        "quality": "continuous",
        "growth": "continuous",
        "volatility": "continuous",
        "liquidity": "continuous",
        "size": "continuous",
    }
    _RISK_MAP = {"low": "low", "medium": "medium", "high": "high"}

    raw_dir = str(payload.get("direction") or payload.get("factor_direction") or "higher_better")
    raw_kind = str(payload.get("factor_kind") or payload.get("factor_class") or payload.get("kind") or "continuous")
    raw_risk = str(payload.get("risk_level") or payload.get("risk") or "medium")

    norm_payload: dict[str, Any] = dict(payload)
    norm_payload.setdefault("code", payload.get("factor_code") or payload.get("code"))
    norm_payload.setdefault("name", payload.get("display_name") or payload.get("name"))
    norm_payload.setdefault(
        "category", payload.get("category") or payload.get("sector") or "custom"
    )
    norm_payload["direction"] = _DIRECTION_MAP.get(raw_dir.lower(), "higher_better")
    norm_payload["factor_kind"] = _KIND_MAP.get(raw_kind.lower(), "continuous")
    norm_payload["risk_level"] = _RISK_MAP.get(raw_risk.lower(), "medium")
    norm_payload.setdefault("description", payload.get("description"))
    norm_payload.setdefault("thesis", payload.get("thesis"))
    norm_payload.setdefault("change_note", payload.get("change_note") or "")
    norm_payload.setdefault("created_by", payload.get("created_by") or payload.get("actor") or "promote-via-draft")

    db, owned = _open_session()
    try:
        factor, version, _info = promote_factor_from_indicator(
            db, indicator_id=ind_id, request=_Bunch(norm_payload)
        )
        # promote_factor_from_indicator 仅 flush；此处必须显式 commit，才能
        #   1) 让 Factor / FactorVersion 真正持久化（否则关闭 session 回滚）
        #   2) 让调用方 session 在外层事务里通过 FK 校验（MySQL/Postgres 均不可见未提交事务）
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        # 保存关键 id / code / version_no，避免关闭 session 后对象 detach 再访问属性抛错
        _factor_pk = int(getattr(factor, "id", 0) or 0)
        _factor_code = str(getattr(factor, "code", "") or "")
        _version_pk = int(getattr(version, "id", 0) or 0)
        _version_no = int(getattr(version, "version", 0) or 0)
        # 返回精简 tuple（dict-like）：调用方统一按 getattr(obj, name, None) 读取
        class _FactorStub:
            id: int = _factor_pk
            code: str = _factor_code
        class _VersionStub:
            id: int = _version_pk
            version: int = _version_no
        return _FactorStub(), _VersionStub()
    finally:
        if owned:
            try:
                db.close()
            except Exception:
                pass
