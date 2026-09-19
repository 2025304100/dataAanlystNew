"""组合复盘记录模型（WP8 绩效归因）。

复盘记录关联一次归因报告与组合，供用户记录对归因结果的解读、决策与后续行动。
与 JournalEntry 的区别：
- JournalEntry 关联单标的 + 交易设置，侧重"单笔交易复盘"
- Review 关联组合 + 时间范围 + 归因报告快照，侧重"组合级绩效复盘"

归因报告本身是即时计算的（不持久化），Review 仅保存归因快照 JSON，
确保历史复盘记录即使后续数据变化也不会失真。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Review(Base):
    """组合复盘记录（WP8）。

    一条复盘记录 = 一次归因报告快照 + 用户备注 + 时间范围。
    归因报告通过 report_snapshot_json 完整保存，读取时无需重算。
    """

    __tablename__ = "portfolio_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), index=True,
        comment="组合 ID",
    )
    # 复盘覆盖的时间范围（与归因报告的 start_date/end_date 一致）
    start_date: Mapped[date] = mapped_column(Date, comment="复盘起始日期（含）")
    end_date: Mapped[date] = mapped_column(Date, comment="复盘结束日期（含）")
    # 归因报告快照（JSON 字符串），完整保存 get_attribution_report 返回结果
    report_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="归因报告快照 JSON（get_attribution_report 返回值的序列化）",
    )
    # 用户填写的备注（对归因结果的解读、决策依据、后续行动等）
    note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="复盘备注")
    # 复盘标题（便于列表展示）
    title: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="复盘标题",
    )
    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True,
        comment="创建时间",
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=lambda: datetime.now(timezone.utc),
        comment="最后更新时间",
    )


__all__ = ["Review"]
