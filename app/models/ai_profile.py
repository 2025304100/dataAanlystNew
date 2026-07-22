"""AI Profile 配置数据模型（WP-AI.2 多 Profile 主备降级）。

存储多个 AI 提供商配置，支持主备降级、限流、健康状态跟踪。

设计要点：
- 鉴权字段不存明文，只存 secret_key_ref 引用 Secret Store
- 与 ai_config.json（单一配置文件）兼容：init_db 迁移逻辑会自动迁移
- health_status 用于主备降级决策（unknown/healthy/degraded/down）
- daily_request_count + daily_request_reset_at 用于每日限流
- is_fallback 标记备用 Profile，主备降级时优先选择 is_fallback=True 的 Profile
- 与 init_db.py 的 schema patch 配合，支持 SQLite/MySQL 双库升级

project_memory 硬约束：
- 永不返回明文 Secret（API 响应只返回 secret_key_ref 名称）
- AI 失败不阻塞任何业务流程（try/except 包裹）
- 切换在回复中显示 provider_used 字段
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# ── Provider 枚举 ────────────────────────────────────────────

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = "ollama"
PROVIDER_AZURE = "azure"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"

PROVIDERS = (
    PROVIDER_OPENAI,
    PROVIDER_ANTHROPIC,
    PROVIDER_OLLAMA,
    PROVIDER_AZURE,
    PROVIDER_OPENAI_COMPATIBLE,
)

# ── Auth 类型 ────────────────────────────────────────────────

AUTH_BEARER = "bearer"
AUTH_API_KEY = "api_key"
AUTH_OAUTH = "oauth"
AUTH_NONE = "none"

AUTH_TYPES = (AUTH_BEARER, AUTH_API_KEY, AUTH_OAUTH, AUTH_NONE)

# ── 用途 ─────────────────────────────────────────────────────

PURPOSE_EXPLANATION = "explanation"
PURPOSE_DRAFT = "draft"
PURPOSE_CHAT = "chat"
PURPOSE_ALL = "all"

PURPOSES = (PURPOSE_EXPLANATION, PURPOSE_DRAFT, PURPOSE_CHAT, PURPOSE_ALL)

# ── 健康状态 ─────────────────────────────────────────────────

HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_DOWN = "down"

HEALTH_STATUSES = (
    HEALTH_UNKNOWN,
    HEALTH_HEALTHY,
    HEALTH_DEGRADED,
    HEALTH_DOWN,
)


def _utcnow_naive() -> datetime:
    """返回无时区信息的 UTC 当前时间（与项目其它模型保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AIProfile(Base):
    """AI Profile 配置（WP-AI.2）。

    字段说明：
        name: Profile 名称（唯一）
        provider: 提供商 openai/anthropic/ollama/azure/openai_compatible
        base_url: API 地址（如 https://api.openai.com/v1）
        model: 模型名称（如 gpt-4o-mini）
        auth_type: 鉴权方式 bearer/api_key/oauth/none
        secret_key_ref: Secret Store 中的 key 引用（不存明文）
        timeout_seconds: 请求超时秒数
        max_tokens: 最大输出 Token
        max_context_tokens: 单次上下文上限
        daily_request_limit: 每日请求量上限
        max_concurrent: 最大并发
        purpose: 用途 explanation/draft/chat/all
        priority: 优先级（0=最高）
        is_enabled: 是否启用
        is_fallback: 是否为备用（主备降级时优先选择）
        health_status: 健康状态 unknown/healthy/degraded/down
        last_health_check: 最近健康检查时间
        daily_request_count: 当日请求计数
        daily_request_reset_at: 当日计数重置时间
        created_at: 创建时间
        updated_at: 最后更新时间
    """

    __tablename__ = "ai_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        comment="Profile 名称（唯一）",
    )
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="提供商：openai/anthropic/ollama/azure/openai_compatible",
    )
    base_url: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
        comment="API 地址",
    )
    model: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="模型名称",
    )

    # 鉴权 - 不存明文，只存 secret_key 引用
    auth_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="鉴权方式：bearer/api_key/oauth/none",
    )
    secret_key_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="Secret Store 中的 key 引用（不存明文）",
    )

    # 超时和限流
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30,
        comment="请求超时秒数",
    )
    max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4096,
        comment="最大输出 Token",
    )
    max_context_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=8192,
        comment="单次上下文上限",
    )
    daily_request_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100,
        comment="每日请求量上限",
    )
    max_concurrent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3,
        comment="最大并发",
    )

    # 用途和优先级
    purpose: Mapped[str] = mapped_column(
        String(64), nullable=False, default=PURPOSE_ALL, index=True,
        comment="用途：explanation/draft/chat/all",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, index=True,
        comment="优先级（0=最高）",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True,
        comment="是否启用",
    )
    is_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="是否为备用（主备降级时优先选择）",
    )

    # 元数据
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=HEALTH_UNKNOWN, index=True,
        comment="健康状态：unknown/healthy/degraded/down",
    )
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="最近健康检查时间",
    )
    daily_request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="当日请求计数",
    )
    daily_request_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="当日计数重置时间",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive,
        comment="创建时间",
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    def __repr__(self) -> str:
        # 不暴露 secret_key_ref 实际值，仅暴露字段名（仍不返回值）
        return (
            f"<AIProfile(id={self.id}, name={self.name}, "
            f"provider={self.provider}, model={self.model}, "
            f"priority={self.priority}, is_enabled={self.is_enabled}, "
            f"health_status={self.health_status})>"
        )


__all__ = [
    "AIProfile",
    "PROVIDER_OPENAI",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OLLAMA",
    "PROVIDER_AZURE",
    "PROVIDER_OPENAI_COMPATIBLE",
    "PROVIDERS",
    "AUTH_BEARER",
    "AUTH_API_KEY",
    "AUTH_OAUTH",
    "AUTH_NONE",
    "AUTH_TYPES",
    "PURPOSE_EXPLANATION",
    "PURPOSE_DRAFT",
    "PURPOSE_CHAT",
    "PURPOSE_ALL",
    "PURPOSES",
    "HEALTH_UNKNOWN",
    "HEALTH_HEALTHY",
    "HEALTH_DEGRADED",
    "HEALTH_DOWN",
    "HEALTH_STATUSES",
]
