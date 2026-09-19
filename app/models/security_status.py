from __future__ import annotations
from datetime import date, datetime
from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class SecurityStatusDaily(Base):
    """Point-in-Time 证券状态时效表。

    每行代表某 symbol 在某 trade_date 的证券状态快照，
    用于回测/评价的次新股、ST、停牌、退市过滤判定。
    """
    __tablename__ = "security_status_daily"
    __table_args__ = (
        Index(
            "ix_security_status_daily_symbol_date",
            "symbol_id", "trade_date",
            unique=True,
        ),
        Index(
            "ix_security_status_daily_trade_date",
            "trade_date",
            unique=False,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 引用 symbols.id，外键 RESTRICT（历史状态不能因 symbol 删除就丢失）
    symbol_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("symbols.id", ondelete="RESTRICT"),
        nullable=False,
        index=False,  # 由复合索引覆盖
        comment="证券 ID，symbols.id 外键",
    )
    trade_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="状态生效交易日（历史时点，非当前日期）",
    )
    is_st: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="当日是否 ST 或 *ST",
    )
    is_suspended: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="当日是否停牌（全天不可交易）",
    )
    is_delisting_period: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="当日是否处于退市整理期",
    )
    is_listed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="当日是否仍上市（true=未摘牌，false=已正式摘牌）",
    )
    listing_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="上市日期（可能为 None，未知时 status 判定为 UNKNOWN）",
    )
    delisting_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="正式摘牌日期（None 表示未知）",
    )
    status_source: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="unknown",
        comment="状态来源：akshare / tdx / raw_universe / inferred / unknown",
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="源数据的更新时间戳",
    )
    as_of_batch_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="对应状态数据批次 ID，用于复现",
    )
