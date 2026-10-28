"""Public input contracts; scores and empirical probabilities are distinct."""
from typing import Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SignalRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    target_type: Literal["sector", "symbol"]
    target_key: str = Field(min_length=1, max_length=64)
    quote_key: str | None = Field(default=None, max_length=32)
    quote_type: Literal["index", "symbol"] = "index"
    min_score: int = Field(default=85, ge=0, le=100)
    window_days: Literal[1, 3, 5] = 3
    min_move_pct: float = Field(default=2, gt=0, le=50)
    adaptive_threshold: bool = True
    volatility_percentile: int = Field(default=80, ge=50, le=99)
    max_age_hours: int = Field(default=24, ge=1, le=168)
    cooldown_hours: int = Field(default=6, ge=0, le=168)
    max_daily: int = Field(default=1, ge=1, le=10)
    delivery_mode: Literal["instant", "digest"] = "instant"
    digest_time: str = "16:10"
    mode: Literal["shadow", "trial"] = "shadow"
    enabled: bool = True
    channel_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("digest_time")
    @classmethod
    def valid_time(cls, value):
        from datetime import time
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("汇总时间须为 HH:MM") from exc
        if parsed.second or parsed.tzinfo or len(value) != 5:
            raise ValueError("汇总时间须为 HH:MM")
        return value

    @model_validator(mode="after")
    def quote_for_sector(self):
        if self.target_type == "sector" and not self.quote_key:
            raise ValueError("请选择用于验证该板块的指数代码")
        if any(value <= 0 for value in self.channel_ids):
            raise ValueError("通知渠道 ID 必须为正整数")
        self.channel_ids = list(dict.fromkeys(self.channel_ids))
        return self


class SignalRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = None
    target_type: Literal["sector", "symbol"] | None = None
    target_key: str | None = None
    quote_key: str | None = None
    quote_type: Literal["index", "symbol"] | None = None
    min_score: int | None = None
    window_days: int | None = None
    min_move_pct: float | None = None
    adaptive_threshold: bool | None = None
    volatility_percentile: int | None = None
    max_age_hours: int | None = None
    cooldown_hours: int | None = None
    max_daily: int | None = None
    delivery_mode: str | None = None
    digest_time: str | None = None
    mode: str | None = None
    enabled: bool | None = None
    channel_ids: list[int] | None = None


class SignalEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=6, max_length=300)
    text: str = Field(min_length=6, max_length=40000)
    source_url: str | None = Field(default=None, max_length=2000)
    published_at: datetime | None = None
    kind: Literal["news", "calendar"] = "news"
    sector_ids: list[str] = Field(default_factory=list, max_length=10)
    symbol_codes: list[str] = Field(default_factory=list, max_length=30)


class SignalSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    ai_profile_id: int | None = Field(default=None, gt=0)
    global_daily_limit: int | None = Field(default=None, ge=1, le=20)
    max_analysis_per_run: int | None = Field(default=None, ge=1, le=30)
    auto_analyze: bool | None = None
    collect_on_monitor: bool | None = None


class SignalAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_ids: list[int] = Field(default_factory=list, max_length=30)
    rule_ids: list[int] = Field(default_factory=list, max_length=30)
    retry_failed: bool = False


class GroundedSignalAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_identity: str = Field(min_length=3, max_length=160)
    new_facts: list[str] = Field(max_length=8)
    event_type: Literal["policy", "supply", "demand", "cost", "regulation", "corporate", "macro", "other"]
    relation: Literal["direct", "transmission", "mention", "unrelated"]
    mechanism: str = Field(max_length=1500)
    material_points: Literal[0, 10, 20, 30, 40]
    novelty_points: Literal[0, 10, 20, 30]
    breadth_points: Literal[0, 5, 10]
    duration_points: Literal[0, 5, 10]
    evidence_quotes: list[str] = Field(min_length=1, max_length=8)
    missing: list[str] = Field(default_factory=list, max_length=8)
    same_as_assessment_id: int | None = None


class SignalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analyze: bool = False


class SignalJobRequest(SignalAnalyzeRequest):
    kind: Literal["monitor", "analyze", "sync_data", "import_legacy", "enrich"] = "monitor"
    analyze: bool = True
    evidence_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_enrichment_target(self):
        if self.kind == "enrich" and self.evidence_id is None:
            raise ValueError("提取原文必须指定消息")
        if any(value <= 0 for value in [*self.rule_ids, *self.evidence_ids]):
            raise ValueError("规则与证据 ID 必须为正整数")
        return self
