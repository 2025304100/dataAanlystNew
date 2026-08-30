"""Public HTTP mirror of the factor-domain scoring facade.

Non-factor-domain frontends (TodayDecision, StrategyRules, TaskCenter,
ExternalDataSync) MUST hit these endpoints instead of the factor-domain
native ``/factor-models``, ``/factors/overview``, ``/factor-pipeline`` ones.

The factor-domain pages themselves may continue using the native routes
which expose richer internal details (weights, audit logs, versions, …).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.factors.__facade__ import (
    ExternalFactorDraftSubmission,
    FactorDraftDTO,
    FactorUsageDTO,
    FeatureInputStatusDTO,
    GovernanceGateResultDTO,
    ScoreFactorSetDTO,
    ScoreModelBriefDTO,
    ScoreModelDetailDTO,
    ScoreRuntimeOverviewDTO,
    ScoreScopeDTO,
    _dc2dict,
    approve_factor_draft,
    cancel_opaque_async_task,
    create_scoring_task,
    ensure_feature_inputs_ready,
    get_active_runtime_with_fallback_reason,
    get_factor_draft,
    get_factor_usage,
    get_model_detail,
    get_score_runtime_overview,
    get_scoring_task,
    list_factor_drafts,
    list_factorsets,
    list_score_models,
    reject_factor_draft,
    run_training_eligibility_gates,
    submit_factor_draft_from_external,
    activate_scoring_model,
    fallback_scoring_model,
    freeze_scoring_factor_set,
    get_scoring_pipeline_eta,
    init_scoring_warehouse,
    list_scoring_tasks,
    train_scoring_model,
    update_scoring_system_config,
    list_scoring_factor_definitions,
    get_scoring_factor_definition,
)


router = APIRouter()


class ExternalFactorDraftRequest(BaseModel):
    source_module: str = Field(..., min_length=1, max_length=64)
    source_ref_id: str | int = Field(...)
    actor: str = "external:untrusted"
    payload: dict = Field(default_factory=dict)


class FeatureRefreshRequest(BaseModel):
    source_hint: str = "external_data_sync"
    actor: str = "system:data_gateway"
    mirror_only: bool = True


class CreateScoringTaskRequest(BaseModel):
    """Scoring/feature-pipeline task create request (thin envelope for FactorPipelineCreate).

    P0.3 A1 baseline: date range + full_refresh + train_model + materialize + scope/actor/source_hint.
    P1.1 Settings 页增补：train_model 场景下的 factor_set_id / window_days / validation_days;
    以及可选批大小 / data_cutoff_date / mirror_only（覆盖默认 mirror_only=!materialize_scores 推断）。
    """
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    data_cutoff_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    full_refresh: bool = False
    train_model: bool = False
    materialize_scores: bool = True
    scope: str = Field(default="recent", pattern=r"^(recent|full|backfill)$")
    actor: str = "external:data_sync"
    source_hint: str = "external_data_sync"
    factor_set_id: str | None = Field(default=None, max_length=64)
    window_days: int | None = Field(default=None, ge=1, le=10000)
    validation_days: int | None = Field(default=None, ge=0, le=10000)
    batch_size: int = Field(default=50000, ge=1, le=10_000_000)
    mirror_only: bool | None = None


class DraftReviewRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=128)
    review_msg: str | None = Field(default=None, max_length=4000)


class DraftRejectRequest(BaseModel):
    reviewer: str = Field(..., min_length=1, max_length=128)
    review_msg: str = Field(..., min_length=1, max_length=4000)


class TrainingEligibilityGatesRequest(BaseModel):
    factor_codes: list[str] | None = None
    factorset_id: str | None = None
    model_run_id: str | None = None
    coverage_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    ic_min: float = Field(default=0.01, ge=0.0, le=1.0)
    ic_max: float = Field(default=0.10, ge=0.0, le=1.0)
    lookback_days: int = Field(default=30, ge=1, le=365)


# ── Strategy / Backtest dropdowns ─────────────────────────────────────

@router.get("/scoring/models", response_model=list[dict])
def api_list_score_models(
    scope: str = Query(default="any", pattern="^(any|validated|shadow|production)$"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List factor models suitable for strategy / backtest selection.

    External consumers see **only** display-oriented fields; factor-set
    internal ids, weight matrices, ORM internals are intentionally hidden.
    """
    items: list[ScoreModelBriefDTO] = list_score_models(
        scope=scope, limit=limit,  # type: ignore[arg-type]
    )
    return [_dc2dict(x) for x in items]


# ── Runtime overview for TodayDecision / dashboards ───────────────────

