from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
import json


class JournalCreate(BaseModel):
    portfolio_id: int
    symbol_id: int
    trade_setup_id: int | None = None
    entry_type: str
    title: str
    content: str | None = None
    subjective_view: str | None = None
    follow_system: bool = False
    outcome: str | None = None
    review_note: str | None = None
    score_id: int | None = None
    stage: str | None = None
    action: str | None = None
    actual_action: str | None = None
    review_tags: list[str] | None = None

    @field_validator("review_tags", mode="before")
    @classmethod
    def serialize_review_tags(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False)


class JournalUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    subjective_view: str | None = None
    follow_system: bool | None = None
    outcome: str | None = None
    review_note: str | None = None
    score_id: int | None = None
    stage: str | None = None
    action: str | None = None
    actual_action: str | None = None
    review_tags: list[str] | str | None = None

    @field_validator("review_tags", mode="before")
    @classmethod
    def serialize_review_tags(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False)


class JournalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    symbol_id: int
    trade_setup_id: int | None = None
    entry_type: str
    title: str
    content: str | None = None
    subjective_view: str | None = None
    follow_system: int
    outcome: str | None = None
    review_note: str | None = None
    score_id: int | None = None
    stage: str | None = None
    action: str | None = None
    actual_action: str | None = None
    review_tags: str | None = None
    created_at: datetime
    updated_at: datetime

