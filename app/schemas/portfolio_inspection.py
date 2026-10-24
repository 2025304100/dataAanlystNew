"""Read-only inspection evidence and explicitly confirmed data repair contracts."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


InspectionStatus = Literal[
    "PASSED", "MISSING", "STALE", "READ_ERROR", "VERSION_MISMATCH",
    "FUTURE_DATA", "NOT_DUE", "CALENDAR_UNKNOWN", "UNKNOWN", "NOT_APPLICABLE",
]


class InspectionEvidence(BaseModel):
    evidence_id: str
    symbol_id: int | None = None
    symbol: str | None = None
    check_code: str
    label: str
    status: InspectionStatus
    reason: str
    value: Any = None
    expected: Any = None
    actual_trade_date: date | None = None
    expected_trade_date: date | None = None
    trading_day_lag: int | None = None
    allowed_lag: int | None = None
    available_at: datetime | None = None
    availability_basis: str | None = None
    observed_at: datetime
    source_code: str
    source_label: str
    source_record_ref: str | None = None
    model_run_id: str | None = None
    config_id: int | None = None
    config_version: int | None = None
    affected_capabilities: list[str] = Field(default_factory=list)
    admission_relevant: bool = False
    depends_on: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class InspectionSymbol(BaseModel):
    symbol_id: int
    symbol: str
    name: str
    market: str
    asset_type: str
    scopes: list[str]
    entry_authorized: bool
    expected_trade_date: date | None = None
    actual_bar_trade_date: date | None = None
    actual_score_trade_date: date | None = None
    scoring: dict[str, Any] = Field(default_factory=dict)
    evidence: list[InspectionEvidence]


class InspectionRepairOperation(BaseModel):
    operation_id: str
    kind: Literal["sync_daily_bars", "recalculate_rule_score", "recalculate_model_score", "review_evidence"]
    label: str
    symbol_id: int | None = None
    symbol: str | None = None
    trade_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    config_id: int | None = None
    config_version: int | None = None
    config_fingerprint: str | None = None
    enabled: bool
    disabled_reason: str | None = None
    disabled_code: str | None = None
    target: str
    depends_on: list[str] = Field(default_factory=list)
    resolves_evidence_ids: list[str] = Field(default_factory=list)
    expected_effect: str


class InspectionRepairPlan(BaseModel):
    plan_id: str
    snapshot_id: str
    portfolio_id: int
    version: int = 1
    generated_at: datetime
    expires_at: datetime
    executable_count: int
    disabled_count: int
    operations: list[InspectionRepairOperation]
    confirmation_required: bool = True
    completion_criteria: list[str]
    limitations: list[str] = Field(default_factory=list)


class PortfolioInspectionResponse(BaseModel):
    portfolio_id: int
    snapshot_id: str
    version: int = 1
    generated_at: datetime
    policy_version: str
    read_only: bool = True
    usable_for_admission: bool = False
    summary: dict[str, Any]
    binding: dict[str, Any]
    calendars: list[dict[str, Any]]
    symbols: list[InspectionSymbol]
    evidence: list[InspectionEvidence]
    repair_plan: InspectionRepairPlan
    limitations: list[str]


class InspectionRepairSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    generated_at: datetime
    operation_ids: list[str] = Field(min_length=1, max_length=1000)
    confirmed: Literal[True]
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("请由本人明确确认修复范围")
        return value


class InspectionRepairRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("请由本人明确确认重试失败项")
        return value


class InspectionRepairTaskResponse(BaseModel):
    task_id: str
    portfolio_id: int
    status: str
    stage: str
    percent: float
    total: int
    processed: int
    ok_count: int
    failed_count: int
    message: str
    objects: list[dict[str, Any]]
    retryable_operation_ids: list[str]
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    can_cancel: bool = False
    recovery: dict[str, Any] = Field(default_factory=dict)
    parent_task_id: str | None = None
    reinspection: dict[str, Any] | None = None
    dimension_recovery: dict[str, Any] = Field(default_factory=dict)
    audit_refs: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    issue_ids: list[str] = Field(default_factory=list)
    report_refs: list[str] = Field(default_factory=list)
    replayed: bool = False


class InspectionRepairTaskListResponse(BaseModel):
    portfolio_id: int
    items: list[InspectionRepairTaskResponse]
    next_cursor: str | None = None
