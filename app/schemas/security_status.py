"""Local historical-status files are explicit, dated inputs, never snapshots."""
from typing import Literal
from datetime import date
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class SecurityStatusFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str = Field(min_length=1, max_length=20_000_000)
    filename: str | None = Field(default=None, max_length=160)
    source_name: str = Field(min_length=2, max_length=45)
    source_reference: str = Field(min_length=3, max_length=1000)
    source_version: str = Field(min_length=1, max_length=120)
    source_updated_at: AwareDatetime

    @field_validator("filename", "source_name", "source_reference", "source_version", mode="before")
    @classmethod
    def trim_metadata(cls, value):
        return value.strip() if isinstance(value, str) else value


class SecurityStatusImportRequest(SecurityStatusFileRequest):
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_explicit_boolean_confirmation(cls, value):
        if value is not True:
            raise ValueError("请本人核对原始资料后明确确认导入。")
        return value


class SecurityStatusDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: int = Field(ge=1)
    request_id: UUID
    start_date: date
    end_date: date
    expected_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
