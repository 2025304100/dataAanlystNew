"""Outbox 服务（WP-MSG.3）。

业务事务只写 Outbox，不直接等待第三方。
event_key + channel_id 唯一约束防重。

project_memory 硬约束：
- 业务事务只写 Outbox，不直接等待第三方
- event_key + channel_id 唯一约束生效
- 超时与临时错误指数退避，达上限进 dead-letter
- 鉴权失败直接暂停渠道并产生站内系统告警
- 错误消息不暴露敏感信息（sanitize_message）
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.notification import (
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
)
from app.schemas.error_sanitizer import sanitize_message


logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    """返回无时区信息的 UTC 当前时间（与项目其它模型保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_auth_error(error_code: str | None, status_code: int | None) -> bool:
    """判断是否鉴权失败。

    - HTTP 401/403 视为鉴权失败
    - error_code 包含 auth/unauthorized/forbidden/token 视为鉴权失败
    """
    if status_code in (401, 403):
        return True
    if error_code and any(
        k in error_code.lower() for k in ("auth", "unauthorized", "forbidden", "token")
    ):
        return True
    return False


def _is_rate_limited(error_code: str | None, status_code: int | None) -> bool:
    """判断是否限流。

    - HTTP 429 视为限流
    - error_code 包含 rate 视为限流
    """
    if status_code == 429:
        return True
    if error_code and "rate" in error_code.lower():
        return True
    return False


def enqueue(
    db: Session,
    *,
    event_key: str,
    source_type: str,
    source_id: int | None,
    event_type: str,
    severity: str,
    payload: dict,
    channel_id: int,
    policy_id: int | None = None,
) -> NotificationOutbox | None:
    """将业务事件写入 Outbox（业务事务内调用）。

    event_key + channel_id 唯一约束：已存在且未发送时返回 None（防重）。
    已发送的记录不阻止新写入（允许手动重发到其他渠道场景）。

    返回：
        NotificationOutbox：新创建的记录
        None：已存在未发送的相同 event_key + channel_id 记录（防重）
    """
    # 检查是否已有未发送的相同 event_key + channel_id 记录
    existing = db.execute(
        select(NotificationOutbox).where(
            and_(
                NotificationOutbox.event_key == event_key,
                NotificationOutbox.channel_id == channel_id,
                NotificationOutbox.status != "sent",
            )
        )
    ).scalars().first()

    if existing is not None:
        logger.debug(
            f"Outbox 防重：event_key={event_key}, channel_id={channel_id} 已存在未发送记录"
        )
        return None

    outbox = NotificationOutbox(
        event_key=event_key,
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        severity=severity,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
        channel_id=channel_id,
        policy_id=policy_id,
        status="pending",
        attempt_count=0,
        max_attempts=5,
        next_retry_at=_now_utc(),
        created_at=_now_utc(),
    )
    db.add(outbox)
    db.commit()
    db.refresh(outbox)
    return outbox


def get_pending_outbox(
    db: Session,
    *,
    limit: int = 50,
) -> list[NotificationOutbox]:
    """获取待发送的 Outbox 记录。

    条件：status='pending' 且 next_retry_at <= now
    按 next_retry_at 升序（最早该重试的先处理）。
    """
    now = _now_utc()
    return list(
        db.execute(
            select(NotificationOutbox)
            .where(
                and_(
                    NotificationOutbox.status == "pending",
                    NotificationOutbox.next_retry_at <= now,
                )
            )
            .order_by(NotificationOutbox.next_retry_at.asc())
            .limit(limit)
        ).scalars().all()
    )


def mark_sending(db: Session, *, outbox_id: int) -> NotificationOutbox | None:
    """标记为发送中。"""
    outbox = db.get(NotificationOutbox, outbox_id)
    if outbox is None:
        return None
    outbox.status = "sending"
    outbox.updated_at = _now_utc()
    db.commit()
    return outbox


def mark_sent(db: Session, *, outbox_id: int, delivery: NotificationDelivery) -> None:
    """标记为已发送。"""
    outbox = db.get(NotificationOutbox, outbox_id)
    if outbox is None:
        return
    outbox.status = "sent"
    outbox.sent_at = _now_utc()
    outbox.updated_at = _now_utc()
    db.add(delivery)
    db.commit()


