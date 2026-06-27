from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    created_at: datetime
    updated_at: datetime

