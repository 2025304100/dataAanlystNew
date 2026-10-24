"""安全门禁（WP6.5）。

- fail-closed：K 线/评分/规则版本过期时禁止买入
- 卖出风控不静默跳过：数据缺失时生成高优先级告警
- 任务取消传播：停止后续组合和后续订单
- 每笔失败独立记录，不回滚已合法成交的其他标的
- 所有新订单可追溯到成员/信号/规则/数据截止时间

project_memory 硬约束：
- K 线、评分或规则版本过期时 fail-closed 禁止买入
- 卖出风控不得因一般数据缺失静默跳过，应生成高优先级告警
- 自动交易任务取消时停止后续组合和后续订单
- 每笔失败独立记录，不回滚已合法成交的其他标的
- 所有新订单可追溯到成员/信号/规则/数据截止时间
- 错误消息不暴露敏感信息
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sim_account import SimOrder

logger = logging.getLogger(__name__)


# Deprecated compatibility exports. Admission uses business dates and the
# portfolio's real policy; these natural-hour values no longer decide health.
KLINE_FRESHNESS_HOURS = 24
SCORE_FRESHNESS_HOURS = 48
RULE_FRESHNESS_HOURS = 168  # 7 天


@dataclass
class DataHealthResult:
    """数据健康检查结果。"""

    healthy: bool
    reason: str
    kline_latest_at: datetime | None = None
    score_latest_at: datetime | None = None
    rule_version_id: int | None = None
    rule_version_at: datetime | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    expected_trade_date: date | None = None
    kline_trade_date: date | None = None
    score_trade_date: date | None = None


def _now_utc_naive() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive(dt: datetime | None) -> datetime | None:
    """将可能带时区的 datetime 转换为 naive UTC。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # 转为 UTC 后去除时区信息
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def check_data_health(
    db: Session,
    *,
    symbol_id: int,
    rule_version_id: int | None = None,
    portfolio_id: int | None = None,
) -> DataHealthResult:
    """Share business-date and source checks with portfolio inspection.

    Kept return fields preserve callers' API. Natural-hour freshness and a
    placeholder rule timestamp are no longer production admission evidence.
    Risk exits continue through check_sell_risk's explicit allowed+alert path.
    """
    from app.models.symbol import Symbol
    from app.services.portfolio_inspection_diagnostics import (
        _binding, _calendar, _member_rule_evidence, _symbol_diagnostics, _utc, inspection_admission_failures,
    )
    at = _utc(_now_utc_naive())
    with db.no_autoflush:
        try:
            symbol = db.get(Symbol, symbol_id)
            if symbol is None:
                return DataHealthResult(False, "标的基本资料缺失", reason_codes=["MISSING"])
            binding = {"model_run_id": None, "factor_set_id": None, "score_allowed_lag": 0}
            dependencies = {"members": []}
            binding_checks = []
            if portfolio_id is not None:
                binding, binding_checks, dependencies = _binding(db, portfolio_id, at)
            row = _symbol_diagnostics(db, {"symbol_id": symbol_id, "scopes": [], "entry_authorized": True},
                symbol, _calendar(db, symbol.market, at), binding, dependencies, at)
            checks = [ev for ev in row["evidence"] if ev.get("admission_relevant")] + binding_checks
            if rule_version_id is not None:
                # Members reference a version registry which is not implemented.
                # A PortfolioRule id is a different object; never claim it exists
                # by returning the current time or guessing another table's id.
                checks.append(_member_rule_evidence(at, symbol, rule_version_id, binding, ["new_buy", "automatic_recovery"]))
            failed = inspection_admission_failures(checks)
            def message(ev):
                label = ev.get("label") or ev["check_code"]
                if ev["check_code"] == "DAILY_BAR_FRESHNESS":
                    label = {"MISSING": "K线数据缺失", "STALE": "K线数据过期", "READ_ERROR": "K线读取失败"}.get(ev["status"], "日K行情")
                elif ev["check_code"] == "SCORE_FRESHNESS":
                    label = {"MISSING": "评分数据缺失", "STALE": "评分数据过期", "READ_ERROR": "评分读取失败"}.get(ev["status"], "适用评分")
                return f"{label}：{ev['reason']}"
            bar_ev = next(ev for ev in checks if ev["check_code"] == "DAILY_BAR_FRESHNESS")
            score_ev = next(ev for ev in checks if ev["check_code"] == "SCORE_FRESHNESS")
            return DataHealthResult(healthy=not failed,
                reason="；".join(dict.fromkeys(message(ev) for ev in failed)),
                kline_latest_at=_to_naive(bar_ev.get("available_at")), score_latest_at=_to_naive(score_ev.get("available_at")),
                rule_version_id=rule_version_id, checks=checks,
                reason_codes=list(dict.fromkeys(ev["status"] for ev in failed)),
                expected_trade_date=row["expected_trade_date"], kline_trade_date=row["actual_bar_trade_date"],
                score_trade_date=row["actual_score_trade_date"])
        except Exception:
            logger.warning("读取业务日期健康证据失败 symbol_id=%s", symbol_id, exc_info=True)
            return DataHealthResult(False, "行情或评分数据读取失败", reason_codes=["READ_ERROR"],
                checks=[{"check_code": "DATA_READ", "status": "READ_ERROR", "reason": "行情或评分数据读取失败"}])