@router.get("/scoring/overview", response_model=dict)
def api_get_score_runtime_overview() -> dict:
    """Lightweight overview used on non-factor pages.

    Replacement for the legacy ``GET /factors/overview`` when the caller
    does not belong to the factor domain.
    """
    overview: ScoreRuntimeOverviewDTO = get_score_runtime_overview()
    return _dc2dict(overview)


# ── Active scope used by strategy / alerts / candidates ───────────────

@router.get("/scoring/active-scope", response_model=dict)
def api_get_active_score_scope() -> dict:
    """Replacement for internal ``get_active_score_scope()``."""
    scope: ScoreScopeDTO = get_active_runtime_with_fallback_reason()
    return _dc2dict(scope)


# ── Async task interface (TaskCenter / DataSync pages) ────────────────

@router.post("/scoring/tasks/{task_id}/cancel", response_model=dict)
def api_cancel_opaque_task(task_id: str) -> dict:
    """Cancel a factor-feature pipeline task by opaque id.

    The caller does not need to know about ``/factor-pipeline`` routes.
    """
    try:
        return cancel_opaque_async_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/scoring/tasks", response_model=dict)
def api_create_scoring_task(req: CreateScoringTaskRequest) -> dict:
    """P0.3 A1: Create a scoring/feature-pipeline task from a non-factor
    page (e.g. ExternalDataSync).

    Replacement for the internal ``POST /factor-pipeline/tasks`` endpoint.
    Returns an opaque AsyncTaskRead-shaped dict the caller can poll by id.
    """
    try:
        task = create_scoring_task(
            start_date=req.start_date,
            end_date=req.end_date,
            data_cutoff_date=req.data_cutoff_date,
            full_refresh=bool(req.full_refresh),
            train_model=bool(req.train_model),
            materialize_scores=bool(req.materialize_scores),
            scope=req.scope,
            actor=req.actor,
            source_hint=req.source_hint,
            factor_set_id=req.factor_set_id,
            window_days=req.window_days,
            validation_days=req.validation_days,
            batch_size=int(req.batch_size),
            mirror_only=req.mirror_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return task or {}


@router.get("/scoring/tasks/{task_id}", response_model=dict)
def api_get_scoring_task(task_id: str) -> dict:
    """P0.3 A1: Get a scoring/feature-pipeline task by opaque id.

    Replacement for the internal ``GET /factor-pipeline/tasks/{id}``.
    Returns 404 if the task is not found.
    """
    task = get_scoring_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task_not_found:{task_id}")
    return task


@router.post("/scoring/feature-inputs/ensure-ready", response_model=dict)
def api_ensure_feature_inputs_ready(req: FeatureRefreshRequest) -> dict:
    """Request factor feature inputs to be ready.

    Endpoint for ExternalDataSync / DiscoveryDataPrep. They no longer need
    to know about ``FactorPipelineCreate`` schemas or ``/factor-pipeline``
    routes.
    """
    status: FeatureInputStatusDTO = ensure_feature_inputs_ready(
        source_hint=req.source_hint,
        actor=req.actor,
        mirror_only=req.mirror_only,
    )
    return _dc2dict(status)


# ── ONLY approved inbound write path ──────────────────────────────────

@router.post("/scoring/external-draft/submit", response_model=dict)
def api_submit_external_draft(req: ExternalFactorDraftRequest) -> dict:
    """Certified inbound path for non-factor-domain modules to seed a new
    factor draft (e.g. promote a custom indicator to factor).

    Always returns a submission DTO. Mutations on factor lifecycle tables
    are the exclusive responsibility of the factor-domain pipeline; the
    caller never reaches them directly.
    """
    result: ExternalFactorDraftSubmission = submit_factor_draft_from_external(
        source_module=req.source_module,
        source_ref_id=req.source_ref_id,
        payload=req.payload or {},
        actor=req.actor,
    )
    status_code = 202
    if result.review_status == "rejected_draft":
        status_code = 422
    body = _dc2dict(result)
    if status_code != 202:
        raise HTTPException(status_code=status_code, detail=body)
    return body


# ── P1 UI Stitching: FactorSets + Model detail + Factor usage ────────

@router.get("/scoring/factorsets", response_model=list[dict])
def api_list_factorsets(
    scope: str = Query(default="any", pattern="^(any|frozen|active)$"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List factor sets consumed by the "FactorSet → which models" UI.

    scope:
      - any:    all sets
      - frozen: only sets with status=frozen (eligible for training)
      - active: only sets referenced by at least one validated model run
    """
    items: list[ScoreFactorSetDTO] = list_factorsets(scope=scope, limit=limit)  # type: ignore[arg-type]
    return [_dc2dict(x) for x in items]


@router.get("/scoring/models/{model_id}", response_model=dict)
def api_get_model_detail(model_id: str) -> dict:
    """Full model detail including factor weights (sorted by |w| desc) with
    merged factor display names + factor set label/id pair.

    Returns 404 if the run id does not exist.
    """
    detail: ScoreModelDetailDTO | None = get_model_detail(model_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Model run {model_id} not found")
    return _dc2dict(detail)


@router.get("/scoring/factors/{factor_code}", response_model=dict)
def api_get_factor_usage(factor_code: str) -> dict:
    """Factor → FactorSets + Model runs usage digest shown on the factor-hub
    drawer. Returns 404 if the factor code does not exist.
    """
    usage: FactorUsageDTO | None = get_factor_usage(factor_code)
    if usage is None:
        raise HTTPException(status_code=404, detail=f"Factor {factor_code} not found")
    return _dc2dict(usage)


# ── P2-G 治理：Factor Drafts + 审批/驳回 ──────────────────────────

@router.get("/scoring/drafts", response_model=list[dict])
def api_list_factor_drafts(
    status: str = Query(
        default="any",
        pattern="^(any|submitted|approved|rejected|applied|deleted)$",
    ),
    source_module: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Factor 草稿列表（治理页面 / 因子中心待审批抽屉）。"""
    items: list[FactorDraftDTO] = list_factor_drafts(
        status=status,  # type: ignore[arg-type]
        source_module=source_module,
        limit=limit,
    )
    return [_dc2dict(x) for x in items]


@router.get("/scoring/drafts/{draft_no}", response_model=dict)
def api_get_factor_draft(draft_no: str) -> dict:
    """按草稿号获取单条 FactorDraft 详情。"""
    item: FactorDraftDTO | None = get_factor_draft(draft_no)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Draft {draft_no} not found")
    return _dc2dict(item)


@router.post("/scoring/drafts/{draft_no}/approve", response_model=dict)
def api_approve_factor_draft(draft_no: str, req: DraftReviewRequest) -> dict:
    """审批通过因子草稿。"""
    try:
        item: FactorDraftDTO = approve_factor_draft(
            draft_no,
            reviewer=req.reviewer,
            review_msg=req.review_msg,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _dc2dict(item)


@router.post("/scoring/drafts/{draft_no}/reject", response_model=dict)
def api_reject_factor_draft(draft_no: str, req: DraftRejectRequest) -> dict:
    """审批驳回因子草稿（必须附带原因）。"""
    try:
        item: FactorDraftDTO = reject_factor_draft(
            draft_no,
            reviewer=req.reviewer,
            review_msg=req.review_msg,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _dc2dict(item)


# ── P2-G 治理：训练准入门禁（2 道） ────────────────────────────────

@router.post("/scoring/gates/training-eligibility", response_model=list[dict])
def api_training_eligibility_gates(req: TrainingEligibilityGatesRequest) -> list[dict]:
    """执行 2 道训练准入门禁（覆盖率 + IC 区间）。

    三种传参方式任选其一：
      - factor_codes：传入待训练的因子列表
      - factorset_id：按 FactorSet 取 feature 成员
      - model_run_id：对已有 model_run 做重校验（先查治理表，再回退 weight 表）
    """
    if (
        not req.factor_codes
        and not req.factorset_id
        and not req.model_run_id
    ):
        raise HTTPException(
            status_code=400,
            detail="one_of_required:factor_codes|factorset_id|model_run_id",
        )
    if req.ic_min > req.ic_max:
        raise HTTPException(
            status_code=400,
            detail="ic_min must be <= ic_max",
        )
    results: list[GovernanceGateResultDTO] = run_training_eligibility_gates(
        factor_codes=req.factor_codes,
        factorset_id=req.factorset_id,
        model_run_id=req.model_run_id,
        coverage_threshold=req.coverage_threshold,
        ic_min=req.ic_min,
        ic_max=req.ic_max,
        lookback_days=req.lookback_days,
    )
    return [_dc2dict(r) for r in results]


# ── P1.1 Settings 页：scoring 薄封镜像（任务列表 / ETA / 配置 / 初始化 / 激活 / 降级） ─

class UpdateScoringConfigRequest(BaseModel):
    feature_enabled: bool
    actor: str = Field(default="local_user", max_length=64)


class InitScoringWarehouseRequest(BaseModel):
    force: bool = False
    actor: str = Field(default="local_user", max_length=64)


class ActivateScoringModelRequest(BaseModel):
    mode: str = Field(..., pattern="^(shadow|ridge)$")
    actor: str = Field(default="local_user", max_length=64)
    note: str | None = Field(default=None, max_length=2000)


class FallbackScoringModelRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)
    actor: str = Field(default="local_user", max_length=64)


class FreezeScoringFactorSetRequest(BaseModel):
    reason: str = Field(default="UI 手动冻结", min_length=1, max_length=2000)
    actor: str = Field(default="local_user", max_length=64)


class TrainScoringModelRequest(BaseModel):
    mode: str = Field(default="offline_minimal", pattern="^(offline_minimal|warehouse)$")
    actor: str = Field(default="local_user", max_length=64)


@router.get("/scoring/tasks", response_model=list[dict])
def api_list_scoring_tasks(
    type: str = Query(default="factor_pipeline", max_length=32),  # noqa: A002 - match HTTP query name
    limit: int = Query(default=20, ge=1, le=200),
):
    """薄封列出评分/流水线任务。Settings/TaskCenter 通用。"""
    task_type_arg = type or None
    return list_scoring_tasks(task_type=task_type_arg, limit=limit)


@router.get("/scoring/pipeline/eta", response_model=dict)
def api_get_scoring_pipeline_eta(
    train_model: bool = Query(default=True),
    full_refresh: bool = Query(default=False),
):
    """薄封评分流水线 ETA。"""
    return get_scoring_pipeline_eta(train_model=train_model, full_refresh=full_refresh)


@router.post("/scoring/system/config", response_model=dict)
def api_update_scoring_config(req: UpdateScoringConfigRequest):
    """薄封更新评分系统配置（当前仅 feature_enabled）。"""
    try:
        return update_scoring_system_config(
            feature_enabled=bool(req.feature_enabled), actor=req.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scoring/warehouse/initialize", response_model=dict)
def api_init_scoring_warehouse(req: InitScoringWarehouseRequest):
    """薄封初始化评分特征仓库。重复初始化在 force=False 时自动幂等跳过。"""
    try:
        overview = init_scoring_warehouse(force=bool(req.force), actor=req.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - 初始化异常统一 500 体
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _dc2dict(overview)


@router.post("/scoring/models/{model_id}/activate", response_model=dict)
def api_activate_scoring_model(model_id: str, req: ActivateScoringModelRequest):
    """薄封激活评分模型。完整复用 runtime 切换门 + 审计。"""
    try:
        return activate_scoring_model(
            model_id, mode=req.mode, actor=req.actor, note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scoring/runtime/fallback", response_model=dict)
def api_fallback_scoring_model(req: FallbackScoringModelRequest):
    """薄封将评分运行态降级为 manual（reason 必填 + 审计留痕）。"""
    try:
        return fallback_scoring_model(reason=req.reason, actor=req.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scoring/factorsets/{factorset_id}/freeze", response_model=dict)
def api_freeze_scoring_factorset(factorset_id: str, req: FreezeScoringFactorSetRequest):
    """P1.2 薄封冻结因子集。"""
    try:
        return freeze_scoring_factor_set(
            factorset_id, reason=req.reason, actor=req.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scoring/factorsets/{factorset_id}/train", response_model=dict)
def api_train_scoring_model(factorset_id: str, req: TrainScoringModelRequest):
    """P1.2 薄封训练评分模型：Facade 层强制先跑 P2.3 门禁（覆盖率≥70% / IC∈[0.01,0.10]），保证页面无法绕过。"""
    try:
        return train_scoring_model(
            factorset_id, mode=req.mode, actor=req.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ListScoringFactorDefinitionsQuery(BaseModel):
    lifecycle_status: str | None = None
    origin: str | None = None
    factor_kind: str | None = None
    category: str | None = None
    search: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)


@router.get("/scoring/factor-definitions", response_model=dict)
def api_list_scoring_factor_definitions(
    lifecycle_status: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    factor_kind: str | None = Query(default=None),
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    """薄封列出因子库定义（listFactorDefinitions → scoringListFactorDefinitions），保留原生分页字典形状。"""
    return list_scoring_factor_definitions(
        lifecycle_status=lifecycle_status, origin=origin, factor_kind=factor_kind,
        category=category, search=search, page=page, page_size=page_size,
    )


@router.get("/scoring/factor-definitions/{factor_code}", response_model=dict)
def api_get_scoring_factor_definition(factor_code: str):
    """薄封返回单因子完整定义（含历史 versions 列表）。"""
    try:
        return get_scoring_factor_definition(factor_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
