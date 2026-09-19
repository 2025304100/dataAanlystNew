from __future__ import annotations

from pydantic import BaseModel, Field


class PortfolioCandidateCreate(BaseModel):
    symbol_id: int
    source_candidate_id: int | None = None
    source_type: str | None = None
    source_scan_run_id: int | None = None
    pool_memberships: list[str] = Field(default_factory=list)
    priority_score: float | None = None
    recommended_position_pct: float | None = None
    factor_tag: str | None = None


class PortfolioCandidateRead(PortfolioCandidateCreate):
    id: int
    portfolio_id: int
    symbol: str
    name: str
    created_at: str | None = None
    admission_snapshot: dict | None = None