def _get_latest_kline_time(db: Session, symbol_id: int) -> datetime | None:
    """Legacy metadata helper; latest business row's ingestion time only.

    This timestamp does not decide admission freshness.
    """
    try:
        from app.models.daily_bar import DailyBar

        bar = db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == symbol_id)
            .order_by(DailyBar.trade_date.desc(), DailyBar.id.desc())
            .limit(1)
        ).scalars().first()
        return bar.created_at if bar is not None else None
    except Exception as exc:
        # 不暴露敏感信息
        logger.warning("获取 K 线最新时间失败 symbol_id=%s: %s", symbol_id, exc)
        return None


def _get_latest_score_time(db: Session, symbol_id: int) -> datetime | None:
    """Legacy metadata helper; latest business row's actual publication time."""
    try:
        from app.models.score import Score

        score = db.execute(
            select(Score)
            .where(Score.symbol_id == symbol_id)
            .order_by(Score.trade_date.desc(), Score.id.desc())
            .limit(1)
        ).scalars().first()
        return score.published_at if score is not None else None
    except Exception as exc:
        logger.warning("获取评分最新时间失败 symbol_id=%s: %s", symbol_id, exc)
        return None


def _get_rule_version_time(db: Session, rule_version_id: int) -> datetime | None:
    """Legacy compatibility helper: no registry exists, so return unverified."""
    # Legacy helper retained for import compatibility. There is no real member
    # rule-version registry, so absence must remain unverified.
    _ = (db, rule_version_id)
    return None


# ----------------------------------------------------------------------------
# 卖出风控（不静默跳过，数据缺失时告警）
# ----------------------------------------------------------------------------


def check_sell_risk(
    db: Session,
    *,
    symbol_id: int,
    portfolio_id: int,
) -> tuple[bool, str]:
    """卖出风控检查（WP6.5）。

    卖出风控不得因一般数据缺失静默跳过。
    数据缺失时生成高优先级告警。

    返回 (allow_sell, reason)：
    - 即使数据缺失，仍允许卖出（止损保护）
    - 但会生成高优先级告警
    """
    health = check_data_health(db, symbol_id=symbol_id, portfolio_id=portfolio_id)

    if not health.healthy:
        # 数据缺失：生成高优先级告警（不静默跳过）
        _emit_sell_data_missing_alert(
            db,
            portfolio_id=portfolio_id,
            symbol_id=symbol_id,
            reason=health.reason,
        )
        # 卖出仍允许（止损保护），但记录告警
        return True, f"数据缺失但卖出允许（止损保护）：{health.reason}"

    return True, ""


def _emit_sell_data_missing_alert(
    db: Session,
    *,
    portfolio_id: int,
    symbol_id: int,
    reason: str,
) -> None:
    """卖出数据缺失告警（WP6.5 高优先级）。

    复用 emit_drawdown_warning 接入通知系统（WP-MSG.7）。
    不暴露敏感信息：reason 仅包含脱敏后的概要描述。
    """
    try:
        from app.services.notifications.event_emitter import emit_drawdown_warning

        # drawdown_pct=0 表示非回撤告警；以高优先级 severity 触发通知
        emit_drawdown_warning(
            db,
            portfolio_id=portfolio_id,
            drawdown_pct=0.0,
            threshold_pct=0.0,
            current_value=None,
        )
        logger.warning(
            "WP6.5 卖出数据缺失告警 portfolio_id=%s symbol_id=%s reason=%s",
            portfolio_id,
            symbol_id,
            reason,
        )
    except Exception as exc:
        # 告警发送失败不能影响卖出决策（止损保护优先）
        logger.warning("WP6.5 卖出告警发送失败： %s", exc)


# ----------------------------------------------------------------------------
# 任务取消传播（停止后续组合和后续订单）
# ----------------------------------------------------------------------------


