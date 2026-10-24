"""Read-only contracts for portfolio inspection history.

Security IDs include their market (for example ``CN:000001``). Missing
historical indexes are represented explicitly rather than reconstructed from
notes, current positions, or the event's occurrence date.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ReviewDecision = Literal["pending", "approved", "rejected", "revoked", "unknown", "not_applicable"]
ExecutionResult = Literal[
    "not_executed", "running", "success", "partial_failure", "failed",
    "dependency_blocked", "unknown", "not_applicable",
]


class HistoryFilters(BaseModel):
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    trade_date_from: date | None = None
    trade_date_to: date | None = None
    security_ids: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    review_decision: ReviewDecision | None = None
    execution_result: ExecutionResult | None = None
    operator: str | None = None
    actor_type: str | None = None
    failure_code: str | None = None
    ai_assisted: bool | None = None
    since_last_reconciliation: bool = False
    q: str | None = None
    issue_id: str | None = None
    correlation_id: str | None = None
    report_ref: str | None = None
    report_version: str | None = None


class HistoryReference(BaseModel):
    kind: str
    ref: str
    version: str | None = None
    retention_status: str = "unknown"
    source_event_id: int
    # References are historical evidence, never an authorization for a write.
    read_only: Literal[True] = True


class HistoryObject(BaseModel):
    object_id: str
    security_id: str | None = None
    symbol_id: int | None = None
    market: str | None = None
    code: str | None = None
    name: str | None = None
    name_source: Literal["audit", "current_catalogue", "unknown"] = "unknown"
    trade_date: date | None = None
    trade_date_to: date | None = None
    security_index_status: Literal["known", "unknown", "not_applicable"] = "unknown"
    trade_date_index_status: Literal["known", "unknown", "not_applicable"] = "unknown"
    result: ExecutionResult = "unknown"
    result_basis: Literal["reinspection", "execution", "unknown"] = "unknown"
    source_event_ids: list[int] = Field(default_factory=list)
    references: list[HistoryReference] = Field(default_factory=list)


class HistoryEvent(BaseModel):
    id: int
    action: str
    source_action: str | None = None
    action_label: str
    occurred_at: datetime
    source_occurred_at: datetime | None = None
    operator_id: str
    correlation_id: str | None = None
    business_key: str | None = None
    before: Any = None
    after: Any = None
    attributes: Any = None
    note: str | None = None
    review_decision: ReviewDecision = "unknown"
    execution_result: ExecutionResult = "unknown"
    references: list[HistoryReference] = Field(default_factory=list)
    material_warnings: list[str] = Field(default_factory=list)


class HistoryBatch(BaseModel):
    batch_id: str
    portfolio_id: int
    title: str
    occurred_at: datetime
    occurred_from: datetime
    display_timezone: str = "Asia/Shanghai"
    trade_date_index_status: Literal["known", "unknown", "not_applicable"] = "unknown"
    operators: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    review_decision: ReviewDecision = "unknown"
    execution_result: ExecutionResult = "unknown"
    event_count: int = 0
    total_object_count: int = 0
    matched_object_count: int = 0
    matched_security_count: int = 0
    objects: list[HistoryObject] = Field(default_factory=list)
    matched_objects: list[HistoryObject] = Field(default_factory=list)
    references: list[HistoryReference] = Field(default_factory=list)
    index_warnings: list[str] = Field(default_factory=list)
    read_only: Literal[True] = True


class HistoryBatchDetail(HistoryBatch):
    events: list[HistoryEvent] = Field(default_factory=list)


class HistoryPage(BaseModel):
    items: list[HistoryBatch]
    total: int
    page: int
    page_size: int
    page_count: int
    has_more: bool
    snapshot_event_id: int
    filters: HistoryFilters
    relation_status: Literal["not_requested", "matched", "no_related_history", "filtered_out"]
    empty_reason: str | None = None
    source: Literal["data_governance_audit_events"] = "data_governance_audit_events"
    read_only: Literal[True] = True


class HistorySecurity(BaseModel):
    security_id: str
    market: str | None = None
    code: str | None = None
    name: str | None = None


class HistorySecurityPage(BaseModel):
    items: list[HistorySecurity]
    total: int
    read_only: Literal[True] = True


class HistoryMaterial(BaseModel):
    portfolio_id: int
    reference: HistoryReference
    status: Literal["available", "partial", "archived", "missing", "inaccessible", "unsupported"]
    material_kind: Literal["immutable_report", "immutable_baseline", "audit_snapshot", "retained_source", "reference_only"]
    title: str
    explanation: str
    data: Any = None
    missing_sources: list[str] = Field(default_factory=list)
    content_hash: str | None = None
    original_audit: Any = None
    read_only: Literal[True] = True
