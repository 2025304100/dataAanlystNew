"""告警评估引擎：根据规则检查当前数据状态，触发告警事件。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.manager import DatabaseManager
from app.db.session import get_session_local
from app.models.alert import AlertEvent, AlertRule
from app.models.daily_bar import DailyBar
from app.models.discovery import DiscoveryTaskRecord
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.factors.score_scope import get_active_score_scope

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_date(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _load_config(rule: AlertRule) -> dict:
    if not rule.config_json:
        return {}
    try:
        return json.loads(rule.config_json)
    except json.JSONDecodeError:
        # 风控加固：config_json 损坏时记录告警规则 ID 与原始片段，便于运维定位
        logger.warning(
            "AlertRule.%s config_json parse failed (JSONDecodeError), fallback to empty dict. raw=%r",
            rule.id, rule.config_json[:200],
        )
        return {}
    except Exception:
        logger.exception(
            "AlertRule %s config_json parse failed (unexpected error), fallback to empty dict",
            rule.id,
        )
        return {}


def _in_cooldown(rule: AlertRule) -> bool:
    if not rule.last_triggered_at:
        return False
    cutoff = _now() - timedelta(minutes=rule.cooldown_minutes)
    return rule.last_triggered_at > cutoff


def _fire_event(session: Session, rule: AlertRule, title: str, message: str,
                symbol_id: int | None = None, data: dict | None = None) -> AlertEvent:
    event = AlertEvent(
        rule_id=rule.id,
        alert_type=rule.alert_type,
        severity=rule.severity,
        title=title,
        message=message,
        symbol_id=symbol_id,
        data_json=json.dumps(data, ensure_ascii=False) if data else None,
    )
    session.add(event)
    rule.last_triggered_at = _now()
    return event


# ── 各类型评估器 ──────────────────────────────────────────


def _eval_score_drop(session: Session, rule: AlertRule) -> list[AlertEvent]:
    """评分跌破阈值：检查最近评分是否低于阈值。"""
    cfg = _load_config(rule)
    threshold = float(cfg.get("threshold", 40))
    symbol_ids = cfg.get("symbol_ids")  # None = 全部
    events = []
    scope = get_active_score_scope(session)

    # 找每个标的最新评分
    latest_score_subq = (
        select(Score.symbol_id, func.max(Score.trade_date).label("max_date"))
        .where(Score.weight_mode == scope.weight_mode)
        .where(
            Score.factor_model_run_id == scope.model_run_id
            if scope.weight_mode == 'ridge'
            else True
        )
        .group_by(Score.symbol_id)
        .subquery()
    )
    stmt = (
        select(Score.symbol_id, Score.priority_score, Score.trade_date)
        .join(
            latest_score_subq,
            (Score.symbol_id == latest_score_subq.c.symbol_id)
            & (Score.trade_date == latest_score_subq.c.max_date),
        )
        .where(
            Score.priority_score < threshold,
            Score.weight_mode == scope.weight_mode,
        )
    )
    if scope.weight_mode == 'ridge':
        stmt = stmt.where(Score.factor_model_run_id == scope.model_run_id)
    if symbol_ids:
        stmt = stmt.where(Score.symbol_id.in_(symbol_ids))

    rows = session.execute(stmt).all()
    # 风控加固：批量预加载 Symbol 避免 N+1
    symbol_ids = [row[0] for row in rows]
    symbol_map: dict[int, Symbol] = {}
    if symbol_ids:
        for sym in session.execute(
            select(Symbol).where(Symbol.id.in_(symbol_ids))
        ).scalars().all():
            symbol_map[sym.id] = sym

    for symbol_id, priority_score, trade_date in rows:
        sym = symbol_map.get(symbol_id)
        label = f"{sym.symbol} {sym.name}" if sym else f"#{symbol_id}"
        events.append(_fire_event(
            session, rule,
            title=f"评分跌破阈值: {label}",
            message=f"{label} 最新评分 {priority_score:.1f}（{trade_date}）低于阈值 {threshold}",
            symbol_id=symbol_id,
            data={"priority_score": priority_score, "threshold": threshold, "trade_date": str(trade_date)},
        ))
    return events


def _eval_data_stale(session: Session, rule: AlertRule) -> list[AlertEvent]:
    """数据过期：检查K线最后更新日期是否超过阈值天数。"""
    cfg = _load_config(rule)
    stale_days = int(cfg.get("stale_days", 7))
    symbol_ids = cfg.get("symbol_ids")
    events = []
    cutoff = _now().date() - timedelta(days=stale_days)

    latest_bar_subq = (
        select(DailyBar.symbol_id, func.max(DailyBar.trade_date).label("latest_date"))
        .group_by(DailyBar.symbol_id)
        .subquery()
    )
    stmt = (
        select(Symbol.id, Symbol.symbol, Symbol.name, latest_bar_subq.c.latest_date)
        .outerjoin(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1)
    )
    if symbol_ids:
        stmt = stmt.where(Symbol.id.in_(symbol_ids))

    rows = session.execute(stmt).all()
    stale_count = 0
    for symbol_id, symbol_code, name, latest_date in rows:
        if stale_count >= 50:
            break
        parsed = _parse_date(latest_date)
        if parsed is None or parsed.date() < cutoff:
            age = (_now().date() - (parsed.date() if parsed else _now().date())).days
            label = f"{symbol_code} {name}"
            events.append(_fire_event(
                session, rule,
                title=f"数据过期: {label}",
                message=f"{label} K线已 {age} 天未更新（最后: {parsed.date() if parsed else '无记录'}）",
                symbol_id=symbol_id,
                data={"stale_days": age, "latest_date": str(parsed.date()) if parsed else None},
            ))
            stale_count += 1
    return events


def _eval_task_failed(session: Session, rule: AlertRule) -> list[AlertEvent]:
    """任务失败：检查最近是否有失败的异步任务。"""
    cfg = _load_config(rule)
    lookback_hours = int(cfg.get("lookback_hours", 24))
    cutoff = _now() - timedelta(hours=lookback_hours)
    events = []

    # 检查 discovery_tasks
    failed_disc = session.execute(
        select(DiscoveryTaskRecord).where(
            DiscoveryTaskRecord.status == "failed",
            DiscoveryTaskRecord.updated_at >= cutoff,
        ).order_by(DiscoveryTaskRecord.updated_at.desc()).limit(5)
    ).scalars().all()

    for task in failed_disc:
        events.append(_fire_event(
            session, rule,
            title=f"机会挖掘任务失败",
            message=f"任务 {task.id[:8]}... 于 {task.updated_at} 失败，状态: {task.stage}，已处理 {task.processed}/{task.total}",
            data={"task_id": task.id, "task_type": "discovery", "stage": task.stage,
                  "processed": task.processed, "total": task.total},
        ))

    # 检查 async_tasks
    from app.models.async_task import AsyncTaskRecord
    failed_async = session.execute(
        select(AsyncTaskRecord).where(
            AsyncTaskRecord.status == "failed",
            AsyncTaskRecord.updated_at >= cutoff,
        ).order_by(AsyncTaskRecord.updated_at.desc()).limit(5)
    ).scalars().all()

    for task in failed_async:
        events.append(_fire_event(
            session, rule,
            title=f"{task.task_type} 任务失败",
            message=f"任务 {task.id[:8]}... 于 {task.updated_at} 失败: {task.message or task.stage}",
            data={"task_id": task.id, "task_type": task.task_type, "stage": task.stage,
                  "message": task.message},
        ))
    return events


def _eval_indicator_trigger(session: Session, rule: AlertRule) -> list[AlertEvent]:
    """指标触发：检查自定义指标是否满足条件（预留，暂不实现复杂逻辑）。"""
    # 预留：后续可以接入自定义指标求值
    return []


EVALUATORS = {
    "score_drop": _eval_score_drop,
    "data_stale": _eval_data_stale,
    "task_failed": _eval_task_failed,
    "indicator_trigger": _eval_indicator_trigger,
}


# ── 公共接口 ──────────────────────────────────────────────


def evaluate_all_rules() -> list[dict]:
    """评估所有启用的规则，返回新触发的告警事件摘要。

    风控加固：单规则失败已 warning 吞没（可恢复），但顶层异常（DB 连接断开等
    不可恢复异常）会 re-raise 让路由层返回 5xx，避免静默失败误导调用方。
    """
    new_events: list[dict] = []
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        rules = session.execute(
            select(AlertRule).where(AlertRule.enabled == 1)
        ).scalars().all()

        for rule in rules:
            if _in_cooldown(rule):
                continue
            evaluator = EVALUATORS.get(rule.alert_type)
            if not evaluator:
                continue
            try:
                fired = evaluator(session, rule)
                for event in fired:
                    new_events.append({
                        "id": None,
                        "rule_id": rule.id,
                        "alert_type": rule.alert_type,
                        "severity": rule.severity,
                        "title": event.title,
                        "message": event.message,
                        "symbol_id": event.symbol_id,
                    })
            except Exception as exc:
                # 单规则失败可恢复，记录 warning 后继续评估其他规则
                logger.warning("Alert rule %s (%s) evaluation failed: %s", rule.id, rule.alert_type, exc)

        if new_events:
            session.commit()
            recent = session.execute(
                select(AlertEvent)
                .where(AlertEvent.acknowledged == 0)
                .order_by(AlertEvent.created_at.desc())
                .limit(len(new_events) + 5)
            ).scalars().all()
            for i, ev in enumerate(recent):
                if i < len(new_events):
                    new_events[i]["id"] = ev.id
                    new_events[i]["created_at"] = ev.created_at.isoformat() if ev.created_at else None
    except Exception:
        # 顶层异常（DB 连接断开、session 异常等不可恢复异常）必须 re-raise
        # 让路由层返回 5xx，避免静默吞没导致调用方误以为评估成功
        logger.exception("evaluate_all_rules failed with unrecoverable error")
        raise
    finally:
        session.close()

    return new_events


def _safe_load_data_json(ev: AlertEvent) -> dict:
    """统一解析 AlertEvent.data_json，损坏时记录日志而非静默吞没。"""
    if not ev.data_json:
        return {}
    try:
        return json.loads(ev.data_json)
    except json.JSONDecodeError:
        logger.warning(
            "AlertEvent %s data_json parse failed (json decode error), fallback to empty dict. raw=%r",
            ev.id, ev.data_json[:200],
        )
        return {}
    except Exception:
        logger.exception(
            "AlertEvent %s data_json parse failed (unexpected error), fallback to empty dict",
            ev.id,
        )
        return {}


def get_active_alerts(limit: int = 50) -> list[dict]:
    """获取未确认的告警事件列表。"""
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AlertEvent)
            .where(AlertEvent.acknowledged == 0)
            .order_by(AlertEvent.created_at.desc())
            .limit(limit)
        ).scalars().all()

        # 风控加固：批量预加载 Symbol 避免 N+1
        symbol_ids = [ev.symbol_id for ev in rows if ev.symbol_id]
        symbol_map: dict[int, Symbol] = {}
        if symbol_ids:
            for sym in session.execute(
                select(Symbol).where(Symbol.id.in_(symbol_ids))
            ).scalars().all():
                symbol_map[sym.id] = sym

        result = []
        for ev in rows:
            data = _safe_load_data_json(ev)
            symbol_info = None
            if ev.symbol_id:
                sym = symbol_map.get(ev.symbol_id)
                if sym:
                    symbol_info = {"id": sym.id, "symbol": sym.symbol, "name": sym.name}

            result.append({
                "id": ev.id,
                "rule_id": ev.rule_id,
                "alert_type": ev.alert_type,
                "severity": ev.severity,
                "title": ev.title,
                "message": ev.message,
                "symbol_id": ev.symbol_id,
                "symbol": symbol_info,
                "data": data,
                "acknowledged": ev.acknowledged,
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            })
        return result
    finally:
        session.close()


def acknowledge_alert(event_id: int) -> bool:
    """确认（消除）一条告警。"""
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        ev = session.get(AlertEvent, event_id)
        if not ev:
            return False
        ev.acknowledged = 1
        session.commit()
        return True
    finally:
        session.close()


def acknowledge_all_alerts() -> int:
    """确认所有未读告警。"""
    SessionLocal = get_session_local()
    session = SessionLocal()
    try:
        count = session.execute(
            select(func.count(AlertEvent.id)).where(AlertEvent.acknowledged == 0)
        ).scalar_one()
        session.execute(
            AlertEvent.__table__.update().where(AlertEvent.acknowledged == 0).values(acknowledged=1)
        )
        session.commit()
        return count
    finally:
        session.close()
