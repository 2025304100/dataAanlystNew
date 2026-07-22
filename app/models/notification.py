"""通知数据模型（WP-MSG.1）。

建立 notification 体系的数据基础，包括 6 张表：
- NotificationChannel：通知渠道（站内/WxPusher/钉钉/QQ/邮件/Webhook）
- NotificationPolicy：推送策略
- NotificationPolicyChannel：策略-渠道关联（多对多）
- NotificationOutbox：发件箱（业务事件 → 渠道消息，dispatcher 异步发送）
- NotificationDelivery：每次发送尝试的记录
- NotificationTemplate：消息模板（Markdown + 纯文本 + 变量定义）

project_memory 硬约束：
- 敏感字段用 Secret Store 引用或加密存储（config_encrypted_json）
- event_key + channel_id 唯一约束生效（部分唯一索引，status != 'sent'）
- 业务事务只写 Outbox，不直接等待第三方
- 错误消息不暴露敏感信息（last_error_message 已脱敏）
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# ── NotificationChannel 枚举 ────────────────────────────────────

CHANNEL_TYPE_IN_APP = "in_app"
CHANNEL_TYPE_WXPUSHER = "wxpusher"
CHANNEL_TYPE_DINGTALK = "dingtalk"
CHANNEL_TYPE_ONEBOT = "onebot"
CHANNEL_TYPE_EMAIL = "email"
CHANNEL_TYPE_WEBHOOK = "webhook"

CHANNEL_TYPES = (
    CHANNEL_TYPE_IN_APP,
    CHANNEL_TYPE_WXPUSHER,
    CHANNEL_TYPE_DINGTALK,
    CHANNEL_TYPE_ONEBOT,
    CHANNEL_TYPE_EMAIL,
    CHANNEL_TYPE_WEBHOOK,
)

CHANNEL_STATUS_UNCONFIGURED = "unconfigured"
CHANNEL_STATUS_PENDING_TEST = "pending_test"
CHANNEL_STATUS_TEST_SUCCESS = "test_success"
CHANNEL_STATUS_TEST_FAILED = "test_failed"
CHANNEL_STATUS_ENABLED = "enabled"
CHANNEL_STATUS_DISABLED = "disabled"

CHANNEL_STATUSES = (
    CHANNEL_STATUS_UNCONFIGURED,
    CHANNEL_STATUS_PENDING_TEST,
    CHANNEL_STATUS_TEST_SUCCESS,
    CHANNEL_STATUS_TEST_FAILED,
    CHANNEL_STATUS_ENABLED,
    CHANNEL_STATUS_DISABLED,
)

# ── NotificationPolicy 枚举 ─────────────────────────────────────

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"

SEVERITIES = (SEVERITY_INFO, SEVERITY_WARN, SEVERITY_ERROR, SEVERITY_CRITICAL)

SCOPE_ALL = "all"
SCOPE_PORTFOLIO = "portfolio"
SCOPE_WATCHLIST = "watchlist"
SCOPE_SYMBOL = "symbol"

SCOPE_TYPES = (SCOPE_ALL, SCOPE_PORTFOLIO, SCOPE_WATCHLIST, SCOPE_SYMBOL)

DELIVERY_INSTANT = "instant"
DELIVERY_DIGEST = "digest"

DELIVERY_MODES = (DELIVERY_INSTANT, DELIVERY_DIGEST)

# ── NotificationOutbox 枚举 ─────────────────────────────────────

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_SENDING = "sending"
OUTBOX_STATUS_SENT = "sent"
OUTBOX_STATUS_FAILED = "failed"
OUTBOX_STATUS_DEAD_LETTER = "dead_letter"

OUTBOX_STATUSES = (
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_SENDING,
    OUTBOX_STATUS_SENT,
    OUTBOX_STATUS_FAILED,
    OUTBOX_STATUS_DEAD_LETTER,
)

# ── NotificationDelivery 枚举 ───────────────────────────────────

DELIVERY_STATUS_SUCCESS = "success"
DELIVERY_STATUS_FAILED = "failed"
DELIVERY_STATUS_TIMEOUT = "timeout"
DELIVERY_STATUS_AUTH_FAILED = "auth_failed"
DELIVERY_STATUS_RATE_LIMITED = "rate_limited"

DELIVERY_STATUSES = (
    DELIVERY_STATUS_SUCCESS,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_TIMEOUT,
    DELIVERY_STATUS_AUTH_FAILED,
    DELIVERY_STATUS_RATE_LIMITED,
)


def _utcnow_naive() -> datetime:
    """返回无时区信息的 UTC 当前时间（与项目其它模型保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotificationChannel(Base):
    """通知渠道（WP-MSG.1）。

    一个渠道代表一个第三方推送平台（如 WxPusher/钉钉/Webhook 等）。
    配置中的敏感字段（Token/Webhook Secret/SMTP 密码）以加密形式存储于
    config_encrypted_json，前端展示使用 config_mask_json（脱敏版本）。

    状态机：unconfigured → pending_test → test_success → enabled
                            ↘ test_failed ↗
    """

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        comment="渠道名称（唯一）",
    )

    channel_type: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
        comment="渠道类型：in_app/wxpusher/dingtalk/onebot/email/webhook",
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="是否启用",
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CHANNEL_STATUS_UNCONFIGURED, index=True,
        comment="渠道状态：unconfigured/pending_test/test_success/test_failed/enabled/disabled",
    )

    config_encrypted_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="加密后的配置 JSON（含 Token/Webhook Secret/SMTP 密码等敏感字段）",
    )

    config_mask_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="脱敏后的配置 JSON，用于前端展示（如 ****1234）",
    )

    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="最近验证成功时间",
    )

    last_test_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="最近测试时间",
    )

    last_test_success: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True,
        comment="最近测试是否成功",
    )

    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="最近错误码",
    )

    last_error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="最近错误消息（脱敏，不暴露完整 Token/Secret/密码）",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationChannel(id={self.id}, name={self.name}, "
            f"channel_type={self.channel_type}, status={self.status}, "
            f"enabled={self.enabled})>"
        )


