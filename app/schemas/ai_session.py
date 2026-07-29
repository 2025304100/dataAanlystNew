"""AI 会话 Schema（WP-AI.7 会话创建 API）。

定义 POST /api/v1/ai/sessions 创建会话端点的请求/响应结构。

约束：
- 永不返回明文 Secret：响应只含消息内容与上下文摘要（已脱敏）
- 响应包含完整 required 字段，避免前端解析错误
- 兼容前端字段：message 等价于 first_message，references 等价于 context
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AiMessageRead(BaseModel):
    """AI 消息响应（对应 AIMessage 模型）。"""

    id: int
    session_id: int
    role: str
    content: str
    context_summary: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    model_used: str | None = None
    provider_used: str | None = None
    metadata_json: str | None = None
    created_at: str | None = None


class AiSessionCreateRequest(BaseModel):
    """创建 AI 会话请求。

    profile_id 未提供则使用默认 Profile（由 failover 选择）。
    first_message 提供时创建会话同时发送首条消息并返回 AI 回复。
    兼容前端字段：message 等价于 first_message，references 等价于 context。
    """

    profile_id: int | None = Field(default=None, description="AI Profile ID，未提供则用默认 Profile")
    title: str | None = Field(default=None, description="会话标题，未提供时取首条消息前 80 字")
    source_page: str | None = Field(default=None, description="来源页：discovery/research/portfolio/backtest/task")
    context: dict[str, Any] | None = Field(default=None, description="上下文（symbol_id/portfolio_id 等）")
    first_message: str | None = Field(default=None, description="可选首条消息，提供则创建会话同时发送")
    # 前端兼容字段
    message: str | None = Field(default=None, description="前端兼容：等价于 first_message")
    references: dict[str, Any] | None = Field(default=None, description="前端兼容：等价于 context")


class AiSessionCreateResponse(BaseModel):
    """创建 AI 会话响应。

    若发送首条消息则 messages 包含 user + assistant 两条；
    response 字段为 AIResponse 字典（前端兼容，未发消息时为 None）。
    """

    session_id: int
    title: str
    created_at: datetime
    messages: list[AiMessageRead] = Field(default_factory=list)
    response: dict[str, Any] | None = None


__all__ = [
    "AiMessageRead",
    "AiSessionCreateRequest",
    "AiSessionCreateResponse",
]
