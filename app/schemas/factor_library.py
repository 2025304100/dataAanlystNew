"""WP1-04: 因子库 Pydantic Schema。

定义因子、版本、状态迁移的请求/响应模型。
对齐 docs/专业因子库开发计划.md §WP1-04。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator


# --- Enums (as Literal types for type safety) ---

LifecycleStatus = Literal[
    "draft", "candidate", "testing", "shadow", "active",
    "quarantined", "deprecated", "rejected",
]

FactorOrigin = Literal["system", "user", "ai_assisted", "imported"]
FactorKind = Literal["continuous", "event", "regime"]
RiskLevel = Literal["low", "medium", "high"]
ValidationStatus = Literal["pending", "valid", "invalid"]
CreatedVia = Literal["manual", "template", "ai", "import"]
TransitionAction = Literal[
    "submit_candidate", "start_testing", "reject",
    "revoke_to_draft", "deprecate",
    # WP6: shadow/active/quarantine 生命周期
    "start_shadow", "activate", "quarantine", "recover_shadow",
]


# --- JSON helpers ---

def _parse_json_field(value: Any, fallback: Any) -> Any:
    """将 ORM 中的 JSON 字符串字段解析为 Python 对象。

    兼容三种输入：
    - None：返回 fallback
    - str：尝试 json.loads，失败返回 fallback
    - 已经是 dict/list：原样返回
    """
    if value is None:
        return fallback
    if isinstance(value, str):
        if not value:
            return fallback
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return fallback
    return value


# --- Read schemas ---

class FactorRead(BaseModel):
    """因子读取响应。"""

    id: int
    code: str
    name: str
    category: str
    direction: str
    status: str | None = None  # legacy
    is_active: int | None = None  # legacy
    source_type: str | None = None
    frequency: str | None = None
    default_missing_policy: str = "exclude"
    description: str | None = None
    formula_expr: str | None = None
    # WP1 lifecycle fields
    origin: str | None = None
    lifecycle_status: str | None = None
    owner: str | None = None
    thesis: str | None = None
    factor_kind: str | None = None
    asset_scope: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("asset_scope_json", "asset_scope"),
    )
    active_version_id: int | None = None
    shadow_version_id: int | None = None
    risk_level: str | None = None
    archived_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}

    @field_validator("asset_scope", mode="before")
    @classmethod
    def _parse_asset_scope(cls, v: Any) -> list[str] | None:
        parsed = _parse_json_field(v, None)
        if parsed is None:
            return None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return None


class FactorVersionRead(BaseModel):
    """因子版本读取响应。"""

    id: int
    factor_id: int
    version: int
    formula_expr: str
    params: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("params_json", "params"),
    )
    direction: str = "higher_better"
    source_mapping: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("source_mapping_json", "source_mapping"),
    )
    effective_from: datetime | None = None
    change_note: str = ""
    is_latest: int = 1
    created_at: datetime | None = None
    # WP1 fields
    formula_ast: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("formula_ast_json", "formula_ast"),
    )
    postprocess: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("postprocess_json", "postprocess"),
    )
    parameter_schema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("parameter_schema_json", "parameter_schema"),
    )
    data_dependencies: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("data_dependencies_json", "data_dependencies"),
    )
    compiler_version: str | None = None
    execution_plan_hash: str | None = None
    complexity_score: float | None = None
    created_by: str | None = None
    created_via: str | None = None
    validation_status: str | None = None
    validation_errors: list[dict[str, Any]] | None = Field(
        default=None,
        validation_alias=AliasChoices("validation_errors_json", "validation_errors"),
    )

    model_config = {"from_attributes": True, "populate_by_name": True}

    @field_validator("params", mode="before")
    @classmethod
    def _parse_params(cls, v: Any) -> dict[str, Any]:
        parsed = _parse_json_field(v, {})
        return parsed if isinstance(parsed, dict) else {}

    @field_validator("source_mapping", mode="before")
    @classmethod
    def _parse_source_mapping(cls, v: Any) -> dict[str, Any]:
        parsed = _parse_json_field(v, {})
        return parsed if isinstance(parsed, dict) else {}

    @field_validator("formula_ast", mode="before")
    @classmethod
    def _parse_formula_ast(cls, v: Any) -> dict[str, Any] | None:
        parsed = _parse_json_field(v, None)
        return parsed if isinstance(parsed, dict) else None

    @field_validator("postprocess", mode="before")
    @classmethod
    def _parse_postprocess(cls, v: Any) -> dict[str, Any] | None:
        parsed = _parse_json_field(v, None)
        return parsed if isinstance(parsed, dict) else None

    @field_validator("parameter_schema", mode="before")
    @classmethod
    def _parse_parameter_schema(cls, v: Any) -> dict[str, Any] | None:
        parsed = _parse_json_field(v, None)
        return parsed if isinstance(parsed, dict) else None

    @field_validator("data_dependencies", mode="before")
    @classmethod
    def _parse_data_dependencies(cls, v: Any) -> dict[str, Any] | None:
        parsed = _parse_json_field(v, None)
        return parsed if isinstance(parsed, dict) else None

    @field_validator("validation_errors", mode="before")
    @classmethod
    def _parse_validation_errors(cls, v: Any) -> list[dict[str, Any]] | None:
        parsed = _parse_json_field(v, None)
        if parsed is None:
            return None
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return None


class FactorReferenceRead(BaseModel):
    """因子版本引用信息。"""

    factor_code: str
    factor_version: int
    referenced_by_evaluations: int = 0
    referenced_by_factor_sets: int = 0
    referenced_by_model_runs: int = 0
    is_immutable: bool = False  # True if any references exist


class TransitionAuditRead(BaseModel):
    """状态迁移审计记录。"""

    id: int
    factor_id: int
    factor_version_id: int | None = None
    from_status: str | None = None
    to_status: str
    actor: str
    reason: str | None = None
    evidence_run_id: str | None = None
    request_id: str | None = None
    migration_note: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FactorListResponse(BaseModel):
    """因子列表分页响应。"""

    items: list[FactorRead]
    total: int
    page: int = 1
    page_size: int = 20


# --- Write schemas ---

class FactorDraftCreate(BaseModel):
    """创建因子草稿请求。"""

    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., max_length=64)
    direction: Literal["higher_better", "lower_better", "nonlinear"] = "higher_better"
    factor_kind: FactorKind = "continuous"
    description: str | None = None
    thesis: str | None = None
    owner: str | None = None
    risk_level: RiskLevel = "medium"
    asset_scope: list[str] = Field(default_factory=lambda: ["cn-stock"])
    default_missing_policy: Literal["exclude", "impute_zero", "ignore"] = "exclude"
    frequency: str | None = None

    @field_validator("code", mode="before")
    @classmethod
    def code_lowercase_snake(cls, v: str) -> str:
        if isinstance(v, str):
            return v.lower()
        return v


class FactorVersionCreate(BaseModel):
    """创建因子版本请求。"""

    formula_expr: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["higher_better", "lower_better", "nonlinear"] = "higher_better"
    source_mapping: dict[str, Any] = Field(default_factory=dict)
    postprocess: dict[str, Any] | None = None
    parameter_schema: dict[str, Any] | None = None
    change_note: str = ""
    created_by: str = "local_user"
    created_via: CreatedVia = "manual"


class FactorTransitionRequest(BaseModel):
    """因子状态迁移请求。"""

    action: TransitionAction
    actor: str = "local_user"
    reason: str | None = None
    evidence_run_id: str | None = None
    request_id: str | None = None  # 幂等键，相同 request_id 二次调用返回既有审计结果


# --- WP4-03: FactorDraft Schema（AI 草案结构化契约） ---

class FactorDraftSchema(BaseModel):
    """因子草案结构（WP4-03）。

    AI 草案服务返回此结构，前端确认后转为 FactorDraftCreate + FactorVersionCreate。
    不直接写库，必须经用户确认和 AST 校验。
    """

    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., max_length=64)
    formula_expr: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["higher_better", "lower_better", "nonlinear"] = "higher_better"
    factor_kind: FactorKind = "continuous"
    risk_level: RiskLevel = "medium"
    description: str | None = None
    thesis: str | None = None
    postprocess: dict[str, Any] | None = None
    change_note: str = ""
    origin: FactorOrigin = "ai_assisted"
    created_via: CreatedVia = "ai"
    content_hash: str | None = None
    ai_provenance: dict[str, Any] | None = None

    @field_validator("code", mode="before")
    @classmethod
    def code_lowercase_snake(cls, v: str) -> str:
        if isinstance(v, str):
            return v.lower()
        return v


class FactorDraftValidateResult(BaseModel):
    """AI 草案校验结果（WP4-03）。"""

    is_valid: bool = False
    draft: FactorDraftSchema | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    content_hash: str | None = None


# --- WP4-01: 数值指标提升 Schema ---

class CustomIndicatorPromoteRequest(BaseModel):
    """数值指标提升为候选因子请求（WP4-01）。

    将 value_type=number 的 CustomIndicator 提升为 Factor 草稿。
    boolean 指标会被拒绝。
    """

    code: str | None = Field(
        default=None,
        description="因子代码，不传则由 indicator.key 自动生成（小写蛇形）",
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    name: str | None = Field(default=None, description="因子名称，不传则用 indicator.name", max_length=128)
    category: str | None = Field(default=None, description="分类，不传则用 indicator.category", max_length=64)
    direction: Literal["higher_better", "lower_better", "nonlinear"] = "higher_better"
    factor_kind: FactorKind = "continuous"
    risk_level: RiskLevel = "medium"
    description: str | None = None
    thesis: str | None = None
    change_note: str = "promoted from custom indicator"
    created_by: str = "local_user"

    @field_validator("code", mode="before")
    @classmethod
    def code_lowercase_snake(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            return v.lower()
        return v


class CustomIndicatorPromoteResponse(BaseModel):
    """数值指标提升响应（WP4-01）。"""

    success: bool = True
    factor_id: int
    factor_code: str
    factor_version_id: int
    factor_version: int
    lifecycle_status: LifecycleStatus = "draft"
    origin: FactorOrigin = "user"
    source_mapping: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    request_id: str | None = None  # idempotency key


# --- Filter schemas ---

class FactorFilter(BaseModel):
    """因子列表筛选。"""

    lifecycle_status: str | None = None
    origin: str | None = None
    factor_kind: str | None = None
    category: str | None = None
    search: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


# --- WP2-05: 校验与预览 Schema ---

class FactorValidateRequest(BaseModel):
    """因子公式校验请求（WP2-05）。"""

    formula_expr: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["higher_better", "lower_better", "nonlinear"] = "higher_better"
    postprocess: dict[str, Any] | None = None
    source_mapping: dict[str, Any] = Field(default_factory=dict)


class FactorValidateResponse(BaseModel):
    """因子公式校验响应。"""

    is_valid: bool
    execution_plan: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    data_dependencies: dict[str, Any] | None = None


class FactorPreviewRequest(BaseModel):
    """因子公式预览请求（WP2-05）。

    预览默认选择最近完整交易日，不选择残缺横截面。
    """

    formula_expr: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["higher_better", "lower_better", "nonlinear"] = "higher_better"
    postprocess: dict[str, Any] | None = None
    source_mapping: dict[str, Any] = Field(default_factory=dict)
    trade_date: str | None = None  # None = 最近完整交易日
    symbols: list[str] | None = None  # None = 全部活跃标的
    max_symbols: int = Field(default=20, ge=1, le=100)


class FactorPreviewValueItem(BaseModel):
    """预览值条目。"""

    symbol: str
    trade_date: str
    raw_value: float | None = None
    processed_value: float | None = None
    winsorized_value: float | None = None
    normalized_value: float | None = None
    eligible: bool = False
    data_source: str | None = None
    missing_reason: str | None = None


class FactorPreviewResponse(BaseModel):
    """因子公式预览响应。"""

    is_valid: bool
    execution_plan: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)
    data_cutoff_at: str | None = None
    selected_trade_date: str | None = None
    complete_trade_day_evidence: dict[str, Any] | None = None
    data_readiness: dict[str, Any] | None = None
    data_dependencies: dict[str, Any] | None = None
    values: list[FactorPreviewValueItem] = Field(default_factory=list)
    missing_reasons: dict[str, str] = Field(default_factory=dict)
    attempted_count: int = 0
    valid_count: int = 0
    missing_count: int = 0
    coverage_rate: float = 0.0
    missing_rate: float = 0.0
    distribution: dict[str, float | None] = Field(default_factory=dict)
    outlier_count: int = 0
    elapsed_ms: float = 0.0
    data_fix_links: list[dict[str, Any]] = Field(default_factory=list)
    evaluation_supported: bool = False
    evaluation_mode: str = "continuous"
    blocking_fields: list[dict[str, Any]] = Field(default_factory=list)
    readiness_warnings: list[dict[str, Any]] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════
# WP7-01: FactorSet Schema
# ══════════════════════════════════════════════════════════

FactorSetStatus = Literal["draft", "frozen", "deprecated"]
FactorSetMemberRole = Literal["feature", "target", "regime"]
FactorSetMissingPolicy = Literal["exclude", "impute_zero", "ignore"]
FactorSetWeightConstraint = Literal["positive", "negative", "free"]


class FactorSetMemberCreate(BaseModel):
    """添加 FactorSet 成员请求。"""

    factor_id: int
    factor_version_id: int
    role: FactorSetMemberRole = "feature"
    weight_constraint: FactorSetWeightConstraint | None = "free"
    display_order: int = 0
    missing_policy: FactorSetMissingPolicy = "exclude"


class FactorSetMemberRead(BaseModel):
    """FactorSet 成员响应。"""

    id: int
    factor_set_id: str
    factor_id: int
    factor_version_id: int
    factor_code: str
    factor_version: int
    role: str
    weight_constraint: str | None
    display_order: int
    missing_policy: str
    excluded_reason: str | None = None

    model_config = {"from_attributes": True}


class FactorSetCreate(BaseModel):
    """创建 FactorSet 请求。"""

    id: str | None = None  # 不传则自动生成
    name: str
    description: str | None = None
    created_by: str = "local_user"


class FactorSetRead(BaseModel):
    """FactorSet 响应。"""

    id: str
    name: str
    description: str | None
    content_hash: str | None
    status: str
    frozen_at: datetime | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    members: list[FactorSetMemberRead] = Field(default_factory=list)
    n_members: int = 0

    model_config = {"from_attributes": True}


class FactorSetFreezeRequest(BaseModel):
    """冻结 FactorSet 请求。"""

    actor: str = "local_user"
    reason: str | None = None


__all__ = [
    "LifecycleStatus",
    "FactorOrigin",
    "FactorKind",
    "RiskLevel",
    "ValidationStatus",
    "CreatedVia",
    "TransitionAction",
    "FactorRead",
    "FactorVersionRead",
    "FactorReferenceRead",
    "TransitionAuditRead",
    "FactorListResponse",
    "FactorDraftCreate",
    "FactorVersionCreate",
    "FactorTransitionRequest",
    "FactorFilter",
    # WP2-05
    "FactorValidateRequest",
    "FactorValidateResponse",
    "FactorPreviewRequest",
    "FactorPreviewValueItem",
    "FactorPreviewResponse",
    # WP7-01
    "FactorSetStatus",
    "FactorSetMemberRole",
    "FactorSetMissingPolicy",
    "FactorSetWeightConstraint",
    "FactorSetMemberCreate",
    "FactorSetMemberRead",
    "FactorSetCreate",
    "FactorSetRead",
    "FactorSetFreezeRequest",
]