def mark_failed(
    db: Session,
    *,
    outbox_id: int,
    error_code: str | None,
    error_message: str | None,
    status_code: int | None = None,
    duration_ms: int = 0,
    response_summary: str | None = None,
    is_auth_fail: bool = False,
) -> NotificationOutbox | None:
    """标记为失败，记录 delivery，计算下次重试或进 dead-letter。

    - 鉴权失败：暂停渠道 + 站内系统告警 + 直接 failed
    - 达到 max_attempts：进 dead-letter
    - 临时错误：指数退避计算 next_retry_at
    """
    outbox = db.get(NotificationOutbox, outbox_id)
    if outbox is None:
        return None

    now = _now_utc()
    outbox.attempt_count += 1
    outbox.last_error_code = error_code
    outbox.last_error_message = (
        sanitize_message(error_message) if error_message else None
    )

    # 记录 delivery
    delivery = NotificationDelivery(
        outbox_id=outbox_id,
        channel_id=outbox.channel_id,
        attempt_number=outbox.attempt_count,
        status="auth_failed" if is_auth_fail else "failed",
        status_code=status_code,
        response_summary=(
            sanitize_message(response_summary)[:500] if response_summary else None
        ),
        error_code=error_code,
        error_message=(
            sanitize_message(error_message) if error_message else None
        ),
        duration_ms=duration_ms,
        sent_at=now,
        created_at=now,
    )
    db.add(delivery)

    # 鉴权失败：暂停渠道 + 站内系统告警 + 直接 failed
    if is_auth_fail:
        _suspend_channel(db, channel_id=outbox.channel_id, reason="鉴权失败")
        _create_in_app_system_alert(
            db,
            title="渠道鉴权失败已暂停",
            body=f"渠道 ID={outbox.channel_id} 鉴权失败，已自动暂停。请检查配置。",
            severity="error",
        )
        outbox.status = "failed"
        outbox.next_retry_at = None
    # 达到上限：进 dead-letter
    elif outbox.attempt_count >= outbox.max_attempts:
        outbox.status = "dead_letter"
        outbox.next_retry_at = None
    # 临时错误：指数退避
    else:
        outbox.status = "pending"
        # 指数退避：base * 2^attempt，封顶 30 分钟
        backoff_seconds = min(60 * (2 ** (outbox.attempt_count - 1)), 1800)
        outbox.next_retry_at = now + timedelta(seconds=backoff_seconds)

    outbox.updated_at = now
    db.commit()
    return outbox


def _suspend_channel(db: Session, *, channel_id: int, reason: str) -> None:
    """暂停渠道。"""
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        return
    channel.status = "disabled"
    channel.enabled = False
    channel.last_error_code = "auth_failed"
    channel.last_error_message = sanitize_message(reason)
    channel.updated_at = _now_utc()
    db.commit()


def _create_in_app_system_alert(
    db: Session,
    *,
    title: str,
    body: str,
    severity: str = "error",
) -> None:
    """产生站内系统告警（WP-MSG.3 渠道故障隔离）。

    使用 in_app 渠道发送，确保即使外部渠道全部失败也能通知用户。
    系统告警的 event_key 含时间戳，确保不防重。
    """
    # 查找 in_app 渠道（启用 + enabled=True）
    in_app_channel = db.execute(
        select(NotificationChannel).where(
            and_(
                NotificationChannel.channel_type == "in_app",
                NotificationChannel.enabled == True,  # noqa: E712
            )
        )
    ).scalars().first()

    if in_app_channel is None:
        logger.warning("无可用 in_app 渠道，站内系统告警未发送")
        return

    # 写入 Outbox（系统告警不防重，确保一定送达）
    enqueue(
        db,
        event_key=f"system_alert:{_now_utc().isoformat()}:{title}",
        source_type="system",
        source_id=None,
        event_type="channel_suspended",
        severity=severity,
        payload={"title": title, "body": body},
        channel_id=in_app_channel.id,
    )


def manual_retry(db: Session, *, outbox_id: int) -> NotificationOutbox | None:
    """用户手动重发失败记录。

    - 重置状态为 pending
    - attempt_count 不重置（保留历史）
    - 允许用户继续使用同一业务事件审计链
    """
    outbox = db.get(NotificationOutbox, outbox_id)
    if outbox is None:
        return None
    if outbox.status not in ("failed", "dead_letter"):
        return None  # 只有 failed/dead_letter 可重发

    outbox.status = "pending"
    outbox.next_retry_at = _now_utc()
    outbox.updated_at = _now_utc()
    db.commit()
    return outbox


__all__ = [
    "enqueue",
    "get_pending_outbox",
    "mark_sending",
    "mark_sent",
    "mark_failed",
    "manual_retry",
    "_is_auth_error",
    "_is_rate_limited",
    "_create_in_app_system_alert",
]
