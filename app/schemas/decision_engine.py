"""Pydantic schemas for FactorUsage, StrategyExecutionSnapshot and DecisionEngine.

Q1, Q8, Q21, Q25 契约：
- 所有组合共享一个全局 active 模型（方案 A）。
- 保存并应用生成新的不可变 snapshot，effective_from = 保存时间。
- 预检只在内存中执行，不落正式库。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_JSON_TEXT_FIELDS = {
    "versions_json", "constraints_json", "reason_codes_json",
    "factor_contributions_json", "benchmark_equity_json",
    "blocking_reasons_json", "gate_result_json", "cost_config_json",
    "decision_clock_json", "member_snapshot_json", "candidate_pool_json",
    "decision_run_ids_json", "key_members_json", "rejections_trace_json",
    "exit_rules_hit_json",
}


def _coerce_json_str(v: Any) -> Any:
    """ORM stores JSON as Text(str); allow both str (DB) and dict (API)."""
    if isinstance(v, str):
        if not v:
            return None
        try:
            return json.loads(v)
        except Exception:
            return v  # best-effort：保留原值
    return v


# ──────────────────────────────────────────────────────────── 枚举
RunMode = Literal["research", "production_pit", "production_sim"]
PitMode = Literal["best_effort", "strict_pit_safe"]
SnapshotType = Literal["preflight", "save_and_apply", "task_locked"]
UniverseType = Literal["portfolio_members", "candidate_pool_union"]
SnapshotStatus = Literal["draft", "active", "deprecated", "rollback_pending"]
# DB CheckConstraint 对齐（Q25 / ck_decision_runs_run_type_3values 之后同步迁移）
DecisionRunType = Literal["backtest", "auto_simulation", "research_preflight"]
DecisionBlockingStatus = Literal[
    "READY", "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED",
    "MODEL_INACTIVE", "SCORE_STALE",
]
DecisionAction = Literal["BUY", "SELL", "HOLD", "NO_ACTION", "REJECTED", "DATA_BLOCKED"]
PitSafeFlag = Literal["PIT_SAFE", "NOT_PIT_SAFE", "UNKNOWN"]
MatchMode = Literal["NEXT_OPEN", "T_CLOSE"]


class _JsonTextCoercedBase(BaseModel):
    """任何需要把 DB 的 Text JSON 字符串解析为 dict 的 Schema 都继承本类。"""

    @field_validator("*", mode="before")
    @classmethod
    def _parse_text_json_fields(cls, v: Any, info):
        if info.field_name in _JSON_TEXT_FIELDS:
            return _coerce_json_str(v)
        return v


# ──────────────────────────────────────────────────────────── 因子绑定选项
class FactorModelOptionBrief(BaseModel):
    """可选的因子模型（只显示 active，或 research 模式下所有通过门禁的）。"""
    model_config = ConfigDict(from_attributes=True)

    factor_model_run_id: str
    factor_set_id: str | None = None
    trained_at: datetime | None = None
    gate_policy_version: str | None = None
    # G1-WP15：仅 research UI 可显示 production 门禁阈值外的模型
    passes_production_gate: bool = False
    is_global_active: bool = False
    sample_out_rank_ic: float | None = None
    sample_out_icir: float | None = None
    sample_out_coverage_pct: float | None = None
    train_window_days: int | None = None


class FactorSetOptionBrief(BaseModel):
    """可选的已冻结 FactorSet。"""
    model_config = ConfigDict(from_attributes=True)

    factor_set_id: str
    name: str
    status: str  # draft / frozen / deprecated
    member_count: int = 0
    content_hash: str | None = None
    frozen_at: datetime | None = None


class RuleOptionBrief(BaseModel):
    """可选的 PortfolioRule 版本。"""
    model_config = ConfigDict(from_attributes=True)

    rule_id: int
    rule_name: str
    rule_version: int
    has_stage_limits_complete: bool = False
    updated_at: datetime | None = None


class FactorUsageOptionsResponse(BaseModel):
    """GET /portfolios/{id}/factor-usage-options 响应。"""

    global_active_model_run_id: str | None = None
    global_weight_mode: str = "manual"
    runtime_version: int = 0
    # 可选项
    factor_models: list[FactorModelOptionBrief] = []
    factor_sets: list[FactorSetOptionBrief] = []
    portfolio_rules: list[RuleOptionBrief] = []
    defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="UI 建议默认值：当前 global active + 已绑定 rule 的默认版本",
    )
    blocking_reasons: list[str] = Field(
        default_factory=list,
        description="例如：没有全局active模型时，正式链路提示不可绑定",
    )


# ──────────────────────────────────────────────────────────── 当前绑定
class PortfolioFactorUsageRead(_JsonTextCoercedBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    portfolio_id: int
    factor_model_run_id: str
    factor_set_id: str | None = None
    rule_id: int | None = None
    rule_version: int | None = None
    run_mode: RunMode = "research"
    pit_mode: PitMode = "best_effort"
    score_sla_coverage_pct: float = 95.0
    score_sla_max_age_days: int = 1
    status: SnapshotStatus = "draft"
    rollback_target_model_run_id: str | None = None
    versions_json: dict[str, Any] | None = None
    content_hash: str
    effective_from: datetime
    effective_to: datetime | None = None
    created_by: str = "local_user"
    created_at: datetime
    updated_at: datetime


class CurrentFactorUsageResponse(BaseModel):
    """GET /portfolios/{id}/factor-usage 当前有效绑定 + 当前挂起的 snapshot 列表。"""

    current: PortfolioFactorUsageRead | None = None
    history: list[PortfolioFactorUsageRead] = Field(
        default_factory=list,
        description="最近 10 条历史（含已 deprecated），便于一键回滚",
    )
    latest_snapshot_id: str | None = Field(
        default=None,
        description="当前最新 strategy_execution_snapshots.id（save_and_apply）",
    )


# ──────────────────────────────────────────────────────────── 预检 & 保存
class FactorUsageBindRequest(BaseModel):
    """Preflight 与 Save & Apply 共用体。

    Q21(方案 A)：factor_model_run_id 必须等于全局 active 或在前端已先确认用户
    接受 research 降级。正式 production 模式下非全局 active 一律阻断。
    """
    factor_model_run_id: str
    factor_set_id: str | None = None
    rule_id: int | None = None
    rule_version: int | None = None
    run_mode: RunMode = "research"
    pit_mode: PitMode = "best_effort"
    score_sla_coverage_pct: float = 95.0
    score_sla_max_age_days: int = 1
    # 仅在 rollback 时使用（Q9）
    rollback_target_model_run_id: str | None = None
    # Q5.3: 关键成员 symbol_id 列表（snapshot 级，优先级 > portfolio 级）；None/空=继承 Portfolio
    key_members_json: list[int] | None = None


class PreflightWarning(BaseModel):
    severity: Literal["info", "warning", "blocking"]
    code: str
    message: str
    detail: dict[str, Any] | None = None


class StrategyPreflightResponse(BaseModel):
    """POST /portfolios/{id}/strategy-preflight 响应。

    只在内存中执行，不创建正式 StrategyExecutionSnapshot，
    但返回完整预检证据（Q8.3）。
    """

    ok: bool
    preflight_snapshot_hash: str
    gate_policy_version: str | None = None
    gate_result_json: dict[str, Any] | None = None
    warnings: list[PreflightWarning] = Field(default_factory=list)
    # 模拟的 snapshot：不保存到数据库，但字段与正式保存的完全一致
    snapshot_preview: dict[str, Any] = Field(default_factory=dict)
    decision_clock: dict[str, Any] = Field(
        default_factory=dict,
        description="decision_at/data_cutoff_at/execution_at Asia/Shanghai",
    )
    # Score 覆盖率 & 新鲜度门禁（Q5）
    score_coverage_pct: float | None = None
    score_max_age_days: int | None = None
    member_count: int = 0
    universe_count: int = 0
    idempotency_key: str


class SaveAndApplyResponse(BaseModel):
    """POST /portfolios/{id}/factor-usage 保存后响应。"""

    factor_usage: PortfolioFactorUsageRead
    strategy_snapshot_id: str
    snapshot_hash: str
    idempotency_key: str
    effective_from: datetime
    warnings: list[PreflightWarning] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────── DecisionRun / Evidence 查询
class DecisionEvidenceRead(_JsonTextCoercedBase):
    """GET /decision-evidence 响应。Q11/Q6：六类动作 + 子分类。"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    decision_run_id: str
    strategy_snapshot_id: str
    portfolio_id: int
    symbol_id: int
    trade_date: date
    decision_at: datetime
    data_cutoff_at: datetime
    execution_at: datetime

    action: DecisionAction  # BUY|SELL|HOLD|NO_ACTION|REJECTED|DATA_BLOCKED
    action_subtype: str | None = None

    target_position_pct: float | None = None
    min_lot_size: int = 100
    target_quantity: float | None = None

    intended_price: float | None = None
    executed_price: float | None = None
    slippage_bps: float | None = None
    rejection_reason: str | None = None
    rejection_detail: str | None = None

    score_id: int | None = None
    score_value: float | None = None
    score_rank: int | None = None
    score_published_at: datetime | None = None
    pit_safe_flag: PitSafeFlag = "UNKNOWN"

    constraints_json: Any = None
    versions_json: Any = None
    reason_codes_json: Any = None
    factor_contributions_json: Any = None

    legacy_fallback_flag: bool | None = False

    stop_loss_verified_price_source: str | None = None
    stop_loss_triggered: bool | None = False
    match_mode: MatchMode = "NEXT_OPEN"

    @field_validator("legacy_fallback_flag", mode="before")
    @classmethod
    def _coerce_legacy_fallback_flag(cls, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        return bool(v)

    @field_validator("stop_loss_triggered", mode="before")
    @classmethod
    def _coerce_stop_loss_triggered(cls, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        return bool(v)

    @field_validator("match_mode", mode="before")
    @classmethod
    def _coerce_match_mode_default(cls, v: Any) -> MatchMode:
        """None/UNKNOWN 一律退化为 NEXT_OPEN（fail-soft，不阻断路由响应）。"""
        if v is None:
            return "NEXT_OPEN"
        if v in {"NEXT_OPEN", "T_CLOSE"}:
            return v  # type: ignore[return-value]
        return "NEXT_OPEN"

    @field_validator("pit_safe_flag", mode="before")
    @classmethod
    def _coerce_pit_safe_flag(cls, v: Any) -> PitSafeFlag:
        if v is None:
            return "UNKNOWN"
        if v in {"PIT_SAFE", "NOT_PIT_SAFE", "UNKNOWN"}:
            return v  # type: ignore[return-value]
        return "UNKNOWN"

    content_hash: str


class DecisionEvidenceDetailRead(DecisionEvidenceRead):
    """Single-symbol evidence detail for an on-demand explanation drawer.

    The paginated list remains compact. This schema adds matching and execution
    fields that are needed only after a user opens one security's explanation.
    """

    target_qty_delta: float | None = None
    blocking_reason: str | None = None
    roll_forward_days: int | None = None
    rejections_trace_json: list[dict[str, Any]] = Field(default_factory=list)
    exit_rules_hit_json: list[dict[str, Any]] = Field(default_factory=list)
    manual_price_flag: bool = False
    idempotency_key: str | None = None
    created_at: datetime

    @field_validator("rejections_trace_json", "exit_rules_hit_json", mode="before")
    @classmethod
    def _coerce_trace_lists(cls, v: Any) -> list[dict[str, Any]]:
        parsed = _coerce_json_str(v)
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    @field_validator("manual_price_flag", mode="before")
    @classmethod
    def _coerce_manual_price_flag(cls, v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        return bool(v)


class DecisionRunRead(_JsonTextCoercedBase):
    """GET /decision-runs/{id} 响应。"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy_snapshot_id: str
    portfolio_id: int
    run_type: DecisionRunType
    trade_date: date
    decision_at: datetime
    data_cutoff_at: datetime
    execution_at: datetime
    run_mode: RunMode
    pit_mode: PitMode

    universe_count: int
    member_count: int
    score_count_expected: int | None = None
    score_count_actual: int | None = None
    score_coverage_pct: float | None = None
    score_max_age_days: int | None = None

    blocking_status: DecisionBlockingStatus = "READY"
    blocking_reasons_json: Any = None
    versions_json: Any = None

    idempotency_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    is_result_production_eligible: bool = True
    created_at: datetime


class DecisionEvaluateRequest(BaseModel):
    """POST /portfolios/{id}/evaluate dry-run 请求。"""
    strategy_snapshot_id: str
    trade_date: date
    run_type: DecisionRunType = "research_preflight"
    persist: bool = Field(default=False, description="True=写 DecisionRun/DecisionEvidence 表；False=dry-run 只返回")
    decision_at: datetime | None = Field(
        default=None,
        description="WP0-6 T7 审计决策时间戳。auto_simulation 门禁用此字段做 schedule 校验；None=服务器当前时间",
    )


class DecisionOrderPlanRead(BaseModel):
    """Deterministic order intent returned by the evaluate seam."""

    order_plan_id: str
    decision_run_id: str
    evidence_id: str
    symbol_id: int
    action: DecisionAction
    signal_date: date
    execution_date: date
    target_quantity: float
    direction: Literal["BUY", "SELL"] | None = None
    intended_price: float | None = None
    reason_code: str | None = None
    rejection_trace: list[dict[str, Any]] = Field(default_factory=list)


class DecisionEvaluateResponse(BaseModel):
    """POST /portfolios/{id}/evaluate 响应。"""
    decision_run_id: str
    blocking_status: str
    blocking_reasons: list[dict[str, Any]] = Field(default_factory=list)
    clock: dict[str, Any] = Field(default_factory=dict)
    score_coverage_pct: float | None = None
    score_max_age_days: int | None = None
    evidence_count: int = 0
    persisted: bool
    dry_run: bool
    # 截取前 N 条 evidence 展示（完整 evidence 通过 GET /decision-evidence?run_id=... 分页拉取）
    evidence_preview: list[DecisionEvidenceRead] = Field(default_factory=list)
    order_plans: list[DecisionOrderPlanRead] = Field(default_factory=list)
    warnings: list[PreflightWarning] = Field(default_factory=list)
