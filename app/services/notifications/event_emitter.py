"""业务事件触发器（WP-MSG.7）。

将业务事件接入通知系统。
现有 AlertEvent 继续作为正式告警事实，不把每个第三方发送结果塞进 data_json。

project_memory 硬约束：
- 业务事件（AlertEvent/任务完成/成交/数据过期/候选新发现/观察信号满足/自动交易阻断/回撤预警）
  能匹配策略并写入 Outbox
- 现有 AlertEvent 继续作为正式告警事实，不修改其 data_json
- 不重写已稳定业务逻辑（仅在关键点插入 emit_xxx 调用）
- 错误消息不暴露敏感信息
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.notifications.policies import emit_event

logger = logging.getLogger(__name__)


def emit_alert_event(
    db: Session,
    *,
    alert_event_id: int,
    alert_type: str,
    severity: str,
    title: str,
    body: str,
    symbol_id: int | None = None,
    portfolio_id: int | None = None,
) -> list:
    """触发告警事件。

    AlertEvent 继续作为正式告警事实（由调用方创建）。
    本函数仅触发通知，不修改 AlertEvent。
    """
    return emit_event(
        db,
        source_type="system",
        source_id=alert_event_id,
        event_type=alert_type,
        severity=severity,
        title=title,
        body=body,
        scope_type="portfolio" if portfolio_id else None,
        scope_id=portfolio_id,
        template_variables={
            "alert_type": alert_type,
            "symbol": str(symbol_id) if symbol_id else "",
            "title": title,
        },
    )


def emit_task_complete(
    db: Session,
    *,
    task_id: int,
    task_name: str,
    success: bool,
    duration_seconds: float | None = None,
) -> list:
    """触发任务完成事件。"""
    severity = "info" if success else "error"
    title = f"任务完成: {task_name}"
    body = f"任务 ID={task_id}, 状态={'成功' if success else '失败'}"
    if duration_seconds is not None:
        body += f", 耗时 {duration_seconds:.1f}s"

    return emit_event(
        db,
        source_type="system",
        source_id=task_id,
        event_type="task_complete",
        severity=severity,
        title=title,
        body=body,
        template_variables={
            "task_name": task_name,
            "status": "成功" if success else "失败",
        },
    )


def emit_trade_executed(
    db: Session,
    *,
    trade_id: int,
    portfolio_id: int,
    symbol_id: int,
    symbol: str,
    action: str,  # buy/sell
    quantity: float,
    price: float,
) -> list:
    """触发成交事件。"""
    return emit_event(
        db,
        source_type="portfolio",
        source_id=trade_id,
        event_type="trade_executed",
        severity="info",
        title=f"成交: {symbol} {action}",
        body=f"组合 ID={portfolio_id}, 标的={symbol}, 动作={action}, 数量={quantity}, 价格={price}",
        scope_type="portfolio",
        scope_id=portfolio_id,
        template_variables={
            "symbol": symbol,
            "action": action,
            "quantity": str(quantity),
            "price": str(price),
        },
    )


def emit_data_expired(
    db: Session,
    *,
    data_type: str,
    symbol_id: int | None,
    expired_at: str,
) -> list:
    """触发数据过期事件。"""
    return emit_event(
        db,
        source_type="data",
        source_id=symbol_id,
        event_type="data_expired",
        severity="warn",
        title=f"数据过期: {data_type}",
        body=f"数据类型={data_type}, 标的 ID={symbol_id}, 过期时间={expired_at}",
        template_variables={
            "data_type": data_type,
            "expired_at": expired_at,
        },
    )


def emit_discovery_new(
    db: Session,
    *,
    candidate_id: int,
    symbol_id: int,
    symbol: str,
    score: float,
    reason: str | None = None,
) -> list:
    """触发候选新发现事件。"""
    return emit_event(
        db,
        source_type="opportunity",
        source_id=candidate_id,
        event_type="discovery_new",
        severity="info",
        title=f"新候选: {symbol}",
        body=f"标的={symbol}, 评分={score:.2f}" + (f", 原因={reason}" if reason else ""),
        template_variables={
            "symbol": symbol,
            "score": f"{score:.2f}",
            "reason": reason or "",
        },
    )


def emit_signal_matched(
    db: Session,
    *,
    signal_id: int,
    symbol_id: int,
    symbol: str,
    signal_name: str,
    portfolio_id: int | None = None,
) -> list:
    """触发观察信号满足事件。"""
    return emit_event(
        db,
        source_type="opportunity",
        source_id=signal_id,
        event_type="signal_matched",
        severity="info",
        title=f"信号满足: {symbol} - {signal_name}",
        body=f"标的={symbol}, 信号={signal_name}",
        scope_type="portfolio" if portfolio_id else None,
        scope_id=portfolio_id,
        template_variables={
            "symbol": symbol,
            "signal_name": signal_name,
        },
    )


def emit_auto_trade_blocked(
    db: Session,
    *,
    portfolio_id: int,
    symbol_id: int,
    symbol: str,
    reason: str,
    rule_name: str | None = None,
) -> list:
    """触发自动交易阻断事件。"""
    return emit_event(
        db,
        source_type="portfolio",
        source_id=portfolio_id,
        event_type="auto_trade_blocked",
        severity="warn",
        title=f"自动交易阻断: {symbol}",
        body=f"标的={symbol}, 原因={reason}" + (f", 规则={rule_name}" if rule_name else ""),
        scope_type="portfolio",
        scope_id=portfolio_id,
        template_variables={
            "symbol": symbol,
            "reason": reason,
            "rule_name": rule_name or "",
        },
    )


def emit_drawdown_warning(
    db: Session,
    *,
    portfolio_id: int,
    drawdown_pct: float,
    threshold_pct: float,
    current_value: float | None = None,
) -> list:
    """触发回撤预警事件。"""
    severity = "critical" if drawdown_pct >= threshold_pct * 1.5 else "error"
    return emit_event(
        db,
        source_type="portfolio",
        source_id=portfolio_id,
        event_type="drawdown_warning",
        severity=severity,
        title=f"回撤预警: 组合 ID={portfolio_id}",
        body=f"当前回撤={drawdown_pct:.2f}%, 阈值={threshold_pct:.2f}%"
             + (f", 当前市值={current_value:.2f}" if current_value else ""),
        scope_type="portfolio",
        scope_id=portfolio_id,
        template_variables={
            "drawdown_pct": f"{drawdown_pct:.2f}",
            "threshold_pct": f"{threshold_pct:.2f}",
        },
    )


__all__ = [
    "emit_alert_event",
    "emit_task_complete",
    "emit_trade_executed",
    "emit_data_expired",
    "emit_discovery_new",
    "emit_signal_matched",
    "emit_auto_trade_blocked",
    "emit_drawdown_warning",
]