class NotificationPolicy(Base):
    """推送策略（WP-MSG.1）。

    策略将消息来源（alert/task_done/trade 等）与渠道关联，
    支持范围限定（all/portfolio/watchlist/symbol）、最低严重级别、
    即时/摘要推送、免打扰时段、冷却与去重。
    """

    __tablename__ = "notification_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="策略名称",
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="是否启用",
    )

    source_types_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment='消息来源类型列表 JSON，如 ["alert","task_done","trade"]',
    )

    min_severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SEVERITY_INFO, index=True,
        comment="最低严重级别：info/warn/error/critical",
    )

    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SCOPE_ALL, index=True,
        comment="范围类型：all/portfolio/watchlist/symbol",
    )

    scope_ids_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="范围对象 ID 列表 JSON",
    )

    delivery_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELIVERY_INSTANT,
        comment="推送模式：instant/digest",
    )

    digest_schedule: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="摘要定时（cron 格式，delivery_mode=digest 时使用）",
    )

    quiet_hours_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment='免打扰时间 JSON，如 {"start":"22:00","end":"08:00","timezone":"Asia/Shanghai","bypass_for_critical":false}',
    )

    cooldown_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="冷却分钟数（同一来源在冷却期内只发送一次）",
    )

    dedup_window_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="去重窗口分钟数",
    )

    template_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="关联模板 ID（可为空）",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationPolicy(id={self.id}, name={self.name}, "
            f"enabled={self.enabled}, min_severity={self.min_severity}, "
            f"delivery_mode={self.delivery_mode})>"
        )


class NotificationPolicyChannel(Base):
    """策略-渠道关联（WP-MSG.1）。

    多对多关联：一个策略可关联多个渠道，一个渠道可被多个策略使用。
    删除策略或渠道时级联删除关联记录（ondelete=CASCADE）。
    (policy_id, channel_id) 唯一约束防止重复关联。
    """

    __tablename__ = "notification_policy_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    policy_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_policies.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="策略 ID",
    )

    channel_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="渠道 ID",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive,
        comment="创建时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "policy_id", "channel_id",
            name="uq_notification_policy_channels_policy_channel",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationPolicyChannel(id={self.id}, "
            f"policy_id={self.policy_id}, channel_id={self.channel_id})>"
        )


