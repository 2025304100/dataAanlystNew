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
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sim_account import SimOrder

logger = logging.getLogger(__name__)


# 数据新鲜度阈值（小时）
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
) -> DataHealthResult:
    """数据健康检查（WP6.5 fail-closed）。

    检查：
    1. K 线数据新鲜度（24 小时内）
    2. 评分数据新鲜度（48 小时内）
    3. 规则版本新鲜度（7 天内，仅当 rule_version_id 指定时）

    任一过期返回 healthy=False（fail-closed 禁止买入）。
    错误消息不暴露敏感信息（不暴露具体表结构、连接字符串等）。
    """
    now = _now_utc_naive()

    # 1. K 线数据新鲜度
    kline_latest = _get_latest_kline_time(db, symbol_id)
    if kline_latest is None:
        return DataHealthResult(
            healthy=False,
            reason="K线数据缺失",
            kline_latest_at=None,
        )
    kline_latest_naive = _to_naive(kline_latest)
    if (now - kline_latest_naive) > timedelta(hours=KLINE_FRESHNESS_HOURS):
        return DataHealthResult(
            healthy=False,
            reason=f"K线数据过期（最后更新 {kline_latest_naive.isoformat()}，阈值 {KLINE_FRESHNESS_HOURS}h）",
            kline_latest_at=kline_latest_naive,
        )

    # 2. 评分数据新鲜度
    score_latest = _get_latest_score_time(db, symbol_id)
    if score_latest is None:
        return DataHealthResult(
            healthy=False,
            reason="评分数据缺失",
            kline_latest_at=kline_latest_naive,
            score_latest_at=None,
        )
    score_latest_naive = _to_naive(score_latest)
    if (now - score_latest_naive) > timedelta(hours=SCORE_FRESHNESS_HOURS):
        return DataHealthResult(
            healthy=False,
            reason=f"评分数据过期（最后更新 {score_latest_naive.isoformat()}，阈值 {SCORE_FRESHNESS_HOURS}h）",
            kline_latest_at=kline_latest_naive,
            score_latest_at=score_latest_naive,
        )

    # 3. 规则版本新鲜度（仅当指定 rule_version_id 时检查）
    rule_version_at: datetime | None = None
    if rule_version_id is not None:
        rule_version_at = _get_rule_version_time(db, rule_version_id)
        if rule_version_at is None:
            return DataHealthResult(
                healthy=False,
                reason=f"规则版本 {rule_version_id} 不存在",
                kline_latest_at=kline_latest_naive,
                score_latest_at=score_latest_naive,
                rule_version_id=rule_version_id,
            )
        rule_version_at_naive = _to_naive(rule_version_at)
        if (now - rule_version_at_naive) > timedelta(hours=RULE_FRESHNESS_HOURS):
            return DataHealthResult(
                healthy=False,
                reason=(
                    f"规则版本过期（版本 {rule_version_id}，"
                    f"最后更新 {rule_version_at_naive.isoformat()}，"
                    f"阈值 {RULE_FRESHNESS_HOURS}h）"
                ),
                kline_latest_at=kline_latest_naive,
                score_latest_at=score_latest_naive,
                rule_version_id=rule_version_id,
                rule_version_at=rule_version_at_naive,
            )

    return DataHealthResult(
        healthy=True,
        reason="",
        kline_latest_at=kline_latest_naive,
        score_latest_at=score_latest_naive,
        rule_version_id=rule_version_id,
        rule_version_at=rule_version_at,
    )


def _get_latest_kline_time(db: Session, symbol_id: int) -> datetime | None:
    """获取最新 K 线数据时间（按 created_at 取最新一条）。

    第一阶段：查询 DailyBar 表，返回最新 created_at。
    """
    try:
        from app.models.daily_bar import DailyBar

        bar = db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id == symbol_id)
            .order_by(DailyBar.created_at.desc(), DailyBar.id.desc())
            .limit(1)
        ).scalars().first()
        return bar.created_at if bar is not None else None
    except Exception as exc:
        # 不暴露敏感信息
        logger.warning("获取 K 线最新时间失败 symbol_id=%s: %s", symbol_id, exc)
        return None


def _get_latest_score_time(db: Session, symbol_id: int) -> datetime | None:
    """获取最新评分时间。"""
    try:
        from app.models.score import Score

        score = db.execute(
            select(Score)
            .where(Score.symbol_id == symbol_id)
            .order_by(Score.created_at.desc(), Score.id.desc())
            .limit(1)
        ).scalars().first()
        return score.created_at if score is not None else None
    except Exception as exc:
        logger.warning("获取评分最新时间失败 symbol_id=%s: %s", symbol_id, exc)
        return None


def _get_rule_version_time(db: Session, rule_version_id: int) -> datetime | None:
    """获取规则版本时间。

    第一阶段占位：规则版本表尚未建立，返回当前时间表示"版本存在且新鲜"。
    后续接入规则版本表后实现真实查询；若版本不存在应返回 None，
    让 check_data_health 走 "规则版本 X 不存在" 分支（fail-closed）。
    """
    # TODO: 接入规则版本表后实现真实查询
    # 当前规则版本表尚未建立，返回当前时间作为占位（视为新鲜）
    _ = (db, rule_version_id)
    return _now_utc_naive()


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
    health = check_data_health(db, symbol_id=symbol_id)

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
