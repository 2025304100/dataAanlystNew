"""防打扰与去重（WP-MSG.5）。

- 去重窗口：相同来源/标的/规则/状态在窗口内只发送一次
- 冷却：同一来源在冷却期内只发送一次（跨 event_type）
- 免打扰时间：交易日/工作日/免打扰时间段
- 高频 info/warn 合并摘要；error 默认即时发送
- 最高风险（critical）是否绕过免打扰由用户明确设置

project_memory 硬约束：
- 相同来源/标的/规则/状态在去重窗口内只发送一次（spec line 126）
- 高频 info/warn 合并摘要；error 默认即时发送（spec line 127）
- 支持交易日/工作日/免打扰时间；最高风险是否绕过免打扰由用户明确设置（spec line 127）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.notification import NotificationOutbox, NotificationPolicy


def _now_utc() -> datetime:
    """返回无时区信息的 UTC 当前时间（与项目其它模型保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_in_dedup_window(
    db: Session,
    *,
    source_type: str,
    source_id: int | None,
    event_type: str,
    channel_id: int,
    dedup_window_minutes: int,
) -> bool:
    """检查是否在去重窗口内。

    相同来源/标的/规则/状态在去重窗口内只发送一次。
    检查最近 dedup_window_minutes 内是否已有相同
    event_type + source_id + channel_id 的记录。

    Args:
        db: 数据库会话
        source_type: 来源类型
        source_id: 来源对象 ID（可为空）
        event_type: 事件类型
        channel_id: 渠道 ID
        dedup_window_minutes: 去重窗口分钟数（<=0 表示不限制）

    Returns:
        True 表示在去重窗口内（应跳过）；False 表示可发送
    """
    if dedup_window_minutes <= 0:
        return False

    cutoff = _now_utc() - timedelta(minutes=dedup_window_minutes)

    query = select(NotificationOutbox).where(
        and_(
            NotificationOutbox.source_type == source_type,
            NotificationOutbox.event_type == event_type,
            NotificationOutbox.channel_id == channel_id,
            NotificationOutbox.created_at >= cutoff,
        )
    )
    if source_id is not None:
        query = query.where(NotificationOutbox.source_id == source_id)

    existing = db.execute(query).scalars().first()
    return existing is not None


def is_in_cooldown(
    db: Session,
    *,
    source_type: str,
    source_id: int | None,
    channel_id: int,
    cooldown_minutes: int,
) -> bool:
    """检查是否在冷却期内。

    同一来源在冷却期内只发送一次（跨 event_type）。

    Args:
        db: 数据库会话
        source_type: 来源类型
        source_id: 来源对象 ID（可为空）
        channel_id: 渠道 ID
        cooldown_minutes: 冷却分钟数（<=0 表示不限制）

    Returns:
        True 表示在冷却期内（应跳过）；False 表示可发送
    """
    if cooldown_minutes <= 0:
        return False

    cutoff = _now_utc() - timedelta(minutes=cooldown_minutes)

    query = select(NotificationOutbox).where(
        and_(
            NotificationOutbox.source_type == source_type,
            NotificationOutbox.channel_id == channel_id,
            NotificationOutbox.created_at >= cutoff,
        )
    )
    if source_id is not None:
        query = query.where(NotificationOutbox.source_id == source_id)

    existing = db.execute(query).scalars().first()
    return existing is not None


