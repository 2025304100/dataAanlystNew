from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class AlertRuleCreate(BaseModel):
    name: str
    alert_type: str  # score_drop / data_stale / indicator_trigger / task_failed / watchlist_signal
    enabled: bool = True
    severity: str = "warn"  # info / warn / error
    config: dict | None = None
    cooldown_minutes: int = 60

    @field_validator("alert_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"score_drop", "data_stale", "indicator_trigger", "task_failed", "watchlist_signal"}
        if v not in allowed:
            raise ValueError(f"alert_type must be one of {allowed}")
        return v


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    severity: str | None = None
    config: dict | None = None
    cooldown_minutes: int | None = None


class AlertRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    alert_type: str
    enabled: int
    severity: str
    config_json: str | None = None
    last_triggered_at: datetime | None = None
    cooldown_minutes: int
    created_at: datetime
    updated_at: datetime

    @property
    def config(self) -> dict:
        if not self.config_json:
            return {}
        try:
            import json
            return json.loads(self.config_json)
        except Exception:
            return {}
