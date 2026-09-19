"""白盒测试 - WP-MSG.3 异步 dispatcher。

覆盖：
1. run_once 基本流程（成功路径）
2. 一渠道失败不影响其他渠道
3. 鉴权失败暂停渠道
4. 指数退避（临时错误）
5. 达上限进 dead-letter
6. dispatcher 重启恢复
7. 渠道未启用跳过
8. 适配器不存在
9. payload 解析（适配器收到正确的 title/body）
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models.notification import (
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
)
from app.services.notifications.base import SendResult
from app.services.notifications.dispatcher import Dispatcher
from app.services.notifications.outbox import _now_utc


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


class _MockAdapter:
    """测试用 mock 适配器。

    记录 send_message 调用参数，根据配置返回成功/失败。
    """

    def __init__(
        self,
        *,
        success: bool = True,
        status_code: int = 200,
        error_code: str | None = None,
        error_message: str | None = None,
        raise_exc: Exception | None = None,
    ):
        self.success = success
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        call_info = {
            "config": config,
            "title": title,
            "body": body,
            "body_text": body_text,
            "payload": payload,
        }
        self.calls.append(call_info)

        if self.raise_exc is not None:
            raise self.raise_exc

        return SendResult(
            success=self.success,
            status_code=self.status_code,
            error_code=self.error_code,
            error_message=self.error_message,
            response_summary=f"[mock] {title}" if self.success else None,
            duration_ms=10,
        )

    def normalize_error(self, exc: Exception) -> tuple[str, str]:
        return type(exc).__name__, str(exc)


def _make_channel_in_db(
    db_session,
    *,
    name: str = "test-channel",
    channel_type: str = "in_app",
    enabled: bool = True,
    status: str = "enabled",
    config: dict | None = None,
) -> NotificationChannel:
    """创建并提交一个 channel。"""
    from app.utils.secret_mask import encrypt_config

    channel = NotificationChannel(
        name=name,
        channel_type=channel_type,
        enabled=enabled,
        status=status,
    )
    if config is not None:
        channel.config_encrypted_json = encrypt_config(json.dumps(config))
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
    """便捷构造一条 outbox 记录。"""
    if payload is None:
        payload = {"title": "测试", "body": "内容"}
    outbox = NotificationOutbox(
        event_key=event_key,
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload_json=json.dumps(payload, ensure_ascii=False),
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


def _patch_adapter(monkeypatch, adapter: _MockAdapter | None = None):
    """patch dispatcher.get_adapter，返回固定 mock。"""
    if adapter is None:
        adapter = _MockAdapter()

    def _get_adapter(channel_type: str):
        # 始终返回同一个 mock（按 channel_type 区分由测试自行实现）
        return adapter

    monkeypatch.setattr(
        "app.services.notifications.dispatcher.get_adapter", _get_adapter
    )
    return adapter


def _patch_adapter_by_type(monkeypatch, adapter_map: dict[str, _MockAdapter]):
    """patch dispatcher.get_adapter，按 channel_type 返回不同 mock。"""

    def _get_adapter(channel_type: str):
        return adapter_map.get(channel_type)

    monkeypatch.setattr(
        "app.services.notifications.dispatcher.get_adapter", _get_adapter
    )
    return adapter_map


# ----------------------------------------------------------------------------
# 1. run_once 基本流程
# ----------------------------------------------------------------------------


def test_run_once_sends_pending_outbox(db_session, monkeypatch):
    """【WP-MSG.3】run_once 处理 pending 记录，发送成功。"""
    channel = _make_channel_in_db(db_session, name="ch-disp-basic")
    outbox = _enqueue_one(
        db_session,
        channel_id=channel.id,
        event_key="evt-disp-basic",
        payload={"title": "买入告警", "body": "600000 触发买入"},
    )

    mock_adapter = _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    count = dispatcher.run_once()

    assert count == 1
    assert len(mock_adapter.calls) == 1
    assert mock_adapter.calls[0]["title"] == "买入告警"
    assert mock_adapter.calls[0]["body"] == "600000 触发买入"

    # 刷新 test session 缓存，验证 DB 中状态
    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.status == "sent"
    assert db_outbox.sent_at is not None

    deliveries = db_session.execute(
        select(NotificationDelivery).where(NotificationDelivery.outbox_id == outbox.id)
    ).scalars().all()
    assert len(deliveries) == 1
    assert deliveries[0].status == "success"
    assert deliveries[0].status_code == 200


# ----------------------------------------------------------------------------
# 2. 一渠道失败不影响其他渠道
# ----------------------------------------------------------------------------


def test_run_once_channel_failure_isolation(db_session, monkeypatch):
    """【WP-MSG.3】一渠道失败不影响其他渠道（单条 try/except）。"""
    # 两个渠道：in_app（成功） + wxpusher（失败）
    in_app_channel = _make_channel_in_db(
        db_session, name="ch-iso-in-app", channel_type="in_app"
    )
    wxpusher_channel = _make_channel_in_db(
        db_session, name="ch-iso-wxpusher", channel_type="wxpusher"
    )

    o_in_app = _enqueue_one(
        db_session, channel_id=in_app_channel.id, event_key="evt-iso-in-app",
        payload={"title": "in_app 消息", "body": "b1"},
    )
    o_wxpusher = _enqueue_one(
        db_session, channel_id=wxpusher_channel.id, event_key="evt-iso-wxpusher",
        payload={"title": "wxpusher 消息", "body": "b2"},
    )

    # 按 channel_type 返回不同 mock
    _patch_adapter_by_type(
        monkeypatch,
        {
            "in_app": _MockAdapter(success=True, status_code=200),
            "wxpusher": _MockAdapter(
                success=False,
                status_code=500,
                error_code="INTERNAL",
                error_message="服务端错误",
            ),
        },
    )

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    count = dispatcher.run_once()

    # 两条都被尝试处理（无论成功失败）
    assert count == 2

    db_session.expire_all()
    o1 = db_session.get(NotificationOutbox, o_in_app.id)
    o2 = db_session.get(NotificationOutbox, o_wxpusher.id)

    # in_app 成功，wxpusher 失败但状态变 pending（待重试）
    assert o1.status == "sent"
    assert o2.status == "pending"  # 临时错误指数退避
    assert o2.attempt_count == 1
    assert o2.next_retry_at is not None


# ----------------------------------------------------------------------------
# 3. 鉴权失败暂停渠道
# ----------------------------------------------------------------------------


def test_run_once_auth_failure_suspends_channel(db_session, monkeypatch):
    """【WP-MSG.3】鉴权失败（401/403）→ 暂停渠道 + 站内系统告警。"""
    # 主渠道（待会被暂停）：wxpusher
    wx_channel = _make_channel_in_db(
        db_session, name="ch-auth-wx", channel_type="wxpusher"
    )
    # in_app 渠道（用于接收告警）
    in_app_channel = _make_channel_in_db(
        db_session, name="ch-auth-in-app", channel_type="in_app"
    )

    _enqueue_one(
        db_session, channel_id=wx_channel.id, event_key="evt-auth-wx",
        payload={"title": "auth 测试", "body": "b"},
    )

    _patch_adapter_by_type(
        monkeypatch,
        {
            "wxpusher": _MockAdapter(
                success=False,
                status_code=401,
                error_code="UNAUTHORIZED",
                error_message="Token 无效",
            ),
            "in_app": _MockAdapter(success=True, status_code=200),
        },
    )

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    # wxpusher 渠道应被暂停
    wx_ch = db_session.get(NotificationChannel, wx_channel.id)
    assert wx_ch.status == "disabled"
    assert wx_ch.enabled is False
    assert wx_ch.last_error_code == "auth_failed"

    # 应产生一条 in_app 系统告警
    alerts = db_session.execute(
        select(NotificationOutbox).where(NotificationOutbox.source_type == "system")
    ).scalars().all()
    assert len(alerts) >= 1
    assert alerts[0].channel_id == in_app_channel.id


# ----------------------------------------------------------------------------
# 4. 指数退避（临时错误）
# ----------------------------------------------------------------------------


def test_run_once_temporary_error_backoff(db_session, monkeypatch):
    """【WP-MSG.3】临时错误（500）→ status='pending', next_retry_at > now。"""
    channel = _make_channel_in_db(
        db_session, name="ch-backoff", channel_type="wxpusher"
    )
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-backoff"
    )

    _patch_adapter(
        monkeypatch,
        _MockAdapter(
            success=False,
            status_code=500,
            error_code="INTERNAL",
            error_message="服务内部错误",
        ),
    )

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.status == "pending"
    assert db_outbox.attempt_count == 1
    assert db_outbox.next_retry_at is not None
    # next_retry_at 应晚于当前时间（指数退避）
    assert db_outbox.next_retry_at > _now_utc()


# ----------------------------------------------------------------------------
# 5. 达上限进 dead-letter
# ----------------------------------------------------------------------------


def test_run_once_reaches_dead_letter(db_session, monkeypatch):
    """【WP-MSG.3】max_attempts=2 时，连续两次失败后进 dead_letter。"""
    channel = _make_channel_in_db(
        db_session, name="ch-dead", channel_type="wxpusher"
    )
    outbox = _enqueue_one(
        db_session,
        channel_id=channel.id,
        event_key="evt-dead",
        max_attempts=2,
    )

    _patch_adapter(
        monkeypatch,
        _MockAdapter(
            success=False,
            status_code=500,
            error_code="INTERNAL",
            error_message="内部错误",
        ),
    )

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)

    # 第一次 run_once：失败，attempt=1, status='pending'
    dispatcher.run_once()
    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.attempt_count == 1
    assert db_outbox.status == "pending"

    # next_retry_at 设为过去，触发再次处理
    db_outbox.next_retry_at = _now_utc() - timedelta(minutes=1)
    db_session.commit()

    # 第二次 run_once：失败，attempt=2 >= max_attempts → dead_letter
    dispatcher.run_once()
    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.attempt_count == 2
    assert db_outbox.status == "dead_letter"
    assert db_outbox.next_retry_at is None


# ----------------------------------------------------------------------------
# 6. dispatcher 重启恢复
# ----------------------------------------------------------------------------


def test_dispatcher_restart_resumes_processing(db_session, monkeypatch):
    """【WP-MSG.3】dispatcher 重启后继续处理未完成 Outbox（状态在 DB 持久化）。"""
    channel = _make_channel_in_db(
        db_session, name="ch-restart", channel_type="in_app"
    )
    o1 = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-restart-1",
        payload={"title": "重启 1", "body": "b1"},
    )

    # 第一次 dispatcher 处理第 1 条
    _patch_adapter(monkeypatch, _MockAdapter(success=True))
    d1 = Dispatcher(poll_interval=0.01, batch_size=10)
    d1.run_once()

    db_session.expire_all()
    db_o1 = db_session.get(NotificationOutbox, o1.id)
    assert db_o1.status == "sent"

    # 新建第 2 条
    o2 = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-restart-2",
        payload={"title": "重启 2", "body": "b2"},
    )

    # 创建新的 dispatcher 实例（模拟重启）
    d2 = Dispatcher(poll_interval=0.01, batch_size=10)
    count = d2.run_once()

    assert count == 1
    db_session.expire_all()
    db_o2 = db_session.get(NotificationOutbox, o2.id)
    assert db_o2.status == "sent"


def test_dispatcher_restart_picks_up_pending_records(db_session, monkeypatch):
    """【WP-MSG.3】dispatcher 重启后 pending 记录仍能被处理。"""
    channel = _make_channel_in_db(
        db_session, name="ch-restart-pending", channel_type="in_app"
    )
    o1 = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-restart-pending-1"
    )
    o2 = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-restart-pending-2"
    )

    # 不调用 run_once，模拟 dispatcher 进程尚未启动
    # 然后启动一个全新 dispatcher 实例
    _patch_adapter(monkeypatch, _MockAdapter(success=True))
    d = Dispatcher(poll_interval=0.01, batch_size=10)
    count = d.run_once()

    assert count == 2
    db_session.expire_all()
    db_o1 = db_session.get(NotificationOutbox, o1.id)
    db_o2 = db_session.get(NotificationOutbox, o2.id)
    assert db_o1.status == "sent"
    assert db_o2.status == "sent"


# ----------------------------------------------------------------------------
# 7. 渠道未启用跳过
# ----------------------------------------------------------------------------


def test_run_once_skips_disabled_channel(db_session, monkeypatch):
    """【WP-MSG.3】disabled 渠道 → outbox 失败，error_code='channel_disabled'。"""
    channel = _make_channel_in_db(
        db_session,
        name="ch-disabled",
        channel_type="wxpusher",
        enabled=False,
        status="disabled",
    )
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-disabled"
    )

    _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    # disabled 渠道被 mark_failed 处理：临时错误，status='pending'，attempt=1
    assert db_outbox.attempt_count == 1
    assert db_outbox.last_error_code == "channel_disabled"


def test_run_once_skips_unconfigured_channel(db_session, monkeypatch):
    """【WP-MSG.3】unconfigured 渠道 → outbox 失败。"""
    channel = _make_channel_in_db(
        db_session,
        name="ch-unconfigured",
        channel_type="wxpusher",
        enabled=True,
        status="unconfigured",
    )
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-unconfigured"
    )

    _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.last_error_code == "channel_disabled"


# ----------------------------------------------------------------------------
# 8. 适配器不存在
# ----------------------------------------------------------------------------


def test_run_once_handles_missing_adapter(db_session, monkeypatch):
    """【WP-MSG.3】channel_type='unknown' → error_code='adapter_not_found'。"""
    channel = _make_channel_in_db(
        db_session,
        name="ch-unknown-type",
        channel_type="unknown_type",
        enabled=True,
        status="enabled",
    )
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-unknown"
    )

    # get_adapter 对 unknown_type 返回 None
    def _get_adapter(channel_type: str):
        return None

    monkeypatch.setattr(
        "app.services.notifications.dispatcher.get_adapter", _get_adapter
    )

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.last_error_code == "adapter_not_found"
    assert db_outbox.attempt_count == 1


# ----------------------------------------------------------------------------
# 9. payload 解析
# ----------------------------------------------------------------------------


def test_run_once_passes_payload_to_adapter(db_session, monkeypatch):
    """【WP-MSG.3】dispatcher 正确解析 payload_json 并传给适配器。"""
    channel = _make_channel_in_db(
        db_session, name="ch-payload", channel_type="in_app"
    )
    payload = {
        "title": "测试标题",
        "body": "测试正文",
        "body_text": "纯文本正文",
        "symbol_id": 600000,
    }
    _enqueue_one(
        db_session,
        channel_id=channel.id,
        event_key="evt-payload",
        payload=payload,
    )

    mock_adapter = _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    assert len(mock_adapter.calls) == 1
    call = mock_adapter.calls[0]
    assert call["title"] == "测试标题"
    assert call["body"] == "测试正文"
    assert call["body_text"] == "纯文本正文"
    # payload 整体也传给适配器
    assert call["payload"]["symbol_id"] == 600000


def test_run_once_handles_missing_payload_fields(db_session, monkeypatch):
    """【WP-MSG.3】payload 缺少 title/body 时适配器收到空字符串。"""
    channel = _make_channel_in_db(
        db_session, name="ch-empty-payload", channel_type="in_app"
    )
    outbox = _enqueue_one(
        db_session,
        channel_id=channel.id,
        event_key="evt-empty-payload",
        payload={"symbol_id": 1},  # 没有 title/body
    )

    mock_adapter = _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    assert len(mock_adapter.calls) == 1
    call = mock_adapter.calls[0]
    assert call["title"] == ""
    assert call["body"] == ""

    # outbox 应成功发送
    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.status == "sent"


def test_run_once_handles_invalid_payload_json(db_session, monkeypatch):
    """【WP-MSG.3】payload_json 解析失败时容错（用空字典）。"""
    channel = _make_channel_in_db(
        db_session, name="ch-invalid-json", channel_type="in_app"
    )
    outbox = NotificationOutbox(
        event_key="evt-invalid-json",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload_json="not-a-valid-json{",
        channel_id=channel.id,
        status="pending",
        attempt_count=0,
        max_attempts=5,
        next_retry_at=_now_utc(),
        created_at=_now_utc(),
    )
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)

    mock_adapter = _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    # 应当容错处理，仍然调用适配器
    assert len(mock_adapter.calls) == 1
    call = mock_adapter.calls[0]
    assert call["title"] == ""
    assert call["body"] == ""


def test_run_once_adapter_exception(db_session, monkeypatch):
    """【WP-MSG.3】适配器抛异常 → normalize_error + mark_failed。"""
    channel = _make_channel_in_db(
        db_session, name="ch-exc", channel_type="wxpusher"
    )
    outbox = _enqueue_one(
        db_session, channel_id=channel.id, event_key="evt-exc"
    )

    _patch_adapter(
        monkeypatch,
        _MockAdapter(raise_exc=RuntimeError("连接超时")),
    )

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.attempt_count == 1
    assert db_outbox.last_error_code == "RuntimeError"


def test_run_once_handles_missing_channel(db_session, monkeypatch):
    """【WP-MSG.3】outbox.channel_id 指向不存在的 channel → error_code='channel_not_found'。"""
    # 直接插入一条引用不存在 channel 的 outbox
    outbox = NotificationOutbox(
        event_key="evt-missing-channel",
        source_type="alert",
        source_id=1,
        event_type="price_alert_triggered",
        severity="warn",
        payload_json='{"title":"t","body":"b"}',
        channel_id=999999,  # 不存在的 channel_id
        status="pending",
        attempt_count=0,
        max_attempts=5,
        next_retry_at=_now_utc(),
        created_at=_now_utc(),
    )
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)

    _patch_adapter(monkeypatch, _MockAdapter(success=True))

    dispatcher = Dispatcher(poll_interval=0.01, batch_size=10)
    dispatcher.run_once()

    db_session.expire_all()
    db_outbox = db_session.get(NotificationOutbox, outbox.id)
    assert db_outbox.last_error_code == "channel_not_found"
    assert db_outbox.attempt_count == 1
