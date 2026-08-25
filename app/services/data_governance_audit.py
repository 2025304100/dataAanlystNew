"""WP0-2 / G3-2 数据治理审计事件模型 + 服务。

审计事件类型（对应 tasks.md §WP0-2 多处要求）：
  - PORTFOLIO_CANDIDATE_SCD2_CHANGE : 候选池 SCD2 变更（同日多次/跨日/手动移除/授权撤销）
  - BENCHMARK_SOURCE_FAILOVER      : 基准主备切换（INDEX→AK→BS，双源失败）
  - AUTO_SIMULATION_RESULT         : 自动推演成功/失败（推进 last_decision_trade_date）
  - RECONCILIATION_RESULT          : 对账成功/差异/阻塞（推进 last_reconciled_trade_date）
  - ILLEGAL_STATE_TRANSITION       : 非法状态机跳转尝试（如 RECONCILIATION_BLOCKED → RUNNING）
  - FACTOR_USAGE_APPLIED           : FactorUsage 原子绑定成功
  - OUTBOX_EVENT_DISPATCHED        : 事务内 Outbox 分发结果
  - DATA_SOURCE_FAILOVER           : 数据集主备源切换（保留尝试链和实际命中源）
  - DATA_QUALITY_QUARANTINE        : 脏数据分区隔离/解除

写入规则：
  - 任何写审计事件的入口必须在**同一 DB 事务**内落库，业务失败则一起回滚。
  - before_json / after_json 字段使用 canonical JSON 规则（字典键排序、去尾零、无 NaN/Inf）。
  - operator_id 缺省为 "system"，人工操作必须传入操作者。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, Session

from app.db.base import Base

AuditAction = Literal[
    "PORTFOLIO_CANDIDATE_SCD2_CHANGE",
    "BENCHMARK_SOURCE_FAILOVER",
    "AUTO_SIMULATION_RESULT",
    "RECONCILIATION_RESULT",
    "ILLEGAL_STATE_TRANSITION",
    "FACTOR_USAGE_APPLIED",
    "OUTBOX_EVENT_DISPATCHED",
    "DATA_BLOCK_RESOLUTION",
    "DATA_SOURCE_FAILOVER",
    "DATA_QUALITY_QUARANTINE",
    "G6_ROLLOUT_STARTED",
    "G6_ROLLOUT_ROLLED_BACK",
    "UNKNOWN_AUDIT_ACTION",
]


ALLOWED_AUDIT_ACTIONS: frozenset[str] = frozenset({
    "PORTFOLIO_CANDIDATE_SCD2_CHANGE",
    "BENCHMARK_SOURCE_FAILOVER",
    "AUTO_SIMULATION_RESULT",
    "RECONCILIATION_RESULT",
    "ILLEGAL_STATE_TRANSITION",
    "FACTOR_USAGE_APPLIED",
    "OUTBOX_EVENT_DISPATCHED",
    "DATA_BLOCK_RESOLUTION",
    "DATA_SOURCE_FAILOVER",
    "DATA_QUALITY_QUARANTINE",
    "G6_ROLLOUT_STARTED",
    "G6_ROLLOUT_ROLLED_BACK",
    "UNKNOWN_AUDIT_ACTION",
})


# ──────────────────────────────────────────────────────────── ORM 模型
class DataGovernanceAuditEvent(Base):
    __tablename__ = "data_governance_audit_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ("
            "'PORTFOLIO_CANDIDATE_SCD2_CHANGE',"
            "'BENCHMARK_SOURCE_FAILOVER',"
            "'AUTO_SIMULATION_RESULT',"
            "'RECONCILIATION_RESULT',"
            "'ILLEGAL_STATE_TRANSITION',"
            "'FACTOR_USAGE_APPLIED',"
            "'OUTBOX_EVENT_DISPATCHED',"
            "'DATA_BLOCK_RESOLUTION',"
            "'DATA_SOURCE_FAILOVER',"
            "'DATA_QUALITY_QUARANTINE',"
            "'G6_ROLLOUT_STARTED',"
            "'G6_ROLLOUT_ROLLED_BACK',"
            "'UNKNOWN_AUDIT_ACTION'"
            ")",
            name="ck_dg_audit_action_values",
        ),
        Index("ix_dg_audit_portfolio_action_time", "portfolio_id", "action", "occurred_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 关联对象（可为 NULL，如系统级事件）
    portfolio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    symbol_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 业务主键（如 DecisionRun.id / PortfolioUsage.id）
    business_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 发生时间
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # 操作者（人工点击=用户ID，系统任务=system/cron_worker）
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False, default="system",
                                             server_default="system")
    # 请求/协程追踪键
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 变更前后快照（字符串形式避免不同 DB JSON 类型差异）
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 结构化属性（事件特有字段：intra_day_seq=同日变更序号、coverage、source_switch 等）
    attributes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ──────────────────────────────────────────────────────────── 服务
def _canonical_dumps(obj: Any) -> str:
    """简化 canonical JSON：键排序、无空格、None 允许。"""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return json.dumps({"_raw_repr": repr(obj)}, sort_keys=True, separators=(",", ":"))


@dataclass
class AuditWriteResult:
    event_id: int | None
    action: str


def write_audit_event(
    db: Session,
    action: AuditAction,
    *,
    portfolio_id: int | None = None,
    symbol_id: int | None = None,
    business_key: str | None = None,
    occurred_at: datetime | None = None,
    operator_id: str = "system",
    correlation_id: str | None = None,
    before: Any = None,
    after: Any = None,
    attributes: dict[str, Any] | None = None,
    note: str | None = None,
) -> AuditWriteResult:
    """事务内写一条审计事件；业务回滚时一同回滚。

    返回 (event_id, action)；event_id 在 flush 后才是有效值。
    """
    allowed = {
        "PORTFOLIO_CANDIDATE_SCD2_CHANGE", "BENCHMARK_SOURCE_FAILOVER",
        "AUTO_SIMULATION_RESULT", "RECONCILIATION_RESULT",
        "ILLEGAL_STATE_TRANSITION", "FACTOR_USAGE_APPLIED",
        "OUTBOX_EVENT_DISPATCHED", "DATA_BLOCK_RESOLUTION",
        "DATA_SOURCE_FAILOVER", "DATA_QUALITY_QUARANTINE",
        "G6_ROLLOUT_STARTED", "G6_ROLLOUT_ROLLED_BACK",
        "UNKNOWN_AUDIT_ACTION",
    }
    if action not in allowed:
        # fail-soft：非法值映射为 UNKNOWN_AUDIT_ACTION，原始 action 放到 attributes.original_action
        effective_action: str = "UNKNOWN_AUDIT_ACTION"
        extra_attr = {"original_action": str(action)}
        attributes = {**(attributes or {}), **extra_attr}
        note = (note or "") + f" [INVALID_ACTION: {action!r}]"
    else:
        effective_action = action
    row = DataGovernanceAuditEvent(
        action=effective_action,
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        business_key=str(business_key) if business_key else None,
        occurred_at=occurred_at or datetime.now(),
        operator_id=str(operator_id or "system"),
        correlation_id=correlation_id,
        before_json=None if before is None else _canonical_dumps(before),
        after_json=None if after is None else _canonical_dumps(after),
        attributes_json=None if attributes is None else _canonical_dumps(attributes),
        note=note,
    )
    db.add(row)
    db.flush()
    return AuditWriteResult(event_id=row.id, action=row.action)


# ──────────────────────────────────────────────────────────── 业务语义写入快捷函数
def audit_candidate_scd2_change(
    db: Session, portfolio_id: int, operator_id: str,
    *,
    intra_day_seq: int,
    action_type: Literal["INSERT", "UPDATE_CLOSE_ONLY", "UPDATE_IN_PLACE"],
    before_rows: list[dict[str, Any]] | None = None,
    after_rows: list[dict[str, Any]] | None = None,
    removed_symbols: list[int] | None = None,
    added_symbols: list[int] | None = None,
    authorized_symbols: list[int] | None = None,
    correlation_id: str | None = None,
) -> AuditWriteResult:
    """WP0-2 TR-02.4 / TR-03.9d：候选池 SCD2 变更专用写入。"""
    attrs: dict[str, Any] = {
        "intra_day_seq": int(intra_day_seq),
        "action_type": action_type,
    }
    if removed_symbols:
        attrs["removed_symbols"] = [int(x) for x in removed_symbols]
    if added_symbols:
        attrs["added_symbols"] = [int(x) for x in added_symbols]
    if authorized_symbols:
        attrs["authorized_symbols"] = [int(x) for x in authorized_symbols]
    return write_audit_event(
        db, "PORTFOLIO_CANDIDATE_SCD2_CHANGE",
        portfolio_id=portfolio_id,
        operator_id=operator_id,
        before=before_rows,
        after=after_rows,
        attributes=attrs,
        correlation_id=correlation_id,
        occurred_at=datetime.now(),
    )


def audit_benchmark_failover(
    db: Session,
    benchmark_symbol: str,
    primary_source: str,
    switch_event: dict[str, Any] | None,
    *,
    portfolio_id: int | None = None,
    correlation_id: str | None = None,
) -> AuditWriteResult:
    """WP0-6b TR-06b.4：基准主备切换审计。"""
    return write_audit_event(
        db, "BENCHMARK_SOURCE_FAILOVER",
        portfolio_id=portfolio_id,
        business_key=f"benchmark::{benchmark_symbol}",
        attributes={
            "benchmark_symbol": benchmark_symbol,
            "primary_source": primary_source,
            "switch_event": switch_event,
        },
        note=(
            None if switch_event is None
            else switch_event.get("reason") if isinstance(switch_event, dict) else None
        ),
        correlation_id=correlation_id,
        occurred_at=datetime.now(),
    )


def audit_source_failover(
    db: Session,
    *,
    interface_key: str,
    attempted_sources: list[str] | tuple[str, ...],
    selected_source: str,
    reason: str | None = None,
    symbol: str | None = None,
    dataset: str | None = None,
    source_snapshot_id: str | None = None,
    correlation_id: str | None = None,
    operator_id: str = "system",
) -> AuditWriteResult:
    """Record a concrete primary/backup source switch.

    The attempted order and the selected source are retained as immutable
    attributes.  This is intentionally separate from the legacy benchmark
    action so daily-bars and valuation failovers are queryable by interface.
    """
    attempted = [str(item) for item in attempted_sources]
    attrs: dict[str, Any] = {
        "interface_key": str(interface_key),
        "dataset": dataset,
        "attempted_sources": attempted,
        "selected_source": str(selected_source),
        "source_snapshot_id": source_snapshot_id,
        "reason": reason,
    }
    business_key = f"source::{interface_key}::{symbol or dataset or 'all'}"
    return write_audit_event(
        db,
        "DATA_SOURCE_FAILOVER",
        symbol_id=None,
        business_key=business_key,
        operator_id=operator_id,
        correlation_id=correlation_id,
        attributes=attrs,
        note=reason,
        occurred_at=datetime.now(),
    )


def audit_data_quarantine(
    db: Session,
    *,
    quarantine_id: str,
    partition_id: str | None,
    dataset: str,
    reason_code: str,
    source_name: str | None = None,
    correlation_id: str | None = None,
    operator_id: str = "system",
) -> AuditWriteResult:
    """Write the quarantine transition in the same transaction as the row."""
    return write_audit_event(
        db,
        "DATA_QUALITY_QUARANTINE",
        business_key=str(quarantine_id),
        operator_id=operator_id,
        correlation_id=correlation_id,
        attributes={
            "event": "quarantined",
            "quarantine_id": str(quarantine_id),
            "partition_id": partition_id,
            "dataset": str(dataset),
            "reason_code": str(reason_code),
            "source_name": source_name,
        },
        note=f"quarantine:{reason_code}",
        occurred_at=datetime.now(),
    )


def audit_data_quarantine_release(
    db: Session,
    *,
    quarantine_id: str,
    reason: str,
    operator_id: str = "system",
    correlation_id: str | None = None,
) -> AuditWriteResult:
    """Record an explicit human/system quarantine release."""
    return write_audit_event(
        db,
        "DATA_QUALITY_QUARANTINE",
        business_key=str(quarantine_id),
        operator_id=operator_id,
        correlation_id=correlation_id,
        attributes={
            "event": "released",
            "quarantine_id": str(quarantine_id),
            "reason": str(reason),
        },
        note=f"quarantine_release:{reason}",
        occurred_at=datetime.now(),
    )


def audit_auto_simulation_result(
    db: Session,
    portfolio_id: int,
    trade_date: date,
    result_status: Literal["SUCCEEDED", "FAILED", "INTERRUPTED"],
    *,
    decision_run_id: str,
    operator_id: str = "cron_worker",
    correlation_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> AuditWriteResult:
    """WP0-6：auto_simulation 成功/失败审计（推进 last_decision_trade_date 配套写）。"""
    attributes: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "result_status": result_status,
        "error_code": error_code,
    }
    if error_message:
        attributes["error_message"] = error_message
    return write_audit_event(
        db, "AUTO_SIMULATION_RESULT",
        portfolio_id=portfolio_id,
        business_key=decision_run_id,
        occurred_at=datetime.now(),
        operator_id=operator_id,
        correlation_id=correlation_id,
        attributes=attributes,
    )


def audit_illegal_state_transition(
    db: Session,
    portfolio_id: int,
    from_state: str,
    to_state: str,
    *,
    operator_id: str = "system",
    correlation_id: str | None = None,
) -> AuditWriteResult:
    """WP0-2 TR-02.7d：非法状态机跳转审计。"""
    return write_audit_event(
        db, "ILLEGAL_STATE_TRANSITION",
        portfolio_id=portfolio_id,
        operator_id=operator_id,
        correlation_id=correlation_id,
        attributes={"from_state": from_state, "to_state": to_state},
        note=f"Attempted {from_state} -> {to_state}",
        occurred_at=datetime.now(),
    )


def audit_data_block_resolution(
    db: Session,
    portfolio_id: int,
    resolved_mode: Literal["confirm_manual_price", "continue_forward", "keep_paused"],
    *,
    symbol_id: int | None = None,
    manual_price: float | None = None,
    previous_data_gap_days: int | None = None,
    operator_id: str = "system",
    correlation_id: str | None = None,
    trade_date: date | None = None,
) -> AuditWriteResult:
    """T-A9 Q7.4：数据阻断人工处理审计。resolved_mode 三选一 严格对齐路由。"""
    attributes: dict[str, Any] = {
        "resolved_mode": resolved_mode,
        "symbol_id": symbol_id,
        "previous_data_gap_days": previous_data_gap_days,
    }
    if manual_price is not None:
        attributes["manual_price"] = float(manual_price)
    if trade_date is not None:
        attributes["trade_date"] = trade_date.isoformat()
    bk = f"portfolio::{portfolio_id}::trade_date::{trade_date.isoformat() if trade_date else 'any'}::symbol::{symbol_id if symbol_id is not None else 'ALL'}"
    return write_audit_event(
        db, "DATA_BLOCK_RESOLUTION",
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        business_key=bk,
        operator_id=operator_id,
        correlation_id=correlation_id,
        attributes=attributes,
        note=(
            "DATA_BLOCKED 人工处理："
            + ("人工指定价 {:.4f}".format(float(manual_price)) if resolved_mode == "confirm_manual_price" and manual_price is not None
               else "显式跳过今日（continue_forward）" if resolved_mode == "continue_forward"
               else "保持 DATA_BLOCKED（keep_paused）")
        ),
        occurred_at=datetime.now(),
    )
