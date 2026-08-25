from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
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
    reproducibility_status: Mapped[str] = mapped_column(
        String(32), default="legacy/non_reproducible", server_default="legacy/non_reproducible",
        comment="reproducible for snapshot-backed runs; legacy/non_reproducible for historical runs",
    )
    reproducibility_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Why a run is legacy or cannot be replayed",
    )
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

    # -- G0-WP0-2b: linkage + PIT + benchmark persistence --
    factor_set_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="FactorSet bound via snapshot at task launch",
    )
    strategy_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("strategy_execution_snapshots.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="Snapshot locked at task start; subsequent saves do not affect run",
    )
    pit_mode: Mapped[str] = mapped_column(
        String(16), default="best_effort", server_default="best_effort",
        comment="best_effort | strict_pit_safe",
    )
    decision_run_ids_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="JSON list of decision_runs.id created by this backtest",
    )
    benchmark_equity_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Persisted benchmark equity curve; never recompute ad-hoc with linear 5% fallback",
    )
    benchmark_status: Mapped[str] = mapped_column(
        String(24), default="FULL", server_default="FULL",
        comment="FULL|BENCHMARK_INCOMPLETE|SOURCE_MISSING. Gaps>5d => INCOMPLETE.",
    )
    benchmark_gap_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Number of consecutive benchmark gap days observed",
    )

    is_result_production_eligible: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1",
        comment="T-A4 Q6.1: 结果是否可进入生产；BOOLEAN(0/1)，历史数据默认1兼容",
    )
    match_mode: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
        comment="Q2.1: 撮合模式 NEXT_OPEN/T_CLOSE；正式链路禁止 T_CLOSE",
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

    # -- G0-WP0-2b: evidence linkage + slippage --
    decision_evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("decision_evidence.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="Primary entry-side evidence",
    )
    exit_evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("decision_evidence.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    intended_entry_price: Mapped[float | None] = mapped_column(
        Float, nullable=True,
        comment="Q2.3: NEXT_OPEN × directional 5bp slippage",
    )
    slippage_bps: Mapped[float | None] = mapped_column(
        Float, nullable=True,
        comment="Actual slippage applied to the trade",
    )
    entry_rejection_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="Q2.2: Per-day rejection logged per limit-up/down failure",
    )


class BacktestExecutionFill(Base):
    """One immutable successful fill produced by the backtest matcher.

    ``BacktestTrade`` is an aggregate lot view: a volume-limited order may
    accumulate multiple executions while retaining its original entry date.
    This ledger preserves the actual session of each fill for daily holdings,
    cash reconstruction and evidence-level audit.
    """

    __tablename__ = "backtest_execution_fills"
    __table_args__ = (
        Index(
            "ix_backtest_execution_fills_run_date_symbol",
            "run_id",
            "execution_date",
            "symbol_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True,
    )
    backtest_trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtest_trades.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Aggregate lot affected by this execution; nullable for preserved history",
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True,
    )
    execution_date: Mapped[date] = mapped_column(Date, index=True)
    side: Mapped[str] = mapped_column(String(4), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    executed_price: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float, default=0)
    decision_evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("decision_evidence.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    order_plan_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )


class BacktestValuationSnapshot(Base):
    """Immutable end-of-day valuation used by a completed backtest run.

    Daily bars can be corrected in place by a source sync.  This table stores
    the mark that was available when a backtest completed so historical
    holdings never silently change on a later read.
    """

    __tablename__ = "backtest_valuation_snapshots"
    __table_args__ = (
        Index(
            "uq_backtest_valuation_snapshot_run_date_symbol",
            "run_id",
            "trade_date",
            "symbol_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), index=True,
    )
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    portfolio_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_bar_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
    )


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
