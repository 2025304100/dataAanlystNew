"""组合成员模型（WP4.1）。

成员不等于持仓：成员是组合的标的清单，持仓是实际成交记录。
同一组合同一标的只能存在一条当前有效成员关系
（partial unique index on effective_to IS NULL）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# status 取值枚举
STATUS_ACTIVE = "active"       # 正常买入
STATUS_PAUSED = "paused"       # 暂停买入（卖出规则继续风控退出）
STATUS_ARCHIVED = "archived"   # 归档（effective_to 已设置）

# execution_mode 取值枚举
EXECUTION_MANUAL = "manual"    # 手动：用户每次决定买卖
EXECUTION_CONFIRM = "confirm"  # 确认：系统给出信号，用户确认后执行
EXECUTION_AUTO = "auto"        # 自动：系统自动执行

# source_type 取值枚举
SOURCE_LEGACY_POSITION = "legacy_position"  # 持仓回填
SOURCE_CANDIDATE = "candidate"              # 候选加入
SOURCE_OBSERVATION = "observation"          # 观察项加入
SOURCE_MANUAL = "manual"                    # 手动添加


def _utcnow_naive() -> datetime:
    """返回无时区信息的 UTC 当前时间（与项目其它模型保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PortfolioMember(Base):
    """组合成员。

    成员是组合的标的清单，与持仓（Position）解耦：
    - 一个组合可以有多个成员
    - 同一标的可以是多个组合的成员
    - 成员可以有持仓也可以无持仓（无持仓成员不增加账户权益）
    - 同一组合同一标的只能存在一条当前有效成员关系（effective_to IS NULL）

    归档默认不物理删除：设置 effective_to 表示该成员关系结束。
    """

    __tablename__ = "portfolio_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 关联
    portfolio_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="组合 ID"
    )
    symbol_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="标的 ID"
    )

    # 状态
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_ACTIVE,
        comment="状态：active/paused/archived",
    )
    execution_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EXECUTION_MANUAL,
        comment="执行模式：manual/confirm/auto",
    )

    # 来源
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SOURCE_MANUAL,
        comment="来源类型：legacy_position/candidate/observation/manual",
    )
    source_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="来源对象 ID"
    )

    # 规则版本（WP6 会实现 entry_rule_version / exit_rule_version）
    entry_rule_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="入场规则版本 ID"
    )
    exit_rule_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="出场规则版本 ID"
    )

    # 生效时间范围
    effective_from: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive,
        comment="生效开始时间",
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="生效结束时间，NULL 表示当前有效",
    )

    # 锁定与优先级
    manual_lock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="手动锁定，禁止自动归档",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="优先级"
    )

    # 备注
    note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
        comment="创建时间",
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=_utcnow_naive,
        comment="最后更新时间",
    )

    __table_args__ = (
        # 部分唯一索引：同一组合同一标的只能存在一条当前有效成员关系
        # SQLite 通过 sqlite_where 实现 partial unique index
        # MySQL 不支持 partial index，需在 init_db.py 中通过生成列或应用层检查实现
        Index(
            "idx_portfolio_members_active",
            "portfolio_id", "symbol_id",
            unique=True,
            sqlite_where=text("effective_to IS NULL"),
            postgresql_where=text("effective_to IS NULL"),
        ),
        Index("idx_portfolio_members_status", "status"),
        Index("idx_portfolio_members_source", "source_type", "source_id"),
        Index("idx_portfolio_members_execution", "execution_mode"),
    )

    def __repr__(self) -> str:
        return (
            f"<PortfolioMember(id={self.id}, portfolio_id={self.portfolio_id}, "
            f"symbol_id={self.symbol_id}, status={self.status}, "
            f"execution_mode={self.execution_mode}, source_type={self.source_type})>"
        )


__all__ = [
    "PortfolioMember",
    "STATUS_ACTIVE",
    "STATUS_PAUSED",
    "STATUS_ARCHIVED",
    "EXECUTION_MANUAL",
    "EXECUTION_CONFIRM",
    "EXECUTION_AUTO",
    "SOURCE_LEGACY_POSITION",
    "SOURCE_CANDIDATE",
    "SOURCE_OBSERVATION",
    "SOURCE_MANUAL",
]
