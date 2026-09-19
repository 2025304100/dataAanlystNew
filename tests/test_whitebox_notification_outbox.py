"""白盒测试 - WP-MSG.3 Outbox 服务。

覆盖：
1. enqueue 基本流程
2. enqueue 防重（event_key + channel_id 唯一）
3. enqueue 已发送的不防重
4. get_pending_outbox 基本查询
5. get_pending_outbox 按 next_retry_at 排序
6. mark_sending 状态切换
7. mark_sent 标记 + 创建 delivery
8. mark_failed 临时错误指数退避
9. mark_failed 达上限进 dead-letter
10. mark_failed 鉴权失败暂停渠道 + 站内告警
11. _is_auth_error 错误识别
12. _is_rate_limited 错误识别
13. manual_retry 重置状态
14. manual_retry 只对 failed/dead_letter
15. sanitize 错误消息（脱敏）
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.notification import (
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
)
from app.services.notifications.outbox import (
    _is_auth_error,
    _is_rate_limited,
    enqueue,
    get_pending_outbox,
    manual_retry,
    mark_failed,
    mark_sending,
    mark_sent,
)
from app.services.notifications.outbox import _now_utc


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_channel_in_db(
    db_session,
    *,
    name: str = "test-channel",
    channel_type: str = "in_app",
    enabled: bool = True,
    status: str = "enabled",
) -> NotificationChannel:
    """创建并提交一个 channel，返回 refresh 后的实例。"""
    channel = NotificationChannel(
        name=name,
        channel_type=channel_type,
        enabled=enabled,
        status=status,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)
    return channel


def _enqueue_one(
    db_session,
    *,
    channel_id: int,
    event_key: str = "evt-001",
    payload: dict | None = None,
    max_attempts: int = 5,
) -> NotificationOutbox:
    """便捷构造一条 outbox 记录并写入。"""
    if payload is None:
        payload = {"title": "测试", "body": "内容"}
    outbox = NotificationOutbox(
        event_key=event_key,
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload_json=__import__("json").dumps(payload, ensure_ascii=False),
        channel_id=channel_id,
        status="pending",
        attempt_count=0,
        max_attempts=max_attempts,
        next_retry_at=_now_utc(),
        created_at=_now_utc(),
    )
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)
    return outbox


# ----------------------------------------------------------------------------
# 1. enqueue 基本流程
# ----------------------------------------------------------------------------


def test_enqueue_creates_pending_outbox(db_session):
    """【WP-MSG.3】enqueue 创建 status='pending' 的 outbox 记录。"""
    channel = _make_channel_in_db(db_session, name="ch-enqueue-basic")

    outbox = enqueue(
        db_session,
        event_key="evt-enqueue-1",
        source_type="alert",
        source_id=10,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "买入告警", "body": "600000 触发买入"},
        channel_id=channel.id,
    )

    assert outbox is not None
    assert outbox.id is not None
    assert outbox.status == "pending"
    assert outbox.event_key == "evt-enqueue-1"
    assert outbox.channel_id == channel.id
    assert outbox.attempt_count == 0
    assert outbox.max_attempts == 5
    assert outbox.next_retry_at is not None
    assert outbox.created_at is not None
    # payload 已 JSON 序列化
    import json as _json
    parsed = _json.loads(outbox.payload_json)
    assert parsed["title"] == "买入告警"


# ----------------------------------------------------------------------------
# 2. enqueue 防重（event_key + channel_id 唯一）
# ----------------------------------------------------------------------------


def test_enqueue_dedup_same_event_key_and_channel(db_session):
    """【WP-MSG.3】event_key + channel_id 已存在未发送记录 → 返回 None。"""
    channel = _make_channel_in_db(db_session, name="ch-enqueue-dedup")

    first = enqueue(
        db_session,
        event_key="evt-dedup-1",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "第一次", "body": "b1"},
        channel_id=channel.id,
    )
    assert first is not None

    second = enqueue(
        db_session,
        event_key="evt-dedup-1",  # 相同 event_key
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "第二次", "body": "b2"},
        channel_id=channel.id,  # 相同 channel_id
    )
    assert second is None  # 防重，不创建新记录


def test_enqueue_allows_different_channel(db_session):
    """【WP-MSG.3】同一 event_key + 不同 channel_id 允许创建。"""
    ch1 = _make_channel_in_db(db_session, name="ch-enqueue-ch1")
    ch2 = _make_channel_in_db(db_session, name="ch-enqueue-ch2")

    o1 = enqueue(
        db_session,
        event_key="evt-same-event",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "t", "body": "b"},
        channel_id=ch1.id,
    )
    o2 = enqueue(
        db_session,
        event_key="evt-same-event",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "t", "body": "b"},
        channel_id=ch2.id,
    )
    assert o1 is not None
    assert o2 is not None
    assert o1.id != o2.id


# ----------------------------------------------------------------------------
# 3. enqueue 已发送的不防重
# ----------------------------------------------------------------------------


def test_enqueue_sent_record_not_blocking(db_session):
    """【WP-MSG.3】已发送记录不阻止新写入（允许手动重发场景）。"""
    channel = _make_channel_in_db(db_session, name="ch-enqueue-sent")

    first = enqueue(
        db_session,
        event_key="evt-sent-1",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "t", "body": "b"},
        channel_id=channel.id,
    )
    assert first is not None

    # 标记为 sent
    first.status = "sent"
    first.sent_at = _now_utc()
    db_session.commit()

    # 再次 enqueue 相同 event_key + channel_id：应创建新记录
    second = enqueue(
        db_session,
        event_key="evt-sent-1",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload={"title": "t2", "body": "b2"},
        channel_id=channel.id,
    )
    assert second is not None
    assert second.id != first.id


# ----------------------------------------------------------------------------
# 4. get_pending_outbox 基本查询
# ----------------------------------------------------------------------------


def test_get_pending_outbox_returns_all_pending(db_session):
    """【WP-MSG.3】get_pending_outbox 返回所有 pending 且到期记录。"""
    channel = _make_channel_in_db(db_session, name="ch-get-pending")
    for i in range(3):
        _enqueue_one(db_session, channel_id=channel.id, event_key=f"evt-pending-{i}")

    pending = get_pending_outbox(db_session, limit=50)
    assert len(pending) == 3
    assert all(o.status == "pending" for o in pending)


def test_get_pending_outbox_excludes_sending(db_session):
    """【WP-MSG.3】get_pending_outbox 排除 sending/sent 状态。"""
    channel = _make_channel_in_db(db_session, name="ch-get-pending-exclude")
    o1 = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-excl-1")
    o2 = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-excl-2")

    o1.status = "sending"
    o2.status = "sent"
    db_session.commit()

    pending = get_pending_outbox(db_session, limit=50)
    # 应该不返回 sending/sent
    ids = {o.id for o in pending}
    assert o1.id not in ids
    assert o2.id not in ids


# ----------------------------------------------------------------------------
# 5. get_pending_outbox 按 next_retry_at 排序
# ----------------------------------------------------------------------------


def test_get_pending_outbox_ordered_by_next_retry_at(db_session):
    """【WP-MSG.3】get_pending_outbox 按 next_retry_at 升序。"""
    channel = _make_channel_in_db(db_session, name="ch-get-pending-order")
    o_future = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-future")
    o_past = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-past")

    # future：next_retry_at 设为未来
    o_future.next_retry_at = _now_utc() + timedelta(hours=1)
    # past：next_retry_at 设为过去
    o_past.next_retry_at = _now_utc() - timedelta(minutes=10)
    db_session.commit()

    pending = get_pending_outbox(db_session, limit=50)
    # 只返回到期的（past）
    assert len(pending) == 1
    assert pending[0].id == o_past.id


# ----------------------------------------------------------------------------
# 6. mark_sending
# ----------------------------------------------------------------------------


def test_mark_sending_changes_status(db_session):
    """【WP-MSG.3】mark_sending 将 status 改为 'sending'。"""
    channel = _make_channel_in_db(db_session, name="ch-mark-sending")
    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-sending")

    updated = mark_sending(db_session, outbox_id=outbox.id)
    assert updated is not None
    assert updated.status == "sending"


def test_mark_sending_returns_none_for_missing(db_session):
    """【WP-MSG.3】mark_sending 对不存在的 outbox_id 返回 None。"""
    result = mark_sending(db_session, outbox_id=999999)
    assert result is None


# ----------------------------------------------------------------------------
# 7. mark_sent
# ----------------------------------------------------------------------------


def test_mark_sent_creates_delivery_and_sets_status(db_session):
    """【WP-MSG.3】mark_sent 将 status 改为 'sent'，并创建 delivery 记录。"""
    channel = _make_channel_in_db(db_session, name="ch-mark-sent")
    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-sent")
    mark_sending(db_session, outbox_id=outbox.id)

    now = _now_utc()
    delivery = NotificationDelivery(
        outbox_id=outbox.id,
        channel_id=outbox.channel_id,
        attempt_number=1,
        status="success",
        status_code=200,
        response_summary="[站内] 测试",
        error_code=None,
        error_message=None,
        duration_ms=10,
        sent_at=now,
        created_at=now,
    )
    mark_sent(db_session, outbox_id=outbox.id, delivery=delivery)

    # 刷新 outbox
    db_session.refresh(outbox)
    assert outbox.status == "sent"
    assert outbox.sent_at is not None

    # 验证 delivery 已写入
    from sqlalchemy import select
    deliveries = db_session.execute(
        select(NotificationDelivery).where(NotificationDelivery.outbox_id == outbox.id)
    ).scalars().all()
    assert len(deliveries) == 1
    assert deliveries[0].status == "success"
    assert deliveries[0].status_code == 200


# ----------------------------------------------------------------------------
# 8. mark_failed 临时错误指数退避
# ----------------------------------------------------------------------------


def test_mark_failed_temporary_error_backoff(db_session):
    """【WP-MSG.3】临时错误：status='pending', next_retry_at > now, attempt_count += 1。"""
    channel = _make_channel_in_db(db_session, name="ch-mark-failed-temp")
    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-fail-temp")
    original_next_retry = outbox.next_retry_at

    updated = mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="TIMEOUT",
        error_message="请求超时",
        status_code=500,
        duration_ms=5000,
    )
    assert updated is not None
    assert updated.status == "pending"
    assert updated.attempt_count == 1
    # 指数退避：60 * 2^0 = 60 秒后
    assert updated.next_retry_at is not None
    assert updated.next_retry_at > _now_utc()

    # 验证 delivery 已写入
    from sqlalchemy import select
    deliveries = db_session.execute(
        select(NotificationDelivery).where(NotificationDelivery.outbox_id == outbox.id)
    ).scalars().all()
    assert len(deliveries) == 1
    assert deliveries[0].status == "failed"
    assert deliveries[0].error_code == "TIMEOUT"
    assert deliveries[0].status_code == 500


# ----------------------------------------------------------------------------
# 9. mark_failed 达上限进 dead-letter
# ----------------------------------------------------------------------------


def test_mark_failed_reaches_dead_letter(db_session):
    """【WP-MSG.3】达到 max_attempts 后进 dead-letter，next_retry_at=None。"""
    channel = _make_channel_in_db(db_session, name="ch-dead-letter")
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-dead", max_attempts=2
    )

    # 第一次失败：attempt_count=1, status='pending'
    r1 = mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="INTERNAL",
        error_message="内部错误",
        status_code=500,
    )
    assert r1 is not None
    assert r1.attempt_count == 1
    assert r1.status == "pending"

    # 第二次失败：attempt_count=2 >= max_attempts → dead_letter
    r2 = mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="INTERNAL",
        error_message="内部错误",
        status_code=500,
    )
    assert r2 is not None
    assert r2.attempt_count == 2
    assert r2.status == "dead_letter"
    assert r2.next_retry_at is None


# ----------------------------------------------------------------------------
# 10. mark_failed 鉴权失败暂停渠道
# ----------------------------------------------------------------------------


def test_mark_failed_auth_error_suspends_channel(db_session):
    """【WP-MSG.3】鉴权失败：渠道被暂停 + 站内系统告警。"""
    # 主渠道（待会被暂停）
    channel = _make_channel_in_db(
        db_session,
        name="ch-auth-fail",
        channel_type="wxpusher",  # 用非 in_app，避免 in_app 自身被暂停
        enabled=True,
        status="enabled",
    )
    # in_app 渠道（用于接收系统告警）
    in_app_channel = _make_channel_in_db(
        db_session,
        name="ch-in-app-alert",
        channel_type="in_app",
        enabled=True,
        status="enabled",
    )

    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-auth")

    updated = mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="UNAUTHORIZED",
        error_message="Token 无效",
        status_code=401,
        is_auth_fail=True,
    )
    assert updated is not None
    assert updated.status == "failed"  # 鉴权失败直接 failed
    assert updated.next_retry_at is None

    # 验证渠道被暂停
    db_session.refresh(channel)
    assert channel.status == "disabled"
    assert channel.enabled is False
    assert channel.last_error_code == "auth_failed"

    # 验证产生了站内系统告警
    from sqlalchemy import select
    system_alerts = db_session.execute(
        select(NotificationOutbox).where(NotificationOutbox.source_type == "system")
    ).scalars().all()
    assert len(system_alerts) >= 1
    alert = system_alerts[0]
    assert alert.channel_id == in_app_channel.id
    assert alert.event_type == "channel_suspended"
    # payload 中应包含告警 title
    import json as _json
    payload = _json.loads(alert.payload_json)
    assert "鉴权" in payload["title"] or "auth" in payload["title"].lower()


# ----------------------------------------------------------------------------
# 11. _is_auth_error
# ----------------------------------------------------------------------------


def test_is_auth_error_by_status_code():
    """【WP-MSG.3】_is_auth_error 通过 status_code 识别。"""
    assert _is_auth_error(None, 401) is True
    assert _is_auth_error(None, 403) is True
    assert _is_auth_error(None, 500) is False
    assert _is_auth_error(None, 200) is False
    assert _is_auth_error(None, None) is False


def test_is_auth_error_by_error_code():
    """【WP-MSG.3】_is_auth_error 通过 error_code 识别。"""
    assert _is_auth_error("UnauthorizedError", None) is True
    assert _is_auth_error("AUTH_FAILED", None) is True
    assert _is_auth_error("ForbiddenError", None) is True
    assert _is_auth_error("invalid_token", None) is True
    assert _is_auth_error("TIMEOUT", None) is False
    assert _is_auth_error("RATE_LIMIT", None) is False


# ----------------------------------------------------------------------------
# 12. _is_rate_limited
# ----------------------------------------------------------------------------


def test_is_rate_limited_by_status_code():
    """【WP-MSG.3】_is_rate_limited 通过 status_code 识别。"""
    assert _is_rate_limited(None, 429) is True
    assert _is_rate_limited(None, 500) is False
    assert _is_rate_limited(None, 200) is False


def test_is_rate_limited_by_error_code():
    """【WP-MSG.3】_is_rate_limited 通过 error_code 识别。"""
    assert _is_rate_limited("RateLimitExceeded", None) is True
    assert _is_rate_limited("rate_limited", None) is True
    assert _is_rate_limited("TIMEOUT", None) is False


# ----------------------------------------------------------------------------
# 13. manual_retry
# ----------------------------------------------------------------------------


def test_manual_retry_resets_status(db_session):
    """【WP-MSG.3】manual_retry 将 failed 重置为 pending。

    failed 状态仅出现在鉴权失败场景（is_auth_fail=True）；
    临时错误自动退避回 pending，无需手动重发。
    """
    channel = _make_channel_in_db(db_session, name="ch-manual-retry")
    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-retry")

    # 标记为 failed（鉴权失败 → status='failed'）
    mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="UNAUTHORIZED",
        error_message="Token 无效",
        status_code=401,
        is_auth_fail=True,
    )

    # 验证当前为 failed
    db_session.refresh(outbox)
    assert outbox.status == "failed"

    # 手动重发
    updated = manual_retry(db_session, outbox_id=outbox.id)
    assert updated is not None
    assert updated.status == "pending"
    assert updated.next_retry_at is not None
    # attempt_count 不重置（保留历史）
    assert updated.attempt_count >= 1


def test_manual_retry_works_for_dead_letter(db_session):
    """【WP-MSG.3】manual_retry 对 dead_letter 也可重发。"""
    channel = _make_channel_in_db(db_session, name="ch-manual-retry-dl")
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-retry-dl", max_attempts=1
    )

    mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="INTERNAL",
        error_message="内部错误",
    )

    db_session.refresh(outbox)
    assert outbox.status == "dead_letter"

    updated = manual_retry(db_session, outbox_id=outbox.id)
    assert updated is not None
    assert updated.status == "pending"


# ----------------------------------------------------------------------------
# 14. manual_retry 只对 failed/dead_letter
# ----------------------------------------------------------------------------


def test_manual_retry_only_for_failed_or_dead_letter(db_session):
    """【WP-MSG.3】manual_retry 对 pending/sending/sent 返回 None。"""
    channel = _make_channel_in_db(db_session, name="ch-manual-restrict")
    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-restrict")

    # pending 不可重发
    assert outbox.status == "pending"
    result = manual_retry(db_session, outbox_id=outbox.id)
    assert result is None

    # sending 不可重发
    outbox.status = "sending"
    db_session.commit()
    result = manual_retry(db_session, outbox_id=outbox.id)
    assert result is None

    # sent 不可重发
    outbox.status = "sent"
    db_session.commit()
    result = manual_retry(db_session, outbox_id=outbox.id)
    assert result is None


def test_manual_retry_returns_none_for_missing(db_session):
    """【WP-MSG.3】manual_retry 对不存在的记录返回 None。"""
    result = manual_retry(db_session, outbox_id=999999)
    assert result is None


# ----------------------------------------------------------------------------
# 15. sanitize 错误消息（脱敏）
# ----------------------------------------------------------------------------


def test_mark_failed_sanitizes_error_message(db_session):
    """【WP-MSG.3】mark_failed 中 error_message 已脱敏（不暴露 token）。"""
    channel = _make_channel_in_db(db_session, name="ch-sanitize")
    outbox = _enqueue_one(db_session, channel_id=channel.id, event_key="evt-sanitize")

    sensitive_msg = "token=sk-abcdef1234567890xyz 认证失败"
    updated = mark_failed(
        db_session,
        outbox_id=outbox.id,
        error_code="AUTH_ERROR",
        error_message=sensitive_msg,
        status_code=500,
    )
    assert updated is not None

    # outbox.last_error_message 已脱敏
    db_session.refresh(outbox)
    assert "sk-abcdef1234567890xyz" not in (outbox.last_error_message or "")
    assert "***" in (outbox.last_error_message or "")

    # delivery.error_message 已脱敏
    from sqlalchemy import select
    delivery = db_session.execute(
        select(NotificationDelivery).where(NotificationDelivery.outbox_id == outbox.id)
    ).scalars().first()
    assert delivery is not None
    assert "sk-abcdef1234567890xyz" not in (delivery.error_message or "")
    assert "***" in (delivery.error_message or "")
