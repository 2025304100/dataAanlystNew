"""机会状态流转审计事件模型（WP3.1）。

记录候选→观察→组合→订单的所有状态流转，支持幂等键防双击与完整审计链。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# event_type 取值枚举
EVENT_CANDIDATE_TO_OBSERVATION = "candidate_to_observation"
EVENT_CANDIDATE_TO_PORTFOLIO = "candidate_to_portfolio"
EVENT_OBSERVATION_TO_PORTFOLIO = "observation_to_portfolio"
EVENT_EXCLUDE = "exclude"
EVENT_RESTORE = "restore"
EVENT_EXPIRE = "expire"
EVENT_MEMBER_ARCHIVE_TO_OBSERVATION = "member_archive_to_observation"

# source_type / target_type 取值
TYPE_CANDIDATE = "candidate"
TYPE_OBSERVATION = "observation"
TYPE_PORTFOLIO_MEMBER = "portfolio_member"

# actor_type 取值
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"
ACTOR_MIGRATION = "migration"


class OpportunityTransitionEvent(Base):
    """机会状态流转审计事件。

    每条记录代表一次状态流转操作：
    - 候选加入观察池
    - 候选直接加入组合
    - 观察项加入组合
    - 候选/观察项排除/恢复/过期
    - 组合成员归档后回到观察状态

    幂等键 (idempotency_key) 防止双击/重试/网络超时产生重复关系。
    相同 idempotency_key 的请求视为同一次操作，只产生一条审计记录。
    """

    __tablename__ = "opportunity_transition_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 标的关联
    symbol_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="标的 ID"
    )

    # 事件类型
    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="事件类型：candidate_to_observation/candidate_to_portfolio/"
                "observation_to_portfolio/exclude/restore/expire/"
                "member_archive_to_observation",
    )

    # 来源对象（流转的起点）
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="来源类型：candidate/observation/portfolio_member",
    )
    source_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="来源对象 ID"
    )

    # 目标对象（流转的终点，exclude/restore/expire 可能与 source 相同）
    target_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="目标类型：candidate/observation/portfolio_member",
    )
    target_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="目标对象 ID"
    )

    # 状态变化
    from_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="旧状态"
    )
    to_status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="新状态"
    )

    # 原因（JSON）
    reason_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="流转原因 JSON"
    )

    # 幂等键（唯一索引，防双击/重试）
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True,
        comment="幂等键，相同键视为同一次操作",
    )

    # 操作者
    actor_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user",
        comment="操作者类型：user/system/migration",
    )

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True, comment="事件创建时间",
    )

    __table_args__ = (
        Index("idx_ote_symbol_event", "symbol_id", "event_type"),
        Index("idx_ote_source", "source_type", "source_id"),
        Index("idx_ote_target", "target_type", "target_id"),
        Index("idx_ote_created", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityTransitionEvent(id={self.id}, "
            f"event_type={self.event_type}, symbol_id={self.symbol_id}, "
            f"source={self.source_type}:{self.source_id}, "
            f"target={self.target_type}:{self.target_id}, "
            f"idempotency_key={self.idempotency_key})>"
        )


__all__ = [
    "OpportunityTransitionEvent",
    "EVENT_CANDIDATE_TO_OBSERVATION",
    "EVENT_CANDIDATE_TO_PORTFOLIO",
    "EVENT_OBSERVATION_TO_PORTFOLIO",
    "EVENT_EXCLUDE",
    "EVENT_RESTORE",
    "EVENT_EXPIRE",
    "EVENT_MEMBER_ARCHIVE_TO_OBSERVATION",
    "TYPE_CANDIDATE",
    "TYPE_OBSERVATION",
    "TYPE_PORTFOLIO_MEMBER",
    "ACTOR_USER",
    "ACTOR_SYSTEM",
    "ACTOR_MIGRATION",
]
