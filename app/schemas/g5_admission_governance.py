"""Explicit review contracts for G5 new-admission governance."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator


class G5ImpactReview(BaseModel):
    portfolio_id: int = Field(gt=0)
    disposition: Literal["retain_existing_runtime", "requires_separate_review"]
    rationale: str = Field(min_length=10, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=50)


class G5AdmissionControlRequest(BaseModel):
    portfolio_id: int | None = Field(default=None, gt=0)
    frozen: bool
    expected_version: int = Field(ge=0)
    inventory_snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    review_note: str = Field(min_length=10, max_length=2000)
    request_key: str = Field(min_length=8, max_length=128)
    acknowledge_only_new_admission: Literal[True]
    impact_reviews: list[G5ImpactReview] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def release_requires_inventory(self):
        if not self.frozen and self.inventory_snapshot_id is None:
            raise ValueError("恢复新准入需要先复核只读盘点快照")
        return self


class G5ReportInvalidationRequest(BaseModel):
    action: Literal["invalidate", "restore"]
    expected_report_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    inventory_snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_version: int = Field(ge=0)
    review_note: str = Field(min_length=10, max_length=2000)
    request_key: str = Field(min_length=8, max_length=128)
    acknowledge_only_new_admission: Literal[True]
