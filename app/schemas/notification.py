"""通知模块 Pydantic schemas（WP-MSG.6 / UAT-P0.1）。

定义渠道/策略/模板/发送记录的请求与响应 schema。

project_memory 硬约束：
- API/日志/导出不出现完整 Token、Webhook 签名 Secret、SMTP 密码
- 响应中渠道配置只返回 config_mask_json（脱敏版），永不出 config_encrypted_json
- 错误信息使用统一错误协议，中文文案
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── 渠道 Channels ────────────────────────────────────────────


class ChannelCreate(BaseModel):
    """创建渠道请求。

    config 为明文配置字典（含 Token/Secret/密码等敏感字段），
    由路由层加密存储到 config_encrypted_json，脱敏版本存到 config_mask_json。
    """

    name: str = Field(..., min_length=1, max_length=64)
    channel_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class ChannelUpdate(BaseModel):
    """更新渠道请求。所有字段可选，未提供的字段保持不变。"""

    name: str | None = Field(None, min_length=1, max_length=64)
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class ChannelRead(BaseModel):
    """渠道响应（脱敏）。

    注意：永不出 config_encrypted_json，仅出 config_mask_json。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    channel_type: str
    enabled: bool
    status: str
    config_mask_json: str | None = None
    verified_at: datetime | None = None
    last_test_at: datetime | None = None
    last_test_success: bool | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChannelTestResult(BaseModel):
    """渠道测试发送结果（脱敏）。"""

    success: bool
    status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    response_summary: str | None = None
    duration_ms: int = 0
    delivery_id: int | None = None


# ── 策略 Policies ────────────────────────────────────────────


class PolicyCreate(BaseModel):
    """创建策略请求。

    source_types / channel_ids / quiet_hours 以结构化形式传入，
    由路由层序列化为 *_json 字符串存储。
    """

    name: str = Field(..., min_length=1, max_length=64)
    enabled: bool = False
    source_types: list[str] = Field(default_factory=list)
    min_severity: str = "info"
    scope_type: str = "all"
    scope_ids: list[int] | None = None
    delivery_mode: str = "instant"
    digest_schedule: str | None = None
    quiet_hours: dict[str, Any] | None = None
    cooldown_minutes: int = 0
    dedup_window_minutes: int = 0
    template_id: int | None = None
    channel_ids: list[int] = Field(default_factory=list)


class PolicyUpdate(BaseModel):
    """更新策略请求。所有字段可选。"""

    name: str | None = None
    enabled: bool | None = None
    source_types: list[str] | None = None
    min_severity: str | None = None
    scope_type: str | None = None
    scope_ids: list[int] | None = None
    delivery_mode: str | None = None
    digest_schedule: str | None = None
    quiet_hours: dict[str, Any] | None = None
    cooldown_minutes: int | None = None
    dedup_window_minutes: int | None = None
    template_id: int | None = None
    channel_ids: list[int] | None = None


class PolicyRead(BaseModel):
    """策略响应。channel_ids 由关联表计算得出。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    source_types_json: str | None = None
    min_severity: str
    scope_type: str
    scope_ids_json: str | None = None
    delivery_mode: str
    digest_schedule: str | None = None
    quiet_hours_json: str | None = None
    cooldown_minutes: int
    dedup_window_minutes: int
    template_id: int | None = None
    channel_ids: list[int] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── 模板 Templates ────────────────────────────────────────────


class TemplateCreate(BaseModel):
    """创建模板请求。"""

    name: str = Field(..., min_length=1, max_length=64)
    title_template: str = Field(..., min_length=1, max_length=256)
    body_template: str
    body_text_template: str | None = None
    variables_json: str | None = None
    is_active: bool = True


class TemplateUpdate(BaseModel):
    """更新模板请求。所有字段可选。"""

    name: str | None = None
    title_template: str | None = None
    body_template: str | None = None
    body_text_template: str | None = None
    variables_json: str | None = None
    is_active: bool | None = None


class TemplateRead(BaseModel):
    """模板响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    title_template: str
    body_template: str
    body_text_template: str | None = None
    variables_json: str | None = None
    version: int
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TemplatePreviewRequest(BaseModel):
    """模板预览请求（变量替换预览）。"""

    variables: dict[str, Any] = Field(default_factory=dict)


class TemplatePreviewResponse(BaseModel):
    """模板预览响应（变量已转义，防止 Markdown/Webhook 注入）。"""

    title: str
    body: str
    body_text: str | None = None


# ── 发送记录 Deliveries / Outbox ─────────────────────────────


class DeliveryRead(BaseModel):
    """发送记录响应（response_summary / error_message 已脱敏）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    outbox_id: int
    channel_id: int
    attempt_number: int
    status: str
    status_code: int | None = None
    response_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    sent_at: datetime | None = None
    created_at: datetime | None = None


class OutboxRead(BaseModel):
    """发件箱记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event_key: str
    source_type: str
    source_id: int | None = None
    event_type: str
    severity: str
    payload_json: str | None = None
    channel_id: int
    policy_id: int | None = None
    status: str
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    sent_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "ChannelCreate",
    "ChannelUpdate",
    "ChannelRead",
    "ChannelTestResult",
    "PolicyCreate",
    "PolicyUpdate",
    "PolicyRead",
    "TemplateCreate",
    "TemplateUpdate",
    "TemplateRead",
    "TemplatePreviewRequest",
    "TemplatePreviewResponse",
    "DeliveryRead",
    "OutboxRead",
]