@dataclass
class TaskCancelState:
    """任务取消状态。"""

    cancelled: bool = False
    cancel_reason: str = ""
    cancelled_at: datetime | None = None
    processed_portfolios: list[int] = field(default_factory=list)
    skipped_portfolios: list[int] = field(default_factory=list)


# 全局任务取消状态（单进程内）
_task_cancel_state: TaskCancelState | None = None


def check_task_cancelled() -> bool:
    """检查任务是否已取消（WP6.5）。

    任务取消时停止后续组合和后续订单。
    """
    return _task_cancel_state is not None and _task_cancel_state.cancelled


def cancel_task(reason: str = "") -> None:
    """取消任务（WP6.5）。

    设置取消标志，后续组合和订单不再处理。
    """
    global _task_cancel_state
    _task_cancel_state = TaskCancelState(
        cancelled=True,
        cancel_reason=reason,
        cancelled_at=_now_utc_naive(),
        processed_portfolios=[],
        skipped_portfolios=[],
    )
    logger.warning("WP6.5 任务已取消： %s", reason)


def reset_task_cancel() -> None:
    """重置任务取消状态（用于测试和新一轮任务开始）。"""
    global _task_cancel_state
    _task_cancel_state = None


def record_portfolio_processed(portfolio_id: int) -> None:
    """记录已处理的组合。"""
    if _task_cancel_state is None:
        return
    _task_cancel_state.processed_portfolios.append(portfolio_id)


def record_portfolio_skipped(portfolio_id: int) -> None:
    """记录跳过的组合（因任务取消）。"""
    if _task_cancel_state is None:
        return
    _task_cancel_state.skipped_portfolios.append(portfolio_id)


def get_task_cancel_state() -> TaskCancelState | None:
    """获取当前任务取消状态（主要用于测试断言）。"""
    return _task_cancel_state


# ----------------------------------------------------------------------------
# 任务错误摘要（每笔失败独立记录，不回滚已合法成交的其他标的）
# ----------------------------------------------------------------------------


def get_task_error_summary(
    *,
    errors: list[dict],
    executed_orders: list[dict],
    skipped_orders: list[dict],
) -> dict[str, Any]:
    """生成任务错误摘要（WP6.5）。

    每笔失败独立记录，不回滚已合法成交的其他标的，但形成任务错误摘要。

    status 取值：
    - "success"：无错误且有执行
    - "partial_success"：有错误也有执行
    - "failed"：全部失败（有错误且无执行）
    - "noop"：无错误无执行（空跑）
    """
    if not errors and not executed_orders:
        status = "noop"
    elif not errors:
        status = "success"
    elif executed_orders:
        status = "partial_success"
    else:
        status = "failed"

    return {
        "total_errors": len(errors),
        "total_executed": len(executed_orders),
        "total_skipped": len(skipped_orders),
        "errors": errors,
        "executed_orders": executed_orders,
        "skipped_orders": skipped_orders,
        "status": status,
    }


# ----------------------------------------------------------------------------
# 订单归因完整性验证（所有新订单可追溯到成员/信号/规则/数据截止时间）
# ----------------------------------------------------------------------------


def verify_order_attribution(order: SimOrder) -> tuple[bool, str]:
    """验证订单归因完整性（WP6.5）。

    所有新订单可追溯到成员/信号/规则/数据截止时间。

    规则：
    - source_type="scan"：member_id 可为空（扫描来源不需要成员）
    - 其他来源（member/legacy/manual）：member_id 必填
    - source_type / signal_id / rule_version_id / execution_mode /
      client_order_key / decision_snapshot_json 均不可为空
    """
    missing: list[str] = []

    if order.member_id is None and order.source_type != "scan":
        missing.append("member_id")
    if order.source_type is None:
        missing.append("source_type")
    if order.signal_id is None:
        missing.append("signal_id")
    if order.rule_version_id is None:
        missing.append("rule_version_id")
    if order.execution_mode is None:
        missing.append("execution_mode")
    if order.client_order_key is None:
        missing.append("client_order_key")
    if order.decision_snapshot_json is None:
        missing.append("decision_snapshot_json")

    if missing:
        return False, f"缺失归因字段：{', '.join(missing)}"
    return True, ""


__all__ = [
    "KLINE_FRESHNESS_HOURS",
    "SCORE_FRESHNESS_HOURS",
    "RULE_FRESHNESS_HOURS",
    "DataHealthResult",
    "TaskCancelState",
    "cancel_task",
    "check_data_health",
    "check_sell_risk",
    "check_task_cancelled",
    "get_task_cancel_state",
    "get_task_error_summary",
    "record_portfolio_processed",
    "record_portfolio_skipped",
    "reset_task_cancel",
    "verify_order_attribution",
]
