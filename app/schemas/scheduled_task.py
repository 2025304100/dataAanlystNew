from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


Frequency = Literal["daily", "weekly", "interval"]


class ScheduledTaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    task_type: str = Field(min_length=1, max_length=64)
    frequency: Frequency = "daily"
    time_of_day: str | None = "18:00"
    weekdays: list[int] = Field(default_factory=list)
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("time_of_day")
    @classmethod
    def validate_time_of_day(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("time_of_day must use HH:MM")
        try:
            hour, minute = (int(item) for item in parts)
        except ValueError as exc:
            raise ValueError("time_of_day must use HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("time_of_day must use HH:MM")
        return f"{hour:02d}:{minute:02d}"

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        if any(item < 0 or item > 6 for item in value):
            raise ValueError("weekdays must contain values from 0 to 6")
        return sorted(set(value))

    @model_validator(mode="after")
    def validate_frequency_fields(self):
        if self.frequency in {"daily", "weekly"} and self.time_of_day is None:
            raise ValueError("time_of_day is required for daily/weekly schedules")
        if self.frequency == "weekly" and not self.weekdays:
            raise ValueError("weekdays is required for weekly schedules")
        if self.frequency == "interval" and self.interval_minutes is None:
            raise ValueError("interval_minutes is required for interval schedules")
        return self


class ScheduledTaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    task_type: str | None = Field(default=None, min_length=1, max_length=64)
    frequency: Frequency | None = None
    time_of_day: str | None = None
    weekdays: list[int] | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=525600)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    payload: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduledTaskRead(BaseModel):
    id: int
    name: str
    task_type: str
    frequency: str
    time_of_day: str | None
    weekdays: list[int]
    interval_minutes: int | None
    timezone: str
    payload: dict[str, Any]
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    last_task_id: str | None
    last_task_status: str | None = None
    last_message: str | None = None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ScheduledTaskRunRead(BaseModel):
    id: int
    schedule_id: int
    schedule_name: str | None = None
    task_type: str | None = None
    trigger_source: str
    task_source: str | None
    task_id: str | None
    status: str
    message: str | None
    created_at: datetime
    finished_at: datetime | None = None


class ScheduledTaskDefinitionRead(BaseModel):
    task_type: str
    name: str
    description: str
    default_payload: dict[str, Any]