def is_in_quiet_hours(
    quiet_hours_json: str | None,
    now: datetime | None = None,
) -> bool:
    """检查当前是否在免打扰时间段。

    quiet_hours_json 格式：
    {
        "start": "22:00",
        "end": "08:00",
        "timezone": "Asia/Shanghai",
        "bypass_for_critical": false,
        "trading_days_only": false
    }

    跨天情况：start > end 表示跨天（如 22:00 - 08:00）。

    Args:
        quiet_hours_json: 免打扰配置 JSON 字符串
        now: 当前时间（可选，用于测试）

    Returns:
        True 表示在免打扰时间段内；False 表示不在或无配置
    """
    if not quiet_hours_json:
        return False

    try:
        config = json.loads(quiet_hours_json)
    except Exception:
        return False

    start_str = config.get("start")
    end_str = config.get("end")
    if not start_str or not end_str:
        return False

    # 时区处理（简化：使用本地时间，生产环境需处理时区）
    now = now or _now_utc()

    # 交易日/工作日检查（trading_days_only=true 时周末不算交易日）
    trading_days_only = config.get("trading_days_only", False)
    if trading_days_only and now.weekday() >= 5:
        return False

    start_time = datetime.strptime(start_str, "%H:%M").time()
    end_time = datetime.strptime(end_str, "%H:%M").time()
    now_time = now.time()

    # 跨天情况（如 22:00 - 08:00）
    if start_time > end_time:
        return now_time >= start_time or now_time < end_time
    # 同天情况（如 12:00 - 14:00）
    return start_time <= now_time < end_time


def should_bypass_quiet_hours(
    quiet_hours_json: str | None,
    severity: str,
) -> bool:
    """检查严重级别是否绕过免打扰。

    最高风险（critical）是否绕过免打扰由用户明确设置
    （quiet_hours_json.bypass_for_critical）。

    Args:
        quiet_hours_json: 免打扰配置 JSON 字符串
        severity: 严重级别 info/warn/error/critical

    Returns:
        True 表示可绕过免打扰（应发送）；False 表示不可绕过
    """
    if not quiet_hours_json:
        return True  # 无免打扰配置，总是发送

    try:
        config = json.loads(quiet_hours_json)
    except Exception:
        return True

    bypass = config.get("bypass_for_critical", False)
    return bool(bypass) and severity == "critical"


def should_send_now(
    db: Session,
    *,
    policy: NotificationPolicy,
    source_type: str,
    source_id: int | None,
    event_type: str,
    channel_id: int,
    severity: str,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """综合判断是否应该发送。

    顺序：
    1. error/critical 默认即时发送（不受去重/冷却限制），
       但受免打扰限制（critical 可由用户设置绕过）
    2. info/warn 检查去重窗口 → 冷却 → 免打扰

    Args:
        db: 数据库会话
        policy: 推送策略（含 quiet_hours_json/cooldown_minutes/dedup_window_minutes）
        source_type: 来源类型
        source_id: 来源对象 ID
        event_type: 事件类型
        channel_id: 渠道 ID
        severity: 严重级别
        now: 当前时间（可选，用于测试；为空时使用 _now_utc()）

    Returns:
        (should_send, reason)：reason 为空表示发送，非空表示跳过原因
    """
    # error/critical 默认即时发送（不受去重/冷却限制）
    if severity in ("error", "critical"):
        # 但受免打扰限制（critical 可绕过）
        if is_in_quiet_hours(policy.quiet_hours_json, now=now):
            if not should_bypass_quiet_hours(policy.quiet_hours_json, severity):
                return False, "免打扰时间，未设置绕过"
        return True, ""

    # info/warn 检查去重窗口
    if is_in_dedup_window(
        db,
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        channel_id=channel_id,
        dedup_window_minutes=policy.dedup_window_minutes,
    ):
        return False, "去重窗口内"

    # 检查冷却
    if is_in_cooldown(
        db,
        source_type=source_type,
        source_id=source_id,
        channel_id=channel_id,
        cooldown_minutes=policy.cooldown_minutes,
    ):
        return False, "冷却期内"

    # 检查免打扰
    if is_in_quiet_hours(policy.quiet_hours_json, now=now):
        return False, "免打扰时间"

    return True, ""


__all__ = [
    "is_in_dedup_window",
    "is_in_cooldown",
    "is_in_quiet_hours",
    "should_bypass_quiet_hours",
    "should_send_now",
]
