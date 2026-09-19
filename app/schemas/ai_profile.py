"""AI Profile Schema（WP-AI.2 多 Profile 主备降级）。

约束：
- 永不返回明文 Secret：API 响应中 secret_value 字段被过滤，
  只返回 secret_key_ref（key 名称，不是值）
- 创建/更新时通过 secret_value 字段传入明文，但响应中不返回
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AIProfileBase(BaseModel):
    """AI Profile 基础字段。"""

    name: str = Field(..., min_length=1, max_length=64, description="Profile 名称（唯一）")
    provider: str = Field(..., max_length=64, description="提供商")
    base_url: str | None = Field(default=None, max_length=256, description="API 地址")
    model: str = Field(..., min_length=1, max_length=128, description="模型名称")
    auth_type: str | None = Field(default=None, max_length=32, description="鉴权方式")
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    max_context_tokens: int = Field(default=8192, ge=1, le=200000)
    daily_request_limit: int = Field(default=100, ge=0, le=100000)
    max_concurrent: int = Field(default=3, ge=1, le=100)
    purpose: str = Field(default="all", max_length=64)
    priority: int = Field(default=0, ge=0, le=1000)
    is_enabled: bool = True
    is_fallback: bool = False


class AIProfileCreate(AIProfileBase):
    """创建 AI Profile 请求。"""

    # 创建时可传入明文 secret（不返回）
    secret_value: str | None = Field(
        default=None,
        description="Secret 明文（仅写入 SecretStore，不返回；为空表示无鉴权）",
    )


class AIProfileUpdate(BaseModel):
    """更新 AI Profile 请求。所有字段可选。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    provider: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    auth_type: str | None = Field(default=None, max_length=32)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    max_context_tokens: int | None = Field(default=None, ge=1, le=200000)
    daily_request_limit: int | None = Field(default=None, ge=0, le=100000)
    max_concurrent: int | None = Field(default=None, ge=1, le=100)
    purpose: str | None = Field(default=None, max_length=64)
    priority: int | None = Field(default=None, ge=0, le=1000)
    is_enabled: bool | None = None
    is_fallback: bool | None = None
    # 更新时可传入明文 secret（不返回）
    secret_value: str | None = Field(
        default=None,
        description="Secret 明文（仅写入 SecretStore，不返回；None 表示不变）",
    )


class AIProfileRead(BaseModel):
    """AI Profile 响应。

    永不返回明文 Secret：
    - 不包含 secret_value 字段
    - secret_key_ref 只返回 key 名称（不是值）
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    base_url: str | None
    model: str
    auth_type: str | None
    secret_key_ref: str | None = Field(
        description="Secret Store 中的 key 名称（不返回值）"
    )
    timeout_seconds: int
    max_tokens: int
    max_context_tokens: int
    daily_request_limit: int
    max_concurrent: int
    purpose: str
    priority: int
    is_enabled: bool
    is_fallback: bool
    health_status: str
    last_health_check: str | None = None
    daily_request_count: int
    daily_request_reset_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AIProfileTestResult(BaseModel):
    """测试连接结果。"""

    success: bool
    latency_ms: int = 0
    model_info: dict | None = None
    error: str | None = None


class AIProfileUsage(BaseModel):
    """当日用量。"""

    profile_id: int
    daily_request_count: int
    daily_request_limit: int
    remaining: int
    daily_request_reset_at: str | None = None


class AIProfileHealth(BaseModel):
    """健康状态条目。"""

    id: int
    name: str
    provider: str
    model: str
    priority: int
    is_enabled: bool
    is_fallback: bool
    health_status: str
    last_health_check: str | None = None
    daily_request_count: int
    daily_request_limit: int


__all__ = [
    "AIProfileBase",
    "AIProfileCreate",
    "AIProfileUpdate",
    "AIProfileRead",
    "AIProfileTestResult",
    "AIProfileUsage",
    "AIProfileHealth",
]
