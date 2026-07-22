from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    run_name: Mapped[str] = mapped_column(String(128))
    symbols_json: Mapped[str] = mapped_column(Text)
    rule_config_json: Mapped[str] = mapped_column(Text)
    cost_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_weight_mode: Mapped[str] = mapped_column(
        String(16), default='manual', index=True
    )
    factor_model_run_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    factor_data_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    initial_capital: Mapped[float] = mapped_column(Float)
    total_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_holding_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity_curve_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # WP7.2 回测快照字段（全部 nullable=True，向后兼容历史回测）
    # 注：cost_config_json / factor_model_run_id / factor_data_cutoff_at 已存在，
    # 此处仅新增缺失的快照字段。
    member_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="成员快照 JSON：[{member_id, symbol_id, effective_from, effective_to, execution_mode, entry_rule_version_id, exit_rule_version_id}]",
    )
    symbol_ids_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="标的 ID 列表 JSON",
    )
    excluded_members_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="运行时排除的成员及原因 JSON：[{member_id, symbol_id, reason}]",
    )
    portfolio_rule_version_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="组合风控版本 ID",
    )
    score_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="评分模式：quality/timing/combined",
    )
    data_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="数据截止时间",
    )
    engine_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="引擎名称：event_driven/vectorbt",
    )
    engine_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="引擎版本",
    )
    source_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="来源类型：member/legacy_scan",
    )


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    entry_date: Mapped[date] = mapped_column(Date)
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hold_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_cost: Mapped[float] = mapped_column(Float, default=0)
    exit_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class BacktestRuleTemplate(Base):
    __tablename__ = "backtest_rule_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    rule_config: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
