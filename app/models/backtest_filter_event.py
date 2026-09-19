from __future__ import annotations
from datetime import date
from sqlalchemy import Date, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class BacktestFilterEvent(Base):
    """过滤/冻结/清算审计事件表。

    每条被排除、冻结、强制清算或保留的股票日都记录一行。
    唯一溯源四元组：run_id + symbol_id + trade_date + rule_code。
    """
    __tablename__ = "backtest_filter_events"
    __table_args__ = (
        Index("ix_bfe_run_date", "run_id", "trade_date"),
        Index("ix_bfe_symbol_date", "symbol_id", "trade_date"),
        Index("ix_bfe_rule_code", "rule_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="回测/评价运行 ID（backtest_runs.id）",
    )
    trade_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="对应交易日（signal_date）",
    )
    symbol_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("symbols.id", ondelete="RESTRICT"),
        nullable=False,
        comment="证券 ID（symbols.id）",
    )
    action: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="动作：include | exclude_candidate | freeze_position | force_liquidate",
    )
    rule_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="规则编码（NEW_LISTING_EXCLUDE | ST_EXCLUDE | ...），恒与 reason 对应",
    )
    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="人类可读中文原因（包含数值如 listing_age=119）",
    )
    raw_status_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        comment="当日 PIT 原始状态 JSON（is_st / is_suspended / listing_date 等快照）",
    )
    effective_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="LISTED",
        comment="生效状态：UNKNOWN | LISTED | ST | SUSPENDED | DELISTING_PERIOD | DELISTED",
    )
    price_used: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="若为清算事件则为使用的收盘价；否则为 NULL",
    )
    config_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="BacktestFilterConfig hash，用于复现",
    )
    data_batch_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="对应 security_status / market_data 的数据批次 ID",
    )
