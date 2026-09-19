"""白盒测试 - WP-MSG.5 防打扰与隐私。

覆盖：
1.  is_in_dedup_window 窗口内
2.  is_in_dedup_window 窗口外
3.  is_in_dedup_window 窗口=0
4.  is_in_dedup_window 不同 event_type
5.  is_in_cooldown 冷却期内
6.  is_in_quiet_hours 跨天
7.  is_in_quiet_hours 同天
8.  is_in_quiet_hours 无配置
9.  is_in_quiet_hours trading_days_only
10. should_bypass_quiet_hours critical
11. should_bypass_quiet_hours 未设置
12. should_send_now error 即时发送
13. should_send_now info 去重窗口内
14. should_send_now info 免打扰时间
15. should_send_now critical 绕过免打扰
16. sanitize_payload 移除敏感字段
17. sanitize_payload 白名单
18. truncate_content 截断
19. build_summary
20. get_in_app_payload 保留完整
21. emit_event 集成防打扰 - 去重跳过
22. emit_event error 不受去重限制
23. emit_event 第三方渠道摘要
24. sanitize_value 递归
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.notification import (
    NotificationChannel,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
)
from app.services.notifications.anti_disturb import (
    is_in_cooldown,
    is_in_dedup_window,
    is_in_quiet_hours,
    should_bypass_quiet_hours,
    should_send_now,
)
from app.services.notifications.content_sanitizer import (
    _sanitize_value,
    build_summary,
    get_in_app_payload,
    sanitize_payload,
    truncate_content,
)
from app.services.notifications.outbox import _now_utc
from app.services.notifications.policies import emit_event


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_channel(
    db_session,
    *,
    name: str = "ch",
    channel_type: str = "in_app",
    enabled: bool = True,
    status: str = "enabled",
) -> NotificationChannel:
    """创建并提交一个 channel。"""
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


def _make_policy(
    db_session,
    *,
    name: str = "p",
    enabled: bool = True,
    source_types: list[str] | None = None,
    min_severity: str = "info",
    scope_type: str = "all",
    scope_ids: list[int] | None = None,
    dedup_window_minutes: int = 0,
    cooldown_minutes: int = 0,
    quiet_hours_json: str | None = None,
) -> NotificationPolicy:
    """创建并提交一个 policy（支持防打扰配置）。"""
    policy = NotificationPolicy(
        name=name,
        enabled=enabled,
        source_types_json=json.dumps(source_types or [], ensure_ascii=False),
        min_severity=min_severity,
        scope_type=scope_type,
        scope_ids_json=json.dumps(scope_ids or [], ensure_ascii=False) if scope_ids else None,
        delivery_mode="instant",
        dedup_window_minutes=dedup_window_minutes,
        cooldown_minutes=cooldown_minutes,
        quiet_hours_json=quiet_hours_json,
    )
    db_session.add(policy)
    db_session.commit()
    db_session.refresh(policy)
    return policy


def _link(db_session, *, policy_id: int, channel_id: int) -> NotificationPolicyChannel:
    """关联策略-渠道。"""
    link = NotificationPolicyChannel(policy_id=policy_id, channel_id=channel_id)
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


def _make_outbox(
    db_session,
    *,
    channel_id: int,
    source_type: str = "opportunity",
    source_id: int | None = 1,
    event_type: str = "discovery_new",
    severity: str = "info",
    event_key: str | None = None,
    created_at: datetime | None = None,
) -> NotificationOutbox:
    """便捷构造一条 outbox 记录并写入。"""
    if event_key is None:
        event_key = f"{source_type}:{event_type}:{source_id or 'no-id'}"
    outbox = NotificationOutbox(
        event_key=event_key,
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        severity=severity,
        payload_json=json.dumps({"title": "t", "body": "b"}, ensure_ascii=False),
        channel_id=channel_id,
        status="pending",
        attempt_count=0,
        max_attempts=5,
        next_retry_at=_now_utc(),
        created_at=created_at or _now_utc(),
    )
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)
    return outbox


# ----------------------------------------------------------------------------
# 1. is_in_dedup_window 窗口内
# ----------------------------------------------------------------------------


def test_is_in_dedup_window_inside(db_session):
    """【WP-MSG.5】去重窗口内（刚创建的 outbox）→ True。"""
    channel = _make_channel(db_session, name="ia-dedup-in")
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )

    result = is_in_dedup_window(
        db_session,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        dedup_window_minutes=60,
    )
    assert result is True


# ----------------------------------------------------------------------------
# 2. is_in_dedup_window 窗口外
# ----------------------------------------------------------------------------


def test_is_in_dedup_window_outside(db_session):
    """【WP-MSG.5】去重窗口外（2 小时前的 outbox）→ False。"""
    channel = _make_channel(db_session, name="ia-dedup-out")
    old_time = _now_utc() - timedelta(hours=2)
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        created_at=old_time,
    )

    result = is_in_dedup_window(
        db_session,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        dedup_window_minutes=60,
    )
    assert result is False


# ----------------------------------------------------------------------------
# 3. is_in_dedup_window 窗口=0
# ----------------------------------------------------------------------------


def test_is_in_dedup_window_zero_window(db_session):
    """【WP-MSG.5】dedup_window_minutes=0 → 不限制，返回 False。"""
    channel = _make_channel(db_session, name="ia-dedup-zero")
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )

    result = is_in_dedup_window(
        db_session,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        dedup_window_minutes=0,
    )
    assert result is False


# ----------------------------------------------------------------------------
# 4. is_in_dedup_window 不同 event_type
# ----------------------------------------------------------------------------


def test_is_in_dedup_window_different_event_type(db_session):
    """【WP-MSG.5】不同 event_type 不算重复 → False。"""
    channel = _make_channel(db_session, name="ia-dedup-evt")
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )

    result = is_in_dedup_window(
        db_session,
        source_type="opportunity",
        source_id=1,
        event_type="signal_matched",  # 不同 event_type
        channel_id=channel.id,
        dedup_window_minutes=60,
    )
    assert result is False


# ----------------------------------------------------------------------------
# 5. is_in_cooldown 冷却期内
# ----------------------------------------------------------------------------


def test_is_in_cooldown_inside(db_session):
    """【WP-MSG.5】冷却期内（刚创建的 outbox）→ True。"""
    channel = _make_channel(db_session, name="ia-cooldown")
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )

    result = is_in_cooldown(
        db_session,
        source_type="opportunity",
        source_id=1,
        channel_id=channel.id,
        cooldown_minutes=30,
    )
    assert result is True


def test_is_in_cooldown_cross_event_type(db_session):
    """【WP-MSG.5】冷却期跨 event_type（不同 event_type 也算冷却）→ True。"""
    channel = _make_channel(db_session, name="ia-cooldown-xevt")
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )

    result = is_in_cooldown(
        db_session,
        source_type="opportunity",
        source_id=1,
        channel_id=channel.id,
        cooldown_minutes=30,
    )
    # is_in_cooldown 不检查 event_type，所以不同 event_type 也会被冷却
    assert result is True


# ----------------------------------------------------------------------------
# 6. is_in_quiet_hours 跨天
# ----------------------------------------------------------------------------


def test_is_in_quiet_hours_cross_day():
    """【WP-MSG.5】跨天免打扰（22:00-08:00）：23:00/07:00 在内，10:00 不在内。"""
    quiet_hours = json.dumps({"start": "22:00", "end": "08:00"})

    # 23:00 在免打扰时间内
    now_23 = datetime(2024, 1, 1, 23, 0)
    assert is_in_quiet_hours(quiet_hours, now=now_23) is True

    # 07:00 在免打扰时间内
    now_07 = datetime(2024, 1, 1, 7, 0)
    assert is_in_quiet_hours(quiet_hours, now=now_07) is True

    # 10:00 不在免打扰时间内
    now_10 = datetime(2024, 1, 1, 10, 0)
    assert is_in_quiet_hours(quiet_hours, now=now_10) is False


# ----------------------------------------------------------------------------
# 7. is_in_quiet_hours 同天
# ----------------------------------------------------------------------------


def test_is_in_quiet_hours_same_day():
    """【WP-MSG.5】同天免打扰（12:00-14:00）：13:00 在内，11:00/15:00 不在内。"""
    quiet_hours = json.dumps({"start": "12:00", "end": "14:00"})

    # 13:00 在免打扰时间内
    now_13 = datetime(2024, 1, 1, 13, 0)
    assert is_in_quiet_hours(quiet_hours, now=now_13) is True

    # 11:00 不在免打扰时间内
    now_11 = datetime(2024, 1, 1, 11, 0)
    assert is_in_quiet_hours(quiet_hours, now=now_11) is False

    # 15:00 不在免打扰时间内
    now_15 = datetime(2024, 1, 1, 15, 0)
    assert is_in_quiet_hours(quiet_hours, now=now_15) is False


# ----------------------------------------------------------------------------
# 8. is_in_quiet_hours 无配置
# ----------------------------------------------------------------------------


def test_is_in_quiet_hours_no_config():
    """【WP-MSG.5】无免打扰配置 → False。"""
    assert is_in_quiet_hours(None) is False
    assert is_in_quiet_hours("") is False


def test_is_in_quiet_hours_invalid_json():
    """【WP-MSG.5】无效 JSON → False。"""
    assert is_in_quiet_hours("not-json") is False


# ----------------------------------------------------------------------------
# 9. is_in_quiet_hours trading_days_only
# ----------------------------------------------------------------------------


def test_is_in_quiet_hours_trading_days_only():
    """【WP-MSG.5】trading_days_only=true：周末不算交易日 → False。"""
    quiet_hours = json.dumps({
        "start": "22:00",
        "end": "08:00",
        "trading_days_only": True,
    })

    # 2024-01-06 是周六 23:00 → 周末非交易日 → False
    sat_23 = datetime(2024, 1, 6, 23, 0)
    assert sat_23.weekday() == 5  # Saturday
    assert is_in_quiet_hours(quiet_hours, now=sat_23) is False

    # 2024-01-08 是周一 23:00 → 交易日 → True
    mon_23 = datetime(2024, 1, 8, 23, 0)
    assert mon_23.weekday() == 0  # Monday
    assert is_in_quiet_hours(quiet_hours, now=mon_23) is True


# ----------------------------------------------------------------------------
# 10. should_bypass_quiet_hours critical
# ----------------------------------------------------------------------------


def test_should_bypass_quiet_hours_critical():
    """【WP-MSG.5】bypass_for_critical=true + critical → True；error → False。"""
    quiet_hours = json.dumps({"start": "22:00", "end": "08:00", "bypass_for_critical": True})

    assert should_bypass_quiet_hours(quiet_hours, "critical") is True
    assert should_bypass_quiet_hours(quiet_hours, "error") is False
    assert should_bypass_quiet_hours(quiet_hours, "info") is False


# ----------------------------------------------------------------------------
# 11. should_bypass_quiet_hours 未设置
# ----------------------------------------------------------------------------


def test_should_bypass_quiet_hours_not_set():
    """【WP-MSG.5】bypass_for_critical=false + critical → False。"""
    quiet_hours = json.dumps({"start": "22:00", "end": "08:00", "bypass_for_critical": False})

    assert should_bypass_quiet_hours(quiet_hours, "critical") is False


def test_should_bypass_quiet_hours_no_config():
    """【WP-MSG.5】无免打扰配置 → 总是发送（True）。"""
    assert should_bypass_quiet_hours(None, "critical") is True
    assert should_bypass_quiet_hours(None, "info") is True


# ----------------------------------------------------------------------------
# 12. should_send_now error 即时发送
# ----------------------------------------------------------------------------


def test_should_send_now_error_instant(db_session):
    """【WP-MSG.5】error 默认即时发送（不受去重/冷却限制）→ True。"""
    channel = _make_channel(db_session, name="ia-err-instant")
    # 已存在 outbox（去重窗口内 + 冷却期内）
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )
    policy = _make_policy(
        db_session,
        name="p-err-instant",
        source_types=["opportunity"],
        dedup_window_minutes=60,
        cooldown_minutes=30,
    )

    should_send, reason = should_send_now(
        db_session,
        policy=policy,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        severity="error",
    )
    assert should_send is True
    assert reason == ""


# ----------------------------------------------------------------------------
# 13. should_send_now info 去重窗口内
# ----------------------------------------------------------------------------


def test_should_send_now_info_in_dedup_window(db_session):
    """【WP-MSG.5】info + 去重窗口内 → False, reason='去重窗口内'。"""
    channel = _make_channel(db_session, name="ia-info-dedup")
    _make_outbox(
        db_session,
        channel_id=channel.id,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
    )
    policy = _make_policy(
        db_session,
        name="p-info-dedup",
        source_types=["opportunity"],
        dedup_window_minutes=60,
    )

    should_send, reason = should_send_now(
        db_session,
        policy=policy,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        severity="info",
    )
    assert should_send is False
    assert reason == "去重窗口内"


# ----------------------------------------------------------------------------
# 14. should_send_now info 免打扰时间
# ----------------------------------------------------------------------------


def test_should_send_now_info_in_quiet_hours(db_session):
    """【WP-MSG.5】info + 免打扰时间 → False, reason='免打扰时间'。"""
    channel = _make_channel(db_session, name="ia-info-quiet")
    quiet_hours = json.dumps({"start": "22:00", "end": "08:00"})
    policy = _make_policy(
        db_session,
        name="p-info-quiet",
        source_types=["opportunity"],
        quiet_hours_json=quiet_hours,
    )

    # 23:00 在免打扰时间内
    now_23 = datetime(2024, 1, 1, 23, 0)
    should_send, reason = should_send_now(
        db_session,
        policy=policy,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        severity="info",
        now=now_23,
    )
    assert should_send is False
    assert reason == "免打扰时间"


# ----------------------------------------------------------------------------
# 15. should_send_now critical 绕过免打扰
# ----------------------------------------------------------------------------


def test_should_send_now_critical_bypass_quiet_hours(db_session):
    """【WP-MSG.5】critical + bypass_for_critical=true → 绕过免打扰 → True。"""
    channel = _make_channel(db_session, name="ia-crit-bypass")
    quiet_hours = json.dumps({
        "start": "22:00",
        "end": "08:00",
        "bypass_for_critical": True,
    })
    policy = _make_policy(
        db_session,
        name="p-crit-bypass",
        source_types=["opportunity"],
        quiet_hours_json=quiet_hours,
    )

    # 23:00 在免打扰时间内，但 critical 绕过
    now_23 = datetime(2024, 1, 1, 23, 0)
    should_send, reason = should_send_now(
        db_session,
        policy=policy,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        severity="critical",
        now=now_23,
    )
    assert should_send is True
    assert reason == ""


def test_should_send_now_error_in_quiet_hours_no_bypass(db_session):
    """【WP-MSG.5】error + 免打扰时间 + bypass_for_critical=false → False。"""
    channel = _make_channel(db_session, name="ia-err-quiet")
    quiet_hours = json.dumps({
        "start": "22:00",
        "end": "08:00",
        "bypass_for_critical": False,
    })
    policy = _make_policy(
        db_session,
        name="p-err-quiet",
        source_types=["opportunity"],
        quiet_hours_json=quiet_hours,
    )

    now_23 = datetime(2024, 1, 1, 23, 0)
    should_send, reason = should_send_now(
        db_session,
        policy=policy,
        source_type="opportunity",
        source_id=1,
        event_type="discovery_new",
        channel_id=channel.id,
        severity="error",
        now=now_23,
    )
    assert should_send is False
    assert "免打扰" in reason


# ----------------------------------------------------------------------------
# 16. sanitize_payload 移除敏感字段
# ----------------------------------------------------------------------------


def test_sanitize_payload_removes_sensitive_fields():
    """【WP-MSG.5】sanitize_payload 移除 password/token 等敏感字段。"""
    payload = {
        "title": "测试",
        "password": "abc123",
        "token": "xyz",
        "body": "内容",
    }
    sanitized = sanitize_payload(payload)
    assert "password" not in sanitized
    assert "token" not in sanitized
    assert sanitized["title"] == "测试"
    assert sanitized["body"] == "内容"


# ----------------------------------------------------------------------------
# 17. sanitize_payload 白名单
# ----------------------------------------------------------------------------


def test_sanitize_payload_whitelist():
    """【WP-MSG.5】sanitize_payload 只保留白名单字段。"""
    payload = {
        "title": "t",
        "symbol": "000001",
        "database_url": "mysql://root:pass@host/db",
        "api_key": "sk-xxx",
    }
    sanitized = sanitize_payload(payload)
    assert "title" in sanitized
    assert "symbol" in sanitized
    assert "database_url" not in sanitized
    assert "api_key" not in sanitized


def test_sanitize_payload_non_dict():
    """【WP-MSG.5】sanitize_payload 非 dict 输入 → 空 dict。"""
    assert sanitize_payload(None) == {}
    assert sanitize_payload("string") == {}
    assert sanitize_payload(123) == {}


# ----------------------------------------------------------------------------
# 18. truncate_content 截断
# ----------------------------------------------------------------------------


def test_truncate_content_short():
    """【WP-MSG.5】短内容不截断。"""
    content = "短内容"
    assert truncate_content(content, max_length=500) == content


def test_truncate_content_long():
    """【WP-MSG.5】长内容截断到 max_length，末尾追加 '...'。"""
    content = "a" * 600
    result = truncate_content(content, max_length=500)
    assert len(result) == 500
    assert result.endswith("...")
    assert result[:497] == "a" * 497


# ----------------------------------------------------------------------------
# 19. build_summary
# ----------------------------------------------------------------------------


def test_build_summary_format():
    """【WP-MSG.5】build_summary 格式：'{title}\\n\\n{body}'。"""
    result = build_summary("标题", "内容", max_length=100)
    assert result == "标题\n\n内容"


def test_build_summary_truncated():
    """【WP-MSG.5】build_summary 超长时截断。"""
    long_body = "b" * 200
    result = build_summary("标题", long_body, max_length=50)
    assert len(result) == 50
    assert result.endswith("...")


# ----------------------------------------------------------------------------
# 20. get_in_app_payload 保留完整
# ----------------------------------------------------------------------------


def test_get_in_app_payload_preserves_full():
    """【WP-MSG.5】get_in_app_payload 保留完整 payload（不脱敏不截断）。"""
    payload = {"title": "t", "body": "b", "password": "secret"}
    result = get_in_app_payload(payload)
    assert result is payload  # 站内消息保留完整内容


# ----------------------------------------------------------------------------
# 21. emit_event 集成防打扰 - 去重跳过
# ----------------------------------------------------------------------------


def test_emit_event_dedup_window_skip(db_session):
    """【WP-MSG.5】emit_event 集成去重窗口：第二次相同事件被跳过。"""
    in_app = _make_channel(db_session, name="ia-emit-dedup")
    policy = _make_policy(
        db_session,
        name="p-emit-dedup",
        source_types=["opportunity"],
        dedup_window_minutes=60,
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    # 第一次 emit_event → 创建 1 条
    first = emit_event(
        db_session,
        source_type="opportunity",
        source_id=1001,
        event_type="discovery_new",
        severity="info",
        title="第一次",
        body="b",
    )
    assert len(first) == 1

    # 第二次相同事件 → 去重窗口内被跳过
    second = emit_event(
        db_session,
        source_type="opportunity",
        source_id=1001,
        event_type="discovery_new",
        severity="info",
        title="第二次",
        body="b2",
    )
    assert second == []


# ----------------------------------------------------------------------------
# 22. emit_event error 不受去重限制
# ----------------------------------------------------------------------------


def test_emit_event_error_not_deduped(db_session):
    """【WP-MSG.5】emit_event error 不受去重限制：两次都创建记录。"""
    in_app = _make_channel(db_session, name="ia-emit-err")
    policy = _make_policy(
        db_session,
        name="p-emit-err",
        source_types=["opportunity"],
        dedup_window_minutes=60,
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    # 第一次 error → 创建 1 条
    first = emit_event(
        db_session,
        source_type="opportunity",
        source_id=2002,
        event_type="discovery_new",
        severity="error",
        title="e1",
        body="b1",
    )
    assert len(first) == 1

    # 第二次 error → 不去重，创建 1 条
    second = emit_event(
        db_session,
        source_type="opportunity",
        source_id=2002,
        event_type="discovery_new",
        severity="error",
        title="e2",
        body="b2",
    )
    assert len(second) == 1
    assert first[0].id != second[0].id


# ----------------------------------------------------------------------------
# 23. emit_event 第三方渠道摘要
# ----------------------------------------------------------------------------


def test_emit_event_third_party_summary(db_session):
    """【WP-MSG.5】emit_event 第三方渠道内容超长时截断摘要。"""
    wx = _make_channel(
        db_session,
        name="wx-emit-summary",
        channel_type="wxpusher",
        enabled=True,
        status="enabled",
    )
    policy = _make_policy(
        db_session,
        name="p-emit-summary",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=wx.id)

    long_body = "很长的内容" * 100  # 500 字符
    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=3003,
        event_type="discovery_new",
        severity="info",
        title="摘要测试",
        body=long_body,
    )
    assert len(created) == 1
    outbox = created[0]
    payload = json.loads(outbox.payload_json)
    # 第三方渠道 body 被截断到 500 字符以内
    assert len(payload["body"]) <= 500
    # 原始 body 长度 500，加上标题和换行后超过 500，应被截断
    assert len(payload["body"]) < len(long_body) + len("摘要测试") + 2


def test_emit_event_in_app_keeps_full_content(db_session):
    """【WP-MSG.5】emit_event 站内渠道保留完整内容（不截断）。"""
    in_app = _make_channel(db_session, name="ia-emit-full")
    policy = _make_policy(
        db_session,
        name="p-emit-full",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    body = "正常长度内容"
    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=4004,
        event_type="discovery_new",
        severity="info",
        title="站内完整",
        body=body,
    )
    assert len(created) == 1
    payload = json.loads(created[0].payload_json)
    # 站内消息 body 原样保留
    assert payload["body"] == body
    assert payload["title"] == "站内完整"


# ----------------------------------------------------------------------------
# 24. sanitize_value 递归
# ----------------------------------------------------------------------------


def test_sanitize_value_recursive_dict():
    """【WP-MSG.5】_sanitize_value 递归处理 dict，移除嵌套敏感字段。"""
    value = {"title": {"password": "x"}, "body": "safe"}
    result = _sanitize_value(value)
    # title 的值是 dict，递归处理后 password 被移除
    assert "password" not in str(result)
    assert result["body"] == "safe"


def test_sanitize_value_recursive_list():
    """【WP-MSG.5】_sanitize_value 递归处理 list。"""
    value = ["safe", "mysql://root:pass@host/db", {"password": "x"}]
    result = _sanitize_value(value)
    assert isinstance(result, list)
    assert len(result) == 3
    # 字符串中的敏感信息被 sanitize_message 脱敏
    assert "pass" not in result[1]
    assert "mysql://root:***@host/db" == result[1]


def test_sanitize_value_string():
    """【WP-MSG.5】_sanitize_value 字符串走 sanitize_message。"""
    result = _sanitize_value("mysql://root:pass@host/db")
    assert "pass" not in result
    assert "***" in result


def test_sanitize_value_non_string():
    """【WP-MSG.5】_sanitize_value 非字符串/列表/dict 原样返回。"""
    assert _sanitize_value(123) == 123
    assert _sanitize_value(None) is None
    assert _sanitize_value(True) is True
