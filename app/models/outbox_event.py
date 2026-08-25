"""Outbox Event：与业务写入同库同事务，用于审计/通知/跨域事件投递。

生命周期：PENDING → SENT（成功投递 outbox 处理器）→ DEAD（失败 N 次转死信）。
save_factor_usage_atomic 必须在同一事务内写 OutboxEvent + 业务行，任一失败整体回滚。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Index, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

OUTBOX_STATUS_PENDING = "PENDING"
OUTBOX_STATUS_SENT = "SENT"
OUTBOX_STATUS_DEAD = "DEAD"
VALID_OUTBOX_STATUSES = frozenset({
    OUTBOX_STATUS_PENDING, OUTBOX_STATUS_SENT, OUTBOX_STATUS_DEAD,
})


class OutboxEvent(Base):
    """Transactional Outbox：业务写入同事务提交 → 后续异步转发。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_status_created", "status", "created_at"),
        Index("ix_outbox_correlation_id", "correlation_id"),
        CheckConstraint(
            "status IN ('PENDING','SENT','DEAD')",
            name="ck_outbox_events_status_values",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="事件类型：PORTFOLIO_FACTOR_USAGE_APPLIED / CANDIDATE_POOL_CHANGED / ILLEGAL_TRANSITION_ATTEMPT …",
    )
    payload_json: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="结构化事件载荷（与 factor_weights_hash/审计事件/订单详情对齐）",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OUTBOX_STATUS_PENDING,
        server_default=OUTBOX_STATUS_PENDING, index=True,
    )
    correlation_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="与 idempotency 记录和审计记录一致，贯穿请求/事务/下游事件全链路",
    )
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="转发失败错误栈；达到死信阈值 status=DEAD 并记录最后错误",
    )