class NotificationOutbox(Base):
    """发件箱（WP-MSG.1）。

    业务事务只写 Outbox，后台 dispatcher 异步发送。
    event_key + channel_id 部分唯一索引（status != 'sent'）确保
    同一业务事件重放时不向同一渠道重复发送；
    已发送（status='sent'）的记录不参与唯一约束，允许历史记录与新
    pending 记录共存（如手动重发场景）。
    """

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    event_key: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True,
        comment="业务事件键（与 channel_id 组合唯一）",
    )

    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="来源类型：alert/task_done/trade/data_expired/discovery/signal/auto_block/drawdown",
    )

    source_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="来源对象 ID（如 AlertEvent.id）",
    )

    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment='事件类型，如 "price_alert_triggered"',
    )

    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SEVERITY_INFO, index=True,
        comment="严重级别：info/warn/error/critical",
    )

    payload_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="消息内容 JSON（title/body/symbol_id/jump_url 等）",
    )

    channel_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_channels.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="目标渠道 ID",
    )

    policy_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="匹配的策略 ID",
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OUTBOX_STATUS_PENDING, index=True,
        comment="状态：pending/sending/sent/failed/dead_letter",
    )

    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="尝试次数",
    )

    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5,
        comment="最大尝试次数",
    )

    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True,
        comment="下次重试时间",
    )

    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="最近错误码",
    )

    last_error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="最近错误消息（脱敏，不暴露完整 Token/Secret/密码）",
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="发送成功时间",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    __table_args__ = (
        # 部分唯一索引：event_key + channel_id（仅 status != 'sent' 时生效）
        # SQLite 通过 sqlite_where 实现 partial unique index
        # MySQL 不支持 partial index，由 init_db.py 通过应用层检查实现
        # 已发送（status='sent'）记录不参与唯一约束，允许手动重发场景
        Index(
            "idx_no_event_channel_pending",
            "event_key", "channel_id",
            unique=True,
            sqlite_where=text("status != 'sent'"),
            postgresql_where=text("status != 'sent'"),
        ),
        Index("idx_no_status", "status"),
        Index("idx_no_source", "source_type", "source_id"),
        Index("idx_no_retry", "next_retry_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationOutbox(id={self.id}, event_key={self.event_key}, "
            f"channel_id={self.channel_id}, status={self.status}, "
            f"attempt_count={self.attempt_count})>"
        )


class NotificationDelivery(Base):
    """发送记录（WP-MSG.1）。

    每次发送尝试产生一条记录，含 HTTP 状态码、脱敏响应摘要、
    耗时、错误码等。删除 outbox 时级联删除所有 delivery（ondelete=CASCADE）。
    response_summary / error_message 已脱敏，不暴露完整敏感信息。
    """

    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    outbox_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("notification_outbox.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="发件箱记录 ID",
    )

    channel_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True,
        comment="渠道 ID",
    )

    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="第几次尝试（从 1 开始）",
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
        comment="发送状态：success/failed/timeout/auth_failed/rate_limited",
    )

    status_code: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="HTTP 状态码（如适用）",
    )

    response_summary: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="脱敏后的响应摘要（最多 500 字符）",
    )

    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="错误码",
    )

    error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="错误消息（脱敏，不暴露完整 Token/Secret/密码）",
    )

    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="耗时（毫秒）",
    )

    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="发送时间",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationDelivery(id={self.id}, outbox_id={self.outbox_id}, "
            f"attempt_number={self.attempt_number}, status={self.status}, "
            f"status_code={self.status_code})>"
        )


class NotificationTemplate(Base):
    """消息模板（WP-MSG.1）。

    支持变量替换的标题/正文模板，正文同时提供 Markdown 与纯文本版本，
    用于适配不同渠道（如邮件用 Markdown，钉钉用纯文本）。
    """

    __tablename__ = "notification_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        comment="模板名称（唯一）",
    )

    title_template: Mapped[str] = mapped_column(
        String(256), nullable=False,
        comment="标题模板（支持变量替换）",
    )

    body_template: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="正文模板（Markdown 格式）",
    )

    body_text_template: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="正文纯文本版本（用于不支持 Markdown 的渠道）",
    )

    variables_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment='变量定义 JSON，如 [{"name":"symbol","description":"标的代码"}]',
    )

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
        comment="版本号",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
        comment="是否激活",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationTemplate(id={self.id}, name={self.name}, "
            f"version={self.version}, is_active={self.is_active})>"
        )


__all__ = [
    # 模型
    "NotificationChannel",
    "NotificationPolicy",
    "NotificationPolicyChannel",
    "NotificationOutbox",
    "NotificationDelivery",
    "NotificationTemplate",
    # Channel type
    "CHANNEL_TYPE_IN_APP",
    "CHANNEL_TYPE_WXPUSHER",
    "CHANNEL_TYPE_DINGTALK",
    "CHANNEL_TYPE_ONEBOT",
    "CHANNEL_TYPE_EMAIL",
    "CHANNEL_TYPE_WEBHOOK",
    "CHANNEL_TYPES",
    # Channel status
    "CHANNEL_STATUS_UNCONFIGURED",
    "CHANNEL_STATUS_PENDING_TEST",
    "CHANNEL_STATUS_TEST_SUCCESS",
    "CHANNEL_STATUS_TEST_FAILED",
    "CHANNEL_STATUS_ENABLED",
    "CHANNEL_STATUS_DISABLED",
    "CHANNEL_STATUSES",
    # Severity
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "SEVERITY_ERROR",
    "SEVERITY_CRITICAL",
    "SEVERITIES",
    # Scope
    "SCOPE_ALL",
    "SCOPE_PORTFOLIO",
    "SCOPE_WATCHLIST",
    "SCOPE_SYMBOL",
    "SCOPE_TYPES",
    # Delivery mode
    "DELIVERY_INSTANT",
    "DELIVERY_DIGEST",
    "DELIVERY_MODES",
    # Outbox status
    "OUTBOX_STATUS_PENDING",
    "OUTBOX_STATUS_SENDING",
    "OUTBOX_STATUS_SENT",
    "OUTBOX_STATUS_FAILED",
    "OUTBOX_STATUS_DEAD_LETTER",
    "OUTBOX_STATUSES",
    # Delivery status
    "DELIVERY_STATUS_SUCCESS",
    "DELIVERY_STATUS_FAILED",
    "DELIVERY_STATUS_TIMEOUT",
    "DELIVERY_STATUS_AUTH_FAILED",
    "DELIVERY_STATUS_RATE_LIMITED",
    "DELIVERY_STATUSES",
]
