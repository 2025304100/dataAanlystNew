"""New account coverage can preserve unknown history without certifying it."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CoverageHistoryGap(BaseModel):
    trade_date_from: date
    trade_date_to: date
    reason: str = Field(min_length=10, max_length=2000)
    attempted_sources: list[str] = Field(min_length=1, max_length=30)
    evidence_refs: list[str] = Field(min_length=1, max_length=30)
    verdict: Literal["HISTORICAL_EVIDENCE_UNKNOWN"] = "HISTORICAL_EVIDENCE_UNKNOWN"
    closure_type: Literal["ARCHIVED_UNVERIFIABLE"] = "ARCHIVED_UNVERIFIABLE"

    @model_validator(mode="after")
    def valid_gap(self):
        if self.trade_date_from > self.trade_date_to:
            raise ValueError("历史缺口起点不能晚于终点")
        if any(not item.strip() for item in self.attempted_sources + self.evidence_refs):
            raise ValueError("尝试来源与证据引用不能为空")
        return self


class CoveragePreviewRequest(BaseModel):
    coverage_start_trade_date: date | None = None
    history_gaps: list[CoverageHistoryGap] = Field(default_factory=list, max_length=100)


class CoverageBlocker(BaseModel):
    code: str
    message: str
    details: Any = None


class CoveragePreview(BaseModel):
    portfolio_id: int
    scope_key: Literal["account"] = "account"
    as_of_at: datetime
    can_accept: bool
    coverage_start_trade_date: date | None
    evidence_fingerprint: str
    cursor_row_version: int
    current_generation: int
    snapshot: dict[str, Any]
    source_refs: list[dict[str, Any]]
    history_gaps: list[CoverageHistoryGap]
    blockers: list[CoverageBlocker]
    calendar_version: str | None
    policy_version: str
    runtime_enabled: Literal[False] = False
    acceptance_does_not_resume_trading: Literal[True] = True


class CoverageAcceptRequest(CoveragePreviewRequest):
    coverage_start_trade_date: date
    history_gaps: list[CoverageHistoryGap] = Field(min_length=1, max_length=100)
    expected_evidence_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_cursor_row_version: int = Field(ge=0)
    acceptance_key: str = Field(min_length=8, max_length=128)
    review_note: str = Field(min_length=10, max_length=2000)
    acknowledge_history_unknown: Literal[True]


class CoverageAccepted(BaseModel):
    portfolio_id: int
    baseline_id: str
    cursor_id: str
    generation: int
    coverage_start_trade_date: date
    verified_through_trade_date: None = None
    audit_event_id: int
    content_hash: str
    history_gaps: list[dict[str, Any]]
    idempotent: bool = False
    runtime_enabled: Literal[False] = False
    trading_resumed: Literal[False] = False
