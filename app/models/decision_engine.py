"""G0-WP0-2c: Decision Engine core models.

4 immutable tables for P0 contract (方案 A — single active model):
  - PortfolioFactorUsage     : per-portfolio model binding audit trail
  - StrategyExecutionSnapshot: immutable runtime snapshot frozen at task launch
  - DecisionRun             : per-(snapshot, trade_date) batch run record
  - DecisionEvidence        : per-(run, symbol) atomic decision + evidence

All datetime fields persist as UTC naive (database contract). Application-side
conversions pass exclusively through app/services/decision_clock.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# ---------------------------------------------------------------------------
# TaskIdempotency — T-D5 Q28.1 专用幂等表
# ---------------------------------------------------------------------------
class TaskIdempotency(Base):
    """幂等锁：任何 backtest/factor_calc/model_train/auto_trade 任务在执行前先写本表。

    - idempotency_key 后端独立计算（sha256 5 元组），**不依赖前端传 Idempotency-Key 头**，
      前端 UUID 只辅助防连点；真正的后端幂等只认这个 key。
    - existing_task_id 命中时直接返回原任务，不新建；param_hash 不匹配 → D6 409 冲突。
    """
    __tablename__ = "task_idempotencies"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_task_idempotencies_key"),
        CheckConstraint(
            "task_type IN ('backtest','factor_calc','model_train','auto_trade')",
            name="ck_task_idempotencies_task_type_4values",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True,
        comment="T-D5 Q28.1: sha256(portfolio_id|snapshot_id|decision_at_iso|trade_date_iso|run_type).hexdigest()",
    )
    portfolio_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True,
    )
    strategy_snapshot_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    decision_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        comment="UTC naive：决策/任务触发时刻",
    )
    trade_date: Mapped[date] = mapped_column(
        Date, nullable=False, index=True,
    )
    run_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )
    task_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="backtest|factor_calc|model_train|auto_trade",
    )
    existing_task_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
        comment="首次写入时填已创建的 async_tasks/decision_runs/... 主键；同 key 重放直接返回它",
    )
    param_hash: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="T-D6 用：参数指纹；同 key 但 param_hash 不一致 → 409 CONFLICT",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


# ---------------------------------------------------------------------------
# PortfolioFactorUsage — binding record (effective_from/to timeline)
# ---------------------------------------------------------------------------
class PortfolioFactorUsage(Base):
    __tablename__ = "portfolio_factor_usages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True,
    )
    factor_model_run_id: Mapped[str] = mapped_column(
        String(64), index=True,
        comment="Must equal global FactorRuntimeState.active_model_run_id (方案A)",
    )
    factor_set_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rule_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rule_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    run_mode: Mapped[str] = mapped_column(
        String(16), default="research", server_default="research",
        comment="research | production_pit | production_sim",
    )
    pit_mode: Mapped[str] = mapped_column(
        String(16), default="best_effort", server_default="best_effort",
        comment="best_effort | strict_pit_safe",
    )
    score_sla_coverage_pct: Mapped[float] = mapped_column(
        Float, default=95.0, server_default="95.0",
    )
    score_sla_max_age_days: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1",
    )
    status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="draft", index=True,
        comment="draft | active | deprecated | rollback_pending",
    )
    rollback_target_model_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
    )
    versions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    effective_from: Mapped[datetime] = mapped_column(DateTime, index=True)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[str] = mapped_column(
        String(128), default="local_user", server_default="local_user",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# StrategyExecutionSnapshot — immutable runtime view
# ---------------------------------------------------------------------------
class StrategyExecutionSnapshot(Base):
    __tablename__ = "strategy_execution_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "snapshot_no", name="uq_strategy_exec_snapshot_portfolio_no"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
        comment="组合内快照流水号（自增1开始），服务端生成",
    )
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True,
    )
    portfolio_factor_usage_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("portfolio_factor_usages.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    factor_model_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    factor_set_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    rule_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rule_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    decision_clock_json: Mapped[str] = mapped_column(
        Text,
        comment="decision_at=15:05 / data_cutoff_at=15:00 / execution_at=T+1 Asia/Shanghai",
    )
    cost_config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    member_snapshot_json: Mapped[str] = mapped_column(Text)
    candidate_pool_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    universe_type: Mapped[str] = mapped_column(
        String(32), default="portfolio_members",
        server_default="portfolio_members",
        comment="portfolio_members | candidate_pool_union — 禁止 heuristic 指数池",
    )
    benchmark_code: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="仅用于基准收益曲线，不参与选股",
    )

    snapshot_type: Mapped[str] = mapped_column(
        String(16), default="save_and_apply", server_default="save_and_apply",
        comment="preflight | save_and_apply | task_locked",
    )
    snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )

    # Q5.3: 关键成员 symbol_id 列表 JSON(list[int])；NULL/空=继承 Portfolio 或全默认
    key_members_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    gate_policy_version: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
    )
    gate_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    versions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    effective_from: Mapped[datetime] = mapped_column(DateTime, index=True)
    task_locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )
    task_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    locked_by_run_type: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="backtest | auto_sim | research",
    )
    created_by: Mapped[str] = mapped_column(
        String(128), default="local_user", server_default="local_user",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


# ---------------------------------------------------------------------------
# DecisionRun — one per (snapshot, trade_date)
# ---------------------------------------------------------------------------
class DecisionRun(Base):
    __tablename__ = "decision_runs"
    __table_args__ = (
        CheckConstraint("run_type IN ('research_preflight','backtest','auto_simulation')", name="ck_decision_runs_run_type_3values"),
        CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','INTERRUPTED','CANCELLED')", name="ck_decision_runs_status_6values"),
        UniqueConstraint("idempotency_key", name="uq_decision_runs_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("strategy_execution_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    run_type: Mapped[str] = mapped_column(
        String(20), index=True,
        comment="research_preflight | backtest | auto_simulation — 三种业务场景，Q25/DB枚举严格对齐",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="PENDING", server_default="PENDING", comment="PENDING|RUNNING|SUCCEEDED|FAILED|INTERRUPTED|CANCELLED")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="决策摘要文本；供今日决策页面只读视图直接渲染")
    checks_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="门禁检查项JSON，如覆盖率、断档判定等")
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="全局决策结果内容哈希（today_preview纯视图校验用）")
    trade_date: Mapped[date] = mapped_column(Date, index=True)

    # UTC naive persisted; converted via decision_clock.
    decision_at: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff_at: Mapped[datetime] = mapped_column(DateTime)
    execution_at: Mapped[datetime] = mapped_column(DateTime)

    run_mode: Mapped[str] = mapped_column(
        String(16), default="research", server_default="research",
    )
    pit_mode: Mapped[str] = mapped_column(
        String(16), default="best_effort", server_default="best_effort",
    )

    universe_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    member_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    score_count_expected: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    score_count_actual: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    score_coverage_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="按 成员×交易日 计算",
    )
    score_max_age_days: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )

    blocking_status: Mapped[str] = mapped_column(
        String(32), default="READY", server_default="READY",
        comment="READY|DATA_INCOMPLETE_PAUSED|RECONCILIATION_BLOCKED|MODEL_INACTIVE|SCORE_STALE",
    )
    blocking_reasons_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )

    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    versions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    is_result_production_eligible: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1",
        comment="T-A4 Q6.1: 结果是否可进入生产；BOOLEAN(0/1)，历史数据默认1兼容",
    )
    match_mode: Mapped[str] = mapped_column(
        String(16), default="NEXT_OPEN", server_default="NEXT_OPEN", nullable=False,
        comment="Q2.1 T-B1: 撮合模式 NEXT_OPEN/T_CLOSE；正式链路禁止 T_CLOSE",
    )

    # FR-P1-2 可靠性扩展：与 async_task + governance_events_outbox 串联
    correlation_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
        comment="跨请求/任务/决策/outbox/审计统一关联 ID（8 hex）；AC-10 用来证明 0 重复订单/成交",
    )
    task_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
        comment="归属 async_tasks.id；供 POST /tasks/{id}/cancel 级联取消决策/订单",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


# ---------------------------------------------------------------------------
# DecisionEvidence — one per (run, symbol); all six action categories
# ---------------------------------------------------------------------------
class DecisionEvidence(Base):
    __tablename__ = "decision_evidence"
    __table_args__ = (
        CheckConstraint("action IN ('BUY','SELL','HOLD','NO_ACTION','REJECTED','DATA_BLOCKED')", name="ck_decision_evidence_action_6values"),
        UniqueConstraint("decision_run_id", "symbol_id", name="uq_decision_evidence_run_symbol"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_run_id: Mapped[str] = mapped_column(
        ForeignKey("decision_runs.id", ondelete="RESTRICT"), index=True,
    )
    strategy_snapshot_id: Mapped[str] = mapped_column(String(64), index=True)
    portfolio_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="RESTRICT"), index=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, index=True)

    decision_at: Mapped[datetime] = mapped_column(DateTime)
    data_cutoff_at: Mapped[datetime] = mapped_column(DateTime)
    execution_at: Mapped[datetime] = mapped_column(DateTime)

    action: Mapped[str] = mapped_column(
        String(24), index=True,
        comment="BUY|SELL|HOLD|NO_ACTION|REJECTED|DATA_BLOCKED",
    )
    action_subtype: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True,
        comment="SELL_STOP_LOSS|SELL_RISK_EXIT|SELL_STRATEGY_EXIT|REJECTED_STALE_SCORE|REJECTED_ORDER_BELOW_LOT_SIZE|...",
    )

    target_position_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Post-sequential-clamp, pre-min-lot rounding",
    )
    min_lot_size: Mapped[int] = mapped_column(
        Integer, default=100, server_default="100",
    )
    target_quantity: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Final quantity after lot rounding; 0 if REJECTED",
    )
    target_qty_delta: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="T-A7 Q7.1: target_quantity - current_quantity; DATA_BLOCKED 时=0（零调仓）",
    )

    intended_price: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="NEXT_OPEN × slippage direction (×1.0005 buy / ×0.9995 sell)",
    )
    executed_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    slippage_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    rejection_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    blocking_reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="T-A7 Q7.1: DATA_BLOCKED/SUSPENDED 时的具体阻断原因文本",
    )
    # ════════════════════════════════════════════════════════════════════
    # T-B3 Q2.2：每次拒单/顺延都落证据
    # ════════════════════════════════════════════════════════════════════
    roll_forward_days: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True,
        comment="T-B3 Q2.2: NEXT_OPEN 顺延跳过的天数；0=T+1 直接命中；NULL=未进入顺延流程",
    )
    rejections_trace_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="T-B3 Q2.2: 顺延/拒单跟踪明细 JSON 列表（canonical）；每个元素 {date, reason: SUSPENDED|LIMIT_UP_DOWN|ORDER_BELOW_LOT_SIZE|..., intended_open}",
    )
    # ════════════════════════════════════════════════════════════════════
    # T-B5 Q11.1：卖出规则全部命中记录 + 最高优先级裁决
    # ════════════════════════════════════════════════════════════════════
    exit_rules_hit_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="T-B5 Q11.1: 卖出规则命中 JSON 列表（canonical）；每元素 {rule_code, rule_priority, requested_exit_qty, rule_subtype}",
    )

    score_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("scores.id", ondelete="SET NULL"), nullable=True,
    )
    score_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    score_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
        comment="Score.published_at; NULL => NOT_PIT_SAFE in strict_pit mode",
    )
    pit_safe_flag: Mapped[str] = mapped_column(
        String(16), default="UNKNOWN", server_default="UNKNOWN",
        comment="PIT_SAFE|NOT_PIT_SAFE|UNKNOWN",
    )

    constraints_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Sequential clamp steps: before/after/triggered_constraint/remaining_delta",
    )
    versions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason_codes_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    factor_contributions_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )

    legacy_fallback_flag: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False,
        comment="T-A6 Q6.3: 是否使用 legacy_score fallback；BOOLEAN(0/1)，默认0",
    )
    stop_loss_verified_price_source: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="T-A8 Q7.2: 止损SELL验证价源 FIRST_OPEN/PREV_CLOSE；NULL=未启用/非止损SELL",
    )
    stop_loss_triggered: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False,
        comment="T-A8 Q7.2: 是否触发止损；BOOLEAN(0/1)，默认0",
    )
    match_mode: Mapped[str] = mapped_column(
        String(16), default="NEXT_OPEN", server_default="NEXT_OPEN", nullable=False,
        comment="Q2.1: 撮合模式 NEXT_OPEN/T_CLOSE；正式链路禁止 T_CLOSE",
    )
    manual_price_flag: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False,
        comment="T-A9 Q7.4: 该条 evidence 是否使用 T-A9 手动指定价；BOOLEAN(0/1)，默认0=未使用",
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


# ---------------------------------------------------------------------------
# DecisionOrderPlanRecord — immutable order intent, one-to-one with evidence
# ---------------------------------------------------------------------------
class DecisionOrderPlanRecord(Base):
    """Durable order-plan contract for unified decision consumers.

    Execution is deliberately not stored here: fills, partial retries and
    rejection outcomes remain in the execution ledger and DecisionEvidence.
    This table makes the immutable intent independently queryable without
    breaking historical readers that still consume ``versions_json``.
    """

    __tablename__ = "decision_order_plans"
    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_decision_order_plans_evidence"),
        Index("ix_decision_order_plans_run_execution", "decision_run_id", "execution_date"),
    )

    order_plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_run_id: Mapped[str] = mapped_column(
        ForeignKey("decision_runs.id", ondelete="RESTRICT"), index=True,
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("decision_evidence.id", ondelete="RESTRICT"), index=True,
    )
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True,
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="RESTRICT"), index=True,
    )
    action: Mapped[str] = mapped_column(String(24), index=True)
    signal_date: Mapped[date] = mapped_column(Date, index=True)
    execution_date: Mapped[date] = mapped_column(Date, index=True)
    target_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    intended_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    rejection_trace_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True,
    )


# ---------------------------------------------------------------------------
# GovernanceOutboxEvent — FR-P1-2 事务 Outbox
# ---------------------------------------------------------------------------
class GovernanceOutboxEvent(Base):
    """治理事务 Outbox：与业务写入同事务落事件，Worker 至少一次投递。

    语义与 notification_outbox（消息通道通知）严格区分：本 Outbox 承载
    「决策 → 订单 → 持仓 → 审计」跨表、跨库（MySQL↔DuckDB↔Audit）交付
    一致性保证；同 dedup_key 唯一幂等，保证 AC-10 重复投递 3 次 0 重复成交。
    """

    __tablename__ = "governance_events_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('READY','IN_PROGRESS','DELIVERED','DEAD_LETTERED')",
            name="ck_gov_outbox_status_values",
        ),
        CheckConstraint(
            "retry_count >= 0 AND retry_count <= 200",
            name="ck_gov_outbox_retry_count_range",
        ),
        UniqueConstraint("dedup_key", name="uq_governance_outbox_dedup_key"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    dedup_key: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True,
        comment="幂等键：aggregate_type + aggregate_id + event_type + business_key + correlation_id",
    )
    correlation_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="跨表统一关联 ID（8 hex）",
    )
    aggregate_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="portfolio | decision_run | order_plan | position | audit_event | alert | task",
    )
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        comment="DECISION_MADE / ORDER_PLAN_WRITTEN / POSITION_RECONCILED / AUDIT_WRITTEN / "
                "DATA_INCOMPLETE_PAUSED / RESUME_REQUESTED / TASK_CANCELLED / TASK_HEARTBEAT / "
                "CANCELLED_TIMEOUT / SCHEDULE_GATE_BLOCKED / RECONCILIATION_GAP_WARNING",
    )
    business_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("async_tasks.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    portfolio_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("portfolios.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    payload_json: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="JSON：至少含 correlation_id、occurred_at、operator_id、受影响主键、版本号、前置 hash / 后置 hash",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="READY",
        index=True,
        comment="READY / IN_PROGRESS / DELIVERED / DEAD_LETTERED",
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    locked_by_worker: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# ManualPriceOverride — T-A9 Q7.4 人工输入价 + continue_forward 记录
# ---------------------------------------------------------------------------
class ManualPriceOverride(Base):
    """T-A9：人工处理 DATA_BLOCKED 明细。

    - 3 种 resolution 模式严格存 `resolved_mode`（confirm_manual_price / continue_forward / keep_paused）
    - consumed_flag=1 代表该条 override 已经被 decision_engine evaluate() 消费一次（幂等：同日同 symbol 未消费唯一）
    - evaluate() 查询规则：`portfolio_id + trade_date + (symbol_id=X OR symbol_id IS NULL) AND consumed_flag=0`
      symbol_id=NULL 代表 portfolio 级别 continue_forward / keep_paused 批量决议
    """

    __tablename__ = "manual_price_overrides"
    __table_args__ = (
        CheckConstraint(
            "resolved_mode IN ('confirm_manual_price','continue_forward','keep_paused')",
            name="ck_manual_price_override_resolved_mode_3values",
        ),
        UniqueConstraint(
            "portfolio_id", "trade_date", "symbol_id",
            name="uq_manual_price_override_portfolio_date_symbol",
        ),
        Index(
            "ix_manual_price_overrides_portfolio_date_consumed",
            "portfolio_id", "trade_date", "consumed_flag",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    portfolio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True,
    )
    strategy_snapshot_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("strategy_execution_snapshots.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # symbol_id NULL = portfolio 级批处理决议（例如 keep_paused ALL / continue_forward ALL）
    symbol_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("symbols.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, index=True, comment="本次人工处理作用于哪一个交易日")

    resolved_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="confirm_manual_price=指定价介入; continue_forward=跳过今日保留; keep_paused=维持阻断",
    )
    # 仅 confirm_manual_price 模式非 NULL
    manual_executable_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_note: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="操作员备注/数据来源说明",
    )
    previous_data_gap_days: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="阻断时已数据断档天数（审计对齐用）",
    )

    consumed_flag: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False, index=True,
        comment="BOOLEAN 0/1；1 = decision_engine.evaluate() 已消费注入，不再重复消费",
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    consumed_by_run_id: Mapped[Optional[str]] = mapped_column(
        String(64), ForeignKey("decision_runs.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    operator_id: Mapped[str] = mapped_column(
        String(128), default="system", server_default="system", nullable=False,
    )
    correlation_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        index=True,
    )
