from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CashLedger(Base):
    __tablename__ = "cash_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    entry_type: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Float)
    balance_after: Mapped[float] = mapped_column(Float)
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class SimOrder(Base):
    __tablename__ = "sim_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    side: Mapped[str] = mapped_column(String(8), index=True)
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    quantity: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    submitted_price: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="filled", index=True)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0)
    filled_price: Mapped[float] = mapped_column(Float, default=0)
    filled_amount: Mapped[float] = mapped_column(Float, default=0)
    fee: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # WP6.1 归因字段（全部 nullable=True，向后兼容历史订单）
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio_members.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="组合成员 ID（WP6 归因：订单来自哪个成员）",
    )
    source_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="来源类型：member/scan/legacy/manual",
    )
    source_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="来源对象 ID（member_id 或 scan_result_id）",
    )
    signal_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="信号 ID（触发本次订单的信号）",
    )
    signal_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="信号快照 JSON（防止信号后续修改导致历史不可追溯）",
    )
    rule_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="规则版本 ID（买入/卖出规则版本）",
    )
    execution_mode: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment="执行模式：manual/confirm/auto",
    )
    client_order_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="客户端订单键（组合+成员+信号日期+方向+规则版本），幂等防重",
    )
    decision_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="决策快照 JSON（下单时的完整决策上下文，用于追溯）",
    )
    rejection_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="拒绝码（订单被拒绝时的原因码）",
    )
    rejection_detail: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="拒绝详情（脱敏后的拒绝说明）",
    )

    __table_args__ = (
        Index("idx_sim_orders_member", "member_id"),
        Index("idx_sim_orders_source", "source_type", "source_id"),
        Index("idx_sim_orders_signal", "signal_id"),
        Index("idx_sim_orders_client_key", "client_order_key", unique=True),
    )


class SimTrade(Base):
    __tablename__ = "sim_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sim_orders.id", ondelete="CASCADE"), index=True)
    side: Mapped[str] = mapped_column(String(8), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
