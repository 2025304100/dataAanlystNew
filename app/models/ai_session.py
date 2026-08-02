"""AI 会话与审计数据模型（WP-AI.1）。

建立 AI 量化助手的三张核心数据表：
- AISession：会话（标题/来源页面/提供商/模型/状态/Token/费用）
- AIMessage：消息（角色/内容/上下文摘要/Token/耗时）
- AIActionAudit：动作审计（建议动作/预览/用户确认/最终结果）

设计原则：
- 软删除：deleted_at 字段标记删除，不物理删除会话
- 完整审计链：每条 AIMessage 可关联多条 AIActionAudit，
  记录 AI 建议 → 用户预览 → 确认/拒绝 → 最终执行结果全流程
- 状态字段：active/archived/deleted 三态机
- Token 与费用：会话级累计（total_tokens/total_cost）+ 消息级明细
  （prompt_tokens/completion_tokens/total_tokens）
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ── 会话状态枚举 ────────────────────────────────────────────────
SESSION_STATUS_ACTIVE = "active"      # 正常使用中
SESSION_STATUS_ARCHIVED = "archived"  # 归档（不再展示在活跃列表，但仍可查询）
SESSION_STATUS_DELETED = "deleted"    # 软删除（deleted_at 已设置）

SESSION_STATUSES = (SESSION_STATUS_ACTIVE, SESSION_STATUS_ARCHIVED, SESSION_STATUS_DELETED)

# ── 来源页面枚举 ────────────────────────────────────────────────
SOURCE_PAGE_DISCOVERY = "discovery"
SOURCE_PAGE_RESEARCH = "research"
SOURCE_PAGE_PORTFOLIO = "portfolio"
SOURCE_PAGE_BACKTEST = "backtest"
SOURCE_PAGE_TASK = "task"

SOURCE_PAGES = (
    SOURCE_PAGE_DISCOVERY,
    SOURCE_PAGE_RESEARCH,
    SOURCE_PAGE_PORTFOLIO,
    SOURCE_PAGE_BACKTEST,
    SOURCE_PAGE_TASK,
)

# ── 消息角色枚举 ────────────────────────────────────────────────
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"

ROLES = (ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM)

# ── 动作类型枚举 ────────────────────────────────────────────────
ACTION_DRAFT_INDICATOR = "draft_indicator"
ACTION_DRAFT_FILTER = "draft_filter"
ACTION_DRAFT_ALERT = "draft_alert"
ACTION_DRAFT_NOTE = "draft_note"
ACTION_DRAFT_REVIEW = "draft_review"
ACTION_DRAFT_ORDER = "draft_order"
ACTION_DRAFT_FACTOR = "draft_factor"  # WP4-04 新增：AI 因子草案

ACTION_TYPES = (
    ACTION_DRAFT_INDICATOR,
    ACTION_DRAFT_FILTER,
    ACTION_DRAFT_ALERT,
    ACTION_DRAFT_NOTE,
    ACTION_DRAFT_REVIEW,
    ACTION_DRAFT_ORDER,
    ACTION_DRAFT_FACTOR,
)


def _utcnow_naive() -> datetime:
    """返回无时区信息的 UTC 当前时间（与项目其它模型保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AISession(Base):
    """AI 会话（WP-AI.1）。

    一个会话代表用户与 AI 的一次连续对话上下文，可能横跨多个页面
    （如从挖掘结果页发起、在组合页继续追问）。会话本身只记录元信息
    与累计统计，详细消息存储在 AIMessage 中。

    状态机：active → archived → deleted（软删除）
    """

    __tablename__ = "ai_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    title: Mapped[str] = mapped_column(
        String(256), nullable=False, index=True,
        comment="会话标题",
    )

    source_page: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="来源页面：discovery/research/portfolio/backtest/task",
    )

    provider: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="AI 提供商：openai/anthropic/ollama",
    )

    model: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="模型名称",
    )

    profile_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="使用的 AI Profile ID",
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SESSION_STATUS_ACTIVE, index=True,
        comment="状态：active/archived/deleted",
    )

    context_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="上下文摘要（用于快速恢复会话状态）",
    )

    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="总 Token 数（所有消息累计）",
    )

    total_cost: Mapped[float | None] = mapped_column(
        Float, nullable=True,
        comment="总费用（USD）",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="软删除时间，NULL 表示未删除",
    )

    messages: Mapped[list["AIMessage"]] = relationship(
        "AIMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AIMessage.id",
    )

    __table_args__ = (
        Index("idx_ai_sessions_source", "source_page"),
        Index("idx_ai_sessions_status", "status"),
        Index("idx_ai_sessions_profile", "profile_id"),
        Index("idx_ai_sessions_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AISession(id={self.id}, title={self.title!r}, "
            f"source_page={self.source_page}, status={self.status}, "
            f"total_tokens={self.total_tokens})>"
        )


class AIMessage(Base):
    """AI 消息（WP-AI.1）。

    每条消息记录单轮对话内容、Token 消耗、响应耗时、实际使用的模型
    与提供商（与会话级配置可能不同，如降级场景）。assistant 消息
    可关联多条 AIActionAudit，记录 AI 建议的动作链。
    """

    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="会话 ID",
    )

    role: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
        comment="角色：user/assistant/system",
    )

    content: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="消息内容",
    )

    context_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="上下文摘要（用于压缩历史消息）",
    )

    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="提示 Token 数",
    )

    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="补全 Token 数",
    )

    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="总 Token 数（prompt + completion）",
    )

    latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="响应耗时毫秒",
    )

    model_used: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="实际使用的模型（可能与会话配置不同）",
    )

    provider_used: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="实际使用的提供商",
    )

    metadata_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="元数据 JSON（如 finish_reason, function_call 等附加信息）",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    session: Mapped["AISession"] = relationship("AISession", back_populates="messages")

    action_audits: Mapped[list["AIActionAudit"]] = relationship(
        "AIActionAudit",
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="AIActionAudit.id",
    )

    __table_args__ = (
        Index("idx_ai_messages_session", "session_id"),
        Index("idx_ai_messages_role", "role"),
        Index("idx_ai_messages_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AIMessage(id={self.id}, session_id={self.session_id}, "
            f"role={self.role}, total_tokens={self.total_tokens})>"
        )


class AIActionAudit(Base):
    """AI 动作审计（WP-AI.1）。

    记录 AI 建议 → 用户预览 → 确认/拒绝 → 最终执行结果完整链路。
    一条 assistant 消息可能携带多个建议动作（如同时建议添加指标 +
    创建提醒），每条动作独立审计。

    字段说明：
    - suggested_payload：AI 建议的 payload JSON（前端用于预览渲染）
    - preview_result：执行前的预览结果 JSON（dry-run）
    - user_confirmed：用户是否确认执行
    - final_result：最终执行结果 JSON（含成功/失败/产物引用）
    - rejected_reason：拒绝原因（用户拒绝时填写）
    """

    __tablename__ = "ai_action_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    message_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_messages.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="关联的 assistant 消息 ID",
    )

    action_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="动作类型：draft_indicator/draft_filter/draft_alert/"
                "draft_note/draft_review/draft_order/draft_factor",
    )

    suggested_payload: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="AI 建议的 payload JSON",
    )

    preview_result: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="预览结果 JSON（执行前 dry-run）",
    )

    user_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True,
        comment="用户是否确认执行",
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="确认/拒绝时间",
    )

    final_result: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="最终执行结果 JSON",
    )

    rejected_reason: Mapped[str | None] = mapped_column(
        String(256), nullable=True,
        comment="拒绝原因（用户拒绝时填写）",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    message: Mapped["AIMessage"] = relationship("AIMessage", back_populates="action_audits")

    __table_args__ = (
        Index("idx_ai_action_audits_message", "message_id"),
        Index("idx_ai_action_audits_type", "action_type"),
        Index("idx_ai_action_audits_confirmed", "user_confirmed"),
        Index("idx_ai_action_audits_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AIActionAudit(id={self.id}, message_id={self.message_id}, "
            f"action_type={self.action_type}, user_confirmed={self.user_confirmed})>"
        )


__all__ = [
    # 模型
    "AISession",
    "AIMessage",
    "AIActionAudit",
    # 会话状态
    "SESSION_STATUS_ACTIVE",
    "SESSION_STATUS_ARCHIVED",
    "SESSION_STATUS_DELETED",
    "SESSION_STATUSES",
    # 来源页面
    "SOURCE_PAGE_DISCOVERY",
    "SOURCE_PAGE_RESEARCH",
    "SOURCE_PAGE_PORTFOLIO",
    "SOURCE_PAGE_BACKTEST",
    "SOURCE_PAGE_TASK",
    "SOURCE_PAGES",
    # 消息角色
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLES",
    # 动作类型
    "ACTION_DRAFT_INDICATOR",
    "ACTION_DRAFT_FILTER",
    "ACTION_DRAFT_ALERT",
    "ACTION_DRAFT_NOTE",
    "ACTION_DRAFT_REVIEW",
    "ACTION_DRAFT_ORDER",
    "ACTION_DRAFT_FACTOR",
    "ACTION_TYPES",
]
