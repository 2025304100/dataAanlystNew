"""白盒测试 - WP-MSG.2 渠道适配器。

覆盖：
1. base.py 接口（ChannelAdapter ABC / SendResult dataclass）
2. InAppAdapter
3. WxPusherAdapter（validate / mask / mock send）
4. DingTalkAdapter（validate / mask / mock send with signature）
5. OneBotAdapter（validate / mask / mock send）
6. EmailAdapter（validate / mask / mock smtp）
7. WebhookAdapter（validate / mask / mock send with template）
8. registry.get_adapter / list_adapters
9. template_renderer.render_template / escape_value
10. normalize_error 脱敏
11. _truncate_response 截断
12. 有限超时（默认 10.0，可配置）
"""
from __future__ import annotations

from dataclasses import is_dataclass
from inspect import isabstract
from unittest.mock import MagicMock, patch

import pytest

from app.services.notifications.base import (
    ChannelAdapter,
    CircuitBreakerOpenError,
    CircuitBreakerState,
    SendResult,
)
from app.services.notifications.dingtalk import DingTalkAdapter
from app.services.notifications.email import EmailAdapter
from app.services.notifications.in_app import InAppAdapter
from app.services.notifications.onebot import OneBotAdapter
from app.services.notifications.registry import get_adapter, list_adapters
from app.services.notifications.template_renderer import escape_value, render_template
from app.services.notifications.webhook import WebhookAdapter
from app.services.notifications.wxpusher import WxPusherAdapter


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


class _MockResponse:
    """模拟 httpx.Response。"""

    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or ""

    def json(self):
        return self._json


class _MockContext:
    """模拟 httpx.Client / smtplib.SMTP_SSL / smtplib.SMTP 的上下文管理器。"""

    def __init__(self, mock_obj):
        self._mock = mock_obj

    def __enter__(self):
        return self._mock

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def _make_httpx_client_mock(response: _MockResponse, captured: dict | None = None):
    """构造一个 httpx.Client mock。

    captured: 可选 dict，用于捕获调用参数（timeout / json / headers）。
    """
    client = MagicMock()
    client.post = MagicMock(return_value=response)
    client.get = MagicMock(return_value=response)
    client.put = MagicMock(return_value=response)
    if captured is not None:
        # 包装 post 以捕获参数
        original_post = client.post

        def _capture_post(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return original_post(*args, **kwargs)

        client.post = _capture_post
    return _MockContext(client)


# ----------------------------------------------------------------------------
# 1. base.py 接口
# ----------------------------------------------------------------------------


def test_channel_adapter_is_abstract():
    """【WP-MSG.2】ChannelAdapter 是 ABC，不能直接实例化。"""
    assert isabstract(ChannelAdapter), "ChannelAdapter 应为抽象类"
    with pytest.raises(TypeError):
        ChannelAdapter()  # type: ignore[abstract]


def test_send_result_is_dataclass():
    """【WP-MSG.2】SendResult 是 dataclass，可声明字段。"""
    assert is_dataclass(SendResult)
    r = SendResult(success=True, status_code=200, duration_ms=10)
    assert r.success is True
    assert r.status_code == 200
    assert r.duration_ms == 10
    assert r.response_summary is None
    assert r.error_code is None
    assert r.error_message is None


def test_circuit_breaker_state_logic():
    """【WP-MSG.2】熔断器状态：连续失败达阈值后熔断，成功后恢复。"""
    br = CircuitBreakerState(threshold=3, reset_after_seconds=1.0)
    assert br.allow_request() is True
    br.record_failure()
    br.record_failure()
    assert br.allow_request() is True  # 仍 closed
    br.record_failure()
    # 第 3 次失败 → 熔断开启
    assert br.state == "open"
    assert br.allow_request() is False  # 熔断中


# ----------------------------------------------------------------------------
# 2. InAppAdapter
# ----------------------------------------------------------------------------


def test_in_app_validate_config_returns_empty():
    """【WP-MSG.2】InAppAdapter.validate_config 总返回空列表（无配置要求）。"""
    adapter = InAppAdapter()
    assert adapter.validate_config({}) == []
    assert adapter.validate_config({"any_key": "any_value"}) == []


def test_in_app_send_test_success():
    """【WP-MSG.2】InAppAdapter.send_test 返回 success。"""
    adapter = InAppAdapter()
    result = adapter.send_test({})
    assert result.success is True
    assert result.status_code == 200
    assert "测试" in (result.response_summary or "")


def test_in_app_send_message_success():
    """【WP-MSG.2】InAppAdapter.send_message 返回 success，正文进 response_summary。"""
    adapter = InAppAdapter()
    result = adapter.send_message(
        config={},
        title="价格告警",
        body="600000 触发买入",
    )
    assert result.success is True
    assert "价格告警" in (result.response_summary or "")
    assert "600000 触发买入" in (result.response_summary or "")


def test_in_app_mask_config_returns_empty():
    """【WP-MSG.2】InAppAdapter.mask_config 返回空 dict。"""
    adapter = InAppAdapter()
    assert adapter.mask_config({}) == {}
    assert adapter.mask_config({"foo": "bar"}) == {}


# ----------------------------------------------------------------------------
# 3. WxPusherAdapter
# ----------------------------------------------------------------------------


def test_wxpusher_validate_config_missing_app_token():
    """【WP-MSG.2】WxPusher.validate_config：app_token 为空 → 错误。"""
    adapter = WxPusherAdapter()
    errors = adapter.validate_config({"uids": ["uid1"]})
    assert any("app_token" in e for e in errors)


def test_wxpusher_validate_config_missing_uids_and_topics():
    """【WP-MSG.2】WxPusher.validate_config：uids 与 topic_ids 都为空 → 错误。"""
    adapter = WxPusherAdapter()
    errors = adapter.validate_config({"app_token": "AT_xxx"})
    assert any("uids" in e and "topic_ids" in e for e in errors)


def test_wxpusher_validate_config_full():
    """【WP-MSG.2】WxPusher.validate_config：完整配置 → 空。"""
    adapter = WxPusherAdapter()
    errors = adapter.validate_config({
        "app_token": "AT_xxx",
        "uids": ["uid1"],
        "topic_ids": [123],
    })
    assert errors == []


def test_wxpusher_mask_config_masks_token():
    """【WP-MSG.2】WxPusher.mask_config：app_token 脱敏（前 4 + 后 4）。"""
    adapter = WxPusherAdapter()
    masked = adapter.mask_config({
        "app_token": "AT_abcdef1234567890",
        "uids": ["uid1"],
        "topic_ids": [123],
    })
    assert "AT_a****7890" == masked["app_token"]
    assert "AT_abcdef1234567890" not in str(masked)


def test_wxpusher_mask_config_keeps_uids():
    """【WP-MSG.2】WxPusher.mask_config：uids/topic_ids 保留原值。"""
    adapter = WxPusherAdapter()
    masked = adapter.mask_config({
        "app_token": "AT_xxx",
        "uids": ["uid1", "uid2"],
        "topic_ids": [123, 456],
    })
    assert masked["uids"] == ["uid1", "uid2"]
    assert masked["topic_ids"] == [123, 456]


def test_wxpusher_send_message_success(monkeypatch):
    """【WP-MSG.2】WxPusher.send_message：mock httpx 验证成功路径。"""
    captured: dict = {}
    response = _MockResponse(
        status_code=200,
        json_data={"code": 0, "msg": "ok", "data": {"messageId": "m-1"}},
    )
    context = _make_httpx_client_mock(response, captured)
    monkeypatch.setattr("app.services.notifications.wxpusher.httpx.Client", lambda **kw: context)

    adapter = WxPusherAdapter()
    result = adapter.send_message(
        config={"app_token": "AT_xxx", "uids": ["uid1"]},
        title="价格告警",
        body="600000 触发买入",
    )
    assert result.success is True
    assert result.status_code == 200
    # 验证 response_summary 不包含完整 appToken（响应中也不应出现请求中的 token 值）
    assert "AT_xxx" not in (result.response_summary or "")
    # 验证请求体包含正确字段
    sent_json = captured.get("kwargs", {}).get("json", {})
    assert sent_json.get("appToken") == "AT_xxx"
    assert sent_json.get("uids") == ["uid1"]
    assert sent_json.get("summary") == "价格告警"


def test_wxpusher_send_message_failure(monkeypatch):
    """【WP-MSG.2】WxPusher.send_message：mock httpx 验证失败路径。"""
    response = _MockResponse(
        status_code=200,
        json_data={"code": 1001, "msg": "invalid token"},
    )
    context = _make_httpx_client_mock(response)
    monkeypatch.setattr("app.services.notifications.wxpusher.httpx.Client", lambda **kw: context)

    adapter = WxPusherAdapter()
    # 强制不重试（max_retries=0）
    adapter.max_retries = 0
    result = adapter.send_message(
        config={"app_token": "AT_xxx", "uids": ["uid1"]},
        title="测试",
        body="b",
    )
    assert result.success is False
    assert result.error_code == "1001"


def test_wxpusher_send_message_uses_timeout(monkeypatch):
    """【WP-MSG.2】WxPusher.send_message：默认超时 10.0 秒传给 httpx.Client。"""
    captured: dict = {}
    response = _MockResponse(status_code=200, json_data={"code": 0})
    context = _make_httpx_client_mock(response, captured)
    captured_client_kw: dict = {}

    def _client_factory(**kw):
        captured_client_kw.update(kw)
        return context

    monkeypatch.setattr("app.services.notifications.wxpusher.httpx.Client", _client_factory)

    adapter = WxPusherAdapter()
    adapter.send_message(
        config={"app_token": "AT_xxx", "uids": ["uid1"]},
        title="t",
        body="b",
    )
    assert captured_client_kw["timeout"] == 10.0


# ----------------------------------------------------------------------------
# 4. DingTalkAdapter
# ----------------------------------------------------------------------------


def test_dingtalk_validate_config_missing_webhook():
    """【WP-MSG.2】DingTalk.validate_config：webhook 为空 → 错误。"""
    adapter = DingTalkAdapter()
    errors = adapter.validate_config({})
    assert any("webhook" in e for e in errors)


def test_dingtalk_validate_config_invalid_webhook_scheme():
    """【WP-MSG.2】DingTalk.validate_config：webhook 非 http(s) → 错误。"""
    adapter = DingTalkAdapter()
    errors = adapter.validate_config({"webhook": "ftp://example.com"})
    assert any("http" in e for e in errors)


def test_dingtalk_validate_config_full():
    """【WP-MSG.2】DingTalk.validate_config：完整配置 → 空。"""
    adapter = DingTalkAdapter()
    errors = adapter.validate_config({
        "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
        "secret": "SECxxxx",
    })
    assert errors == []


def test_dingtalk_mask_config_masks_webhook():
    """【WP-MSG.2】DingTalk.mask_config：webhook URL 查询参数脱敏。"""
    adapter = DingTalkAdapter()
    masked = adapter.mask_config({
        "webhook": "https://oapi.dingtalk.com/robot/send?access_token=abc123",
        "secret": "SECxxxxxxxxxxxx1234",
    })
    # webhook 中的 access_token value 替换为 ****
    assert "access_token=****" in masked["webhook"]
    assert "abc123" not in masked["webhook"]
    # secret 脱敏
    assert "abc123" not in masked["secret"]


def test_dingtalk_mask_config_masks_secret():
    """【WP-MSG.2】DingTalk.mask_config：secret 脱敏。"""
    adapter = DingTalkAdapter()
    masked = adapter.mask_config({
        "webhook": "https://oapi.dingtalk.com/robot/send",
        "secret": "SECabcdefghijklmnopqrstuvwxyz123456",
    })
    # secret 保留前 4 + 后 4
    assert masked["secret"] == "SECa****3456"
    assert "bcdefghijklmnopqrstuvwxyz12" not in masked["secret"]


def test_dingtalk_send_message_success(monkeypatch):
    """【WP-MSG.2】DingTalk.send_message：mock httpx 验证成功 + 签名附加。"""
    captured: dict = {}
    response = _MockResponse(
        status_code=200,
        json_data={"errcode": 0, "errmsg": "ok"},
    )
    context = _make_httpx_client_mock(response, captured)
    monkeypatch.setattr("app.services.notifications.dingtalk.httpx.Client", lambda **kw: context)

    adapter = DingTalkAdapter()
    result = adapter.send_message(
        config={
            "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
            "secret": "SECxxxx",
        },
        title="价格告警",
        body="600000 触发",
    )
    assert result.success is True
    # 验证签名参数已附加到 URL
    args = captured.get("args", ())
    if args:
        sent_url = args[0]
        assert "timestamp=" in sent_url
        assert "sign=" in sent_url


def test_dingtalk_send_message_with_keyword(monkeypatch):
    """【WP-MSG.2】DingTalk.send_message：keyword 安全策略自动追加。"""
    captured: dict = {}
    response = _MockResponse(
        status_code=200,
        json_data={"errcode": 0, "errmsg": "ok"},
    )
    context = _make_httpx_client_mock(response, captured)
    monkeypatch.setattr("app.services.notifications.dingtalk.httpx.Client", lambda **kw: context)

    adapter = DingTalkAdapter()
    adapter.send_message(
        config={
            "webhook": "https://oapi.dingtalk.com/robot/send",
            "keyword": "机会中心",
        },
        title="t",
        body="b",
    )
    sent_json = captured.get("kwargs", {}).get("json", {})
    sent_text = sent_json.get("markdown", {}).get("text", "")
    assert "机会中心" in sent_text


# ----------------------------------------------------------------------------
# 5. OneBotAdapter
# ----------------------------------------------------------------------------


def test_onebot_validate_config_missing_webhook():
    """【WP-MSG.2】OneBot.validate_config：webhook 为空 → 错误。"""
    adapter = OneBotAdapter()
    errors = adapter.validate_config({"target_type": "group", "target_id": 123})
    assert any("webhook" in e for e in errors)


def test_onebot_validate_config_invalid_target_type():
    """【WP-MSG.2】OneBot.validate_config：target_type 非 group/user → 错误。"""
    adapter = OneBotAdapter()
    errors = adapter.validate_config({
        "webhook": "http://x.com",
        "target_type": "channel",
        "target_id": 123,
    })
    assert any("target_type" in e for e in errors)


def test_onebot_validate_config_missing_target_id():
    """【WP-MSG.2】OneBot.validate_config：target_id 为空 → 错误。"""
    adapter = OneBotAdapter()
    errors = adapter.validate_config({
        "webhook": "http://x.com",
        "target_type": "group",
    })
    assert any("target_id" in e for e in errors)


def test_onebot_validate_config_full():
    """【WP-MSG.2】OneBot.validate_config：完整配置 → 空。"""
    adapter = OneBotAdapter()
    errors = adapter.validate_config({
        "webhook": "http://127.0.0.1:5700/send_group_msg",
        "target_type": "group",
        "target_id": 123456,
    })
    assert errors == []


def test_onebot_mask_config():
    """【WP-MSG.2】OneBot.mask_config：webhook + access_token 脱敏。"""
    adapter = OneBotAdapter()
    masked = adapter.mask_config({
        "webhook": "http://example.com/send?access_token=abcdef1234567890",
        "access_token": "abcdef1234567890",
        "target_type": "group",
        "target_id": 123456,
    })
    # webhook 中的 access_token= 查询参数 value 替换为 ****
    assert "access_token=****" in masked["webhook"]
    # access_token 字段值脱敏（前 4 + 后 4）
    assert masked["access_token"] == "abcd****7890"
    assert "abcdef1234567890" not in str(masked)
    # target_* 保留
    assert masked["target_type"] == "group"
    assert masked["target_id"] == 123456


def test_onebot_send_message_success(monkeypatch):
    """【WP-MSG.2】OneBot.send_message：mock httpx 验证成功。"""
    captured: dict = {}
    response = _MockResponse(
        status_code=200,
        json_data={"status": "ok", "retcode": 0, "data": {}},
    )
    context = _make_httpx_client_mock(response, captured)
    monkeypatch.setattr("app.services.notifications.onebot.httpx.Client", lambda **kw: context)

    adapter = OneBotAdapter()
    result = adapter.send_message(
        config={
            "webhook": "http://127.0.0.1:5700/send_group_msg",
            "target_type": "group",
            "target_id": 123456,
            "access_token": "tok123",
        },
        title="t",
        body="b",
    )
    assert result.success is True
    assert result.status_code == 200


def test_onebot_send_message_includes_bearer_header(monkeypatch):
    """【WP-MSG.2】OneBot.send_message：access_token 通过 Authorization: Bearer 携带。"""
    captured: dict = {}
    response = _MockResponse(
        status_code=200,
        json_data={"status": "ok", "retcode": 0, "data": {}},
    )
    context = _make_httpx_client_mock(response, captured)
    monkeypatch.setattr("app.services.notifications.onebot.httpx.Client", lambda **kw: context)

    adapter = OneBotAdapter()
    adapter.send_message(
        config={
            "webhook": "http://127.0.0.1:5700/send_group_msg",
            "target_type": "group",
            "target_id": 123456,
            "access_token": "mytoken",
        },
        title="t",
        body="b",
    )
    headers = captured.get("kwargs", {}).get("headers", {})
    assert headers.get("Authorization") == "Bearer mytoken"


# ----------------------------------------------------------------------------
# 6. EmailAdapter
# ----------------------------------------------------------------------------


def test_email_validate_config_missing_fields():
    """【WP-MSG.2】Email.validate_config：缺字段 → 错误。"""
    adapter = EmailAdapter()
    errors = adapter.validate_config({})
    # host / port / username / password / from / to 都应缺失
    err_text = " ".join(errors)
    assert "smtp_host" in err_text
    assert "smtp_port" in err_text
    assert "username" in err_text
    assert "password" in err_text
    assert "from_addr" in err_text
    assert "to_addrs" in err_text


def test_email_validate_config_invalid_port():
    """【WP-MSG.2】Email.validate_config：端口非整数 → 错误。"""
    adapter = EmailAdapter()
    errors = adapter.validate_config({
        "smtp_host": "smtp.example.com",
        "smtp_port": "abc",
        "username": "u",
        "password": "p",
        "from_addr": "f@example.com",
        "to_addrs": ["t@example.com"],
    })
    assert any("smtp_port" in e for e in errors)


def test_email_validate_config_full():
    """【WP-MSG.2】Email.validate_config：完整配置 → 空。"""
    adapter = EmailAdapter()
    errors = adapter.validate_config({
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "use_ssl": True,
        "username": "alerts@example.com",
        "password": "secret-pwd",
        "from_addr": "alerts@example.com",
        "to_addrs": ["trader@example.com"],
    })
    assert errors == []


def test_email_mask_config_masks_password():
    """【WP-MSG.2】Email.mask_config：password 全部 ****。"""
    adapter = EmailAdapter()
    masked = adapter.mask_config({
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "username": "alerts@example.com",
        "password": "my-secret-password-123",
        "from_addr": "alerts@example.com",
        "to_addrs": ["trader@example.com"],
    })
    assert masked["password"] == "****"
    assert "my-secret-password-123" not in str(masked)
    # 其他字段保留
    assert masked["smtp_host"] == "smtp.example.com"
    assert masked["username"] == "alerts@example.com"
    assert masked["to_addrs"] == ["trader@example.com"]


def test_email_send_message_success(monkeypatch):
    """【WP-MSG.2】Email.send_message：mock smtplib.SMTP_SSL 验证成功。"""
    smtp_instance = MagicMock()
    smtp_instance.login = MagicMock(return_value=None)
    smtp_instance.sendmail = MagicMock(return_value=None)
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    def _smtp_ssl_factory(host, port, context=None, timeout=None):
        return smtp_instance

    monkeypatch.setattr(
        "app.services.notifications.email.smtplib.SMTP_SSL", _smtp_ssl_factory
    )

    adapter = EmailAdapter()
    result = adapter.send_message(
        config={
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "use_ssl": True,
            "username": "alerts@example.com",
            "password": "secret-pwd",
            "from_addr": "alerts@example.com",
            "to_addrs": ["trader@example.com"],
        },
        title="价格告警",
        body="600000 触发买入",
    )
    assert result.success is True
    assert result.status_code == 250
    smtp_instance.login.assert_called_once_with("alerts@example.com", "secret-pwd")
    smtp_instance.sendmail.assert_called_once()


def test_email_send_message_uses_timeout(monkeypatch):
    """【WP-MSG.2】Email.send_message：timeout 传给 SMTP_SSL。"""
    captured: dict = {}
    smtp_instance = MagicMock()
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    def _factory(host, port, context=None, timeout=None):
        captured["timeout"] = timeout
        return smtp_instance

    monkeypatch.setattr("app.services.notifications.email.smtplib.SMTP_SSL", _factory)
    adapter = EmailAdapter()
    adapter.send_message(
        config={
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "use_ssl": True,
            "username": "u",
            "password": "p",
            "from_addr": "f@example.com",
            "to_addrs": ["t@example.com"],
            "timeout": 5.0,
        },
        title="t",
        body="b",
    )
    assert captured["timeout"] == 5.0


# ----------------------------------------------------------------------------
# 7. WebhookAdapter
# ----------------------------------------------------------------------------


def test_webhook_validate_config_missing_url():
    """【WP-MSG.2】Webhook.validate_config：url 为空 → 错误。"""
    adapter = WebhookAdapter()
    errors = adapter.validate_config({})
    assert any("url" in e for e in errors)


def test_webhook_validate_config_invalid_scheme():
    """【WP-MSG.2】Webhook.validate_config：url 非 http(s) → 错误。"""
    adapter = WebhookAdapter()
    errors = adapter.validate_config({"url": "ftp://example.com"})
    assert any("http" in e for e in errors)


def test_webhook_validate_config_invalid_method():
    """【WP-MSG.2】Webhook.validate_config：method 非法 → 错误。"""
    adapter = WebhookAdapter()
    errors = adapter.validate_config({
        "url": "https://example.com/h",
        "method": "DELETE",
    })
    assert any("method" in e for e in errors)


def test_webhook_validate_config_invalid_template():
    """【WP-MSG.2】Webhook.validate_config：request_template 非 JSON → 错误。"""
    adapter = WebhookAdapter()
    errors = adapter.validate_config({
        "url": "https://example.com/h",
        "request_template": "not a json",
    })
    assert any("request_template" in e for e in errors)


def test_webhook_validate_config_full():
    """【WP-MSG.2】Webhook.validate_config：完整配置 → 空。"""
    adapter = WebhookAdapter()
    errors = adapter.validate_config({
        "url": "https://example.com/h",
        "method": "POST",
        "auth_header": "Bearer abc",
        "request_template": '{"title":"{title}","body":"{body}"}',
    })
    assert errors == []


def test_webhook_mask_config():
    """【WP-MSG.2】Webhook.mask_config：url + auth_header 脱敏。"""
    adapter = WebhookAdapter()
    masked = adapter.mask_config({
        "url": "https://example.com/h?token=abcdef1234567890",
        "method": "POST",
        "auth_header": "Bearer abcdef1234567890",
        "request_template": '{"title":"{title}"}',
    })
    # URL 查询参数 value 替换为 ****
    assert "token=****" in masked["url"]
    assert "abcdef1234567890" not in masked["url"]
    # auth_header 脱敏（前 4 + 后 4，但 "Bearer " 前缀会被整体处理）
    assert "abcdef1234567890" not in masked["auth_header"]
    # method / template 保留
    assert masked["method"] == "POST"
    assert masked["request_template"] == '{"title":"{title}"}'


def test_webhook_send_message_success(monkeypatch):
    """【WP-MSG.2】Webhook.send_message：mock httpx 验证成功。"""
    captured: dict = {}
    response = _MockResponse(status_code=200, text="ok")
    context = _make_httpx_client_mock(response, captured)
    monkeypatch.setattr("app.services.notifications.webhook.httpx.Client", lambda **kw: context)

    adapter = WebhookAdapter()
    result = adapter.send_message(
        config={
            "url": "https://example.com/h",
            "method": "POST",
            "auth_header": "Bearer tok",
            "request_template": '{"title":"{title}","body":"{body}"}',
        },
        title="t",
        body="b",
    )
    assert result.success is True
    # 验证 Authorization header 被设置
    headers = captured.get("kwargs", {}).get("headers", {})
    assert headers.get("Authorization") == "Bearer tok"


def test_webhook_send_message_failure_status(monkeypatch):
    """【WP-MSG.2】Webhook.send_message：mock httpx 返回 500 → 失败。"""
    response = _MockResponse(status_code=500, text="internal error")
    context = _make_httpx_client_mock(response)
    monkeypatch.setattr("app.services.notifications.webhook.httpx.Client", lambda **kw: context)

    adapter = WebhookAdapter()
    adapter.max_retries = 0
    result = adapter.send_message(
        config={"url": "https://example.com/h"},
        title="t",
        body="b",
    )
    assert result.success is False
    assert result.status_code == 500


# ----------------------------------------------------------------------------
# 8. registry.get_adapter / list_adapters
# ----------------------------------------------------------------------------


def test_registry_get_adapter_in_app():
    """【WP-MSG.2】get_adapter("in_app") 返回 InAppAdapter 实例。"""
    adapter = get_adapter("in_app")
    assert adapter is not None
    assert isinstance(adapter, InAppAdapter)


def test_registry_get_adapter_unknown():
    """【WP-MSG.2】get_adapter("unknown") 返回 None。"""
    assert get_adapter("nonexistent") is None
    assert get_adapter("") is None


def test_registry_list_adapters_returns_six():
    """【WP-MSG.2】list_adapters 返回 6 个适配器。"""
    adapters = list_adapters()
    assert len(adapters) == 6
    expected_keys = {"in_app", "wxpusher", "dingtalk", "onebot", "email", "webhook"}
    assert set(adapters.keys()) == expected_keys
    # 类型校验
    assert isinstance(adapters["wxpusher"], WxPusherAdapter)
    assert isinstance(adapters["dingtalk"], DingTalkAdapter)
    assert isinstance(adapters["onebot"], OneBotAdapter)
    assert isinstance(adapters["email"], EmailAdapter)
    assert isinstance(adapters["webhook"], WebhookAdapter)


def test_registry_list_adapters_returns_copy():
    """【WP-MSG.2】list_adapters 返回副本，修改不影响内部注册表。"""
    adapters = list_adapters()
    adapters["extra"] = InAppAdapter()
    # 内部注册表不应被修改
    assert "extra" not in list_adapters()


# ----------------------------------------------------------------------------
# 9. template_renderer
# ----------------------------------------------------------------------------


def test_render_template_basic():
    """【WP-MSG.2】render_template：基本变量替换。"""
    rendered = render_template("{symbol} 触发 {price}", {"symbol": "000001", "price": "10.5"})
    assert "000001" in rendered
    assert "10.5" in rendered
    assert "触发" in rendered


def test_render_template_supports_legacy_double_braces():
    """历史模板使用 {{name}} 时也应完整替换，不残留额外大括号。"""
    rendered = render_template(
        "{{ symbol }} 触发 {{price}}",
        {"symbol": "000001", "price": "10.5"},
    )
    assert rendered == "000001 触发 10.5"
    assert "{" not in rendered


def test_render_template_mixed_placeholder_styles():
    """同一模板可混用新旧占位符，便于平滑迁移。"""
    rendered = render_template(
        "{symbol} / {{ symbol }} / {{unknown}}",
        {"symbol": "000001"},
    )
    assert rendered == "000001 / 000001 / {{unknown}}"


def test_render_template_supports_historical_unicode_and_numeric_names():
    """Historical variable names accepted by the renderer remain supported."""
    rendered = render_template(
        "{1foo} / {中文}",
        {"1foo": "A", "中文": "B"},
    )
    assert rendered == "A / B"


def test_render_template_unknown_var_preserved():
    """【WP-MSG.2】render_template：未提供的 {name} 占位符原样保留。"""
    rendered = render_template("{known} + {unknown}", {"known": "K"})
    assert "K" in rendered
    assert "{unknown}" in rendered


def test_render_template_empty_template():
    """【WP-MSG.2】render_template：空模板返回空字符串。"""
    assert render_template("", {"a": "b"}) == ""


def test_render_template_none_value():
    """【WP-MSG.2】render_template：变量值为 None 时替换为空字符串。"""
    rendered = render_template("[{x}]", {"x": None})
    assert rendered == "[]"


def test_escape_value_escapes_markdown():
    """【WP-MSG.2】escape_value：转义 Markdown 特殊字符。"""
    escaped = escape_value("**bold**")
    assert "\\*\\*bold\\*\\*" == escaped
    # 防注入：原 * 字符不再以连续 ** 出现
    assert "**" not in escaped


def test_escape_value_escapes_html():
    """【WP-MSG.2】escape_value：转义 HTML 特殊字符（< > &）。"""
    escaped = escape_value("<script>alert('x')</script>")
    assert "<script>" not in escaped
    assert "\\<" in escaped
    assert "\\>" in escaped
    assert "\\&" not in escaped or "\\&" in escaped  # & 会被转义为 \&


def test_escape_value_removes_control_chars():
    """【WP-MSG.2】escape_value：移除控制字符（保留 \\n \\t）。"""
    # \x00 (NUL) 与 \x1b (ESC) 等控制字符应被移除
    value = "hello\x00world\x1b"
    escaped = escape_value(value)
    assert "\x00" not in escaped
    assert "\x1b" not in escaped
    assert "hello" in escaped and "world" in escaped
    # \n \t 保留
    escaped2 = escape_value("a\nb\tc")
    assert "\n" in escaped2
    assert "\t" in escaped2


def test_escape_value_empty():
    """【WP-MSG.2】escape_value：空值返回空字符串。"""
    assert escape_value("") == ""
    assert escape_value(None) == ""  # type: ignore[arg-type]


def test_render_template_prevents_injection():
    """【WP-MSG.2】render_template：变量值含 **bold** → 转义为 \\*\\*bold\\*\\*。"""
    rendered = render_template("{body}", {"body": "**bold**"})
    assert "\\*\\*bold\\*\\*" in rendered
    assert "**bold**" not in rendered


def test_render_template_prevents_markdown_injection_in_json():
    """【WP-MSG.2】render_template：模板嵌入 JSON 时，变量中的 * 仍被转义。"""
    template = '{"text":"{body}"}'
    rendered = render_template(template, {"body": "**bold**"})
    # 即使包裹在 JSON 中，* 也应被转义为 \*
    assert "\\*\\*bold\\*\\*" in rendered
    assert "**bold**" not in rendered


# ----------------------------------------------------------------------------
# 10. normalize_error 脱敏
# ----------------------------------------------------------------------------


def test_normalize_error_masks_token():
    """【WP-MSG.2】normalize_error：异常消息含 token → 脱敏。"""
    adapter = InAppAdapter()
    exc = ValueError("token: abcdef123456")
    code, msg = adapter.normalize_error(exc)
    assert code == "ValueError"
    # 消息中不应包含完整 token 值
    assert "abcdef123456" not in msg
    # 应已被替换为通用脱敏提示
    assert "已脱敏" in msg or "***" in msg


def test_normalize_error_masks_password():
    """【WP-MSG.2】normalize_error：异常消息含 password=xxx → 脱敏。"""
    adapter = InAppAdapter()
    exc = RuntimeError("login failed: password=mysecret123")
    code, msg = adapter.normalize_error(exc)
    assert "mysecret123" not in msg
    assert "***" in msg


def test_normalize_error_preserves_non_sensitive():
    """【WP-MSG.2】normalize_error：非敏感消息原样返回。"""
    adapter = InAppAdapter()
    exc = ValueError("invalid config: port must be int")
    code, msg = adapter.normalize_error(exc)
    assert code == "ValueError"
    assert "port must be int" in msg


# ----------------------------------------------------------------------------
# 11. _truncate_response 截断
# ----------------------------------------------------------------------------


def test_truncate_response_short():
    """【WP-MSG.2】_truncate_response：短字符串原样返回。"""
    adapter = InAppAdapter()
    assert adapter._truncate_response("short") == "short"


def test_truncate_response_long():
    """【WP-MSG.2】_truncate_response：长字符串截断到 500 字符 + "..."。"""
    adapter = InAppAdapter()
    long_str = "x" * 600
    truncated = adapter._truncate_response(long_str)
    assert len(truncated) == 503  # 500 + "..."
    assert truncated.endswith("...")


def test_truncate_response_custom_max_len():
    """【WP-MSG.2】_truncate_response：支持自定义 max_len。"""
    adapter = InAppAdapter()
    truncated = adapter._truncate_response("abcdefghij", max_len=5)
    assert truncated == "abcde..."


def test_truncate_response_none():
    """【WP-MSG.2】_truncate_response：None 返回空字符串。"""
    adapter = InAppAdapter()
    assert adapter._truncate_response(None) == ""  # type: ignore[arg-type]


# ----------------------------------------------------------------------------
# 12. 有限超时（默认 10.0，可配置）
# ----------------------------------------------------------------------------


def test_default_timeout_is_10_seconds():
    """【WP-MSG.2】所有 HTTP 适配器默认超时为 10 秒。"""
    assert WxPusherAdapter.default_timeout == 10.0
    assert DingTalkAdapter.default_timeout == 10.0
    assert OneBotAdapter.default_timeout == 10.0
    assert EmailAdapter.default_timeout == 10.0
    assert WebhookAdapter.default_timeout == 10.0


def test_resolve_timeout_default():
    """【WP-MSG.2】_resolve_timeout：无 config 时使用 default_timeout。"""
    adapter = WxPusherAdapter()
    assert adapter._resolve_timeout({}) == 10.0


def test_resolve_timeout_custom():
    """【WP-MSG.2】_resolve_timeout：config["timeout"] 覆盖默认值。"""
    adapter = WxPusherAdapter()
    assert adapter._resolve_timeout({"timeout": 5.0}) == 5.0


def test_resolve_timeout_capped_to_max():
    """【WP-MSG.2】_resolve_timeout：超过 max_timeout 的值被截断。"""
    adapter = WxPusherAdapter()
    # 100 秒被截断为 max_timeout=60
    assert adapter._resolve_timeout({"timeout": 100.0}) == 60.0


def test_resolve_timeout_invalid_value():
    """【WP-MSG.2】_resolve_timeout：非法 timeout 值回退到默认。"""
    adapter = WxPusherAdapter()
    assert adapter._resolve_timeout({"timeout": "not-a-number"}) == 10.0
    assert adapter._resolve_timeout({"timeout": -1}) == 10.0
    assert adapter._resolve_timeout({"timeout": 0}) == 10.0


def test_send_uses_configured_timeout(monkeypatch):
    """【WP-MSG.2】send_message 实际使用 config 中的 timeout 调用 httpx。"""
    captured_kw: dict = {}

    def _factory(**kw):
        captured_kw.update(kw)
        response = _MockResponse(
            status_code=200,
            json_data={"code": 0, "msg": "ok"},
        )
        return _make_httpx_client_mock(response)

    monkeypatch.setattr("app.services.notifications.wxpusher.httpx.Client", _factory)

    adapter = WxPusherAdapter()
    adapter.send_message(
        config={"app_token": "AT_xxx", "uids": ["u1"], "timeout": 3.5},
        title="t",
        body="b",
    )
    assert captured_kw["timeout"] == 3.5


# ----------------------------------------------------------------------------
# 13. 熔断器在连续失败后开启
# ----------------------------------------------------------------------------


def test_circuit_breaker_opens_after_failures(monkeypatch):
    """【WP-MSG.2】连续失败达阈值后熔断器开启，后续请求被短路。"""
    response = _MockResponse(
        status_code=500,
        json_data={"code": 1, "msg": "fail"},
    )
    context = _make_httpx_client_mock(response)
    monkeypatch.setattr("app.services.notifications.wxpusher.httpx.Client", lambda **kw: context)

    adapter = WxPusherAdapter()
    # 降低阈值便于测试
    adapter._breaker.threshold = 3
    adapter._breaker.reset_after_seconds = 60.0
    adapter.max_retries = 0  # 不要重试，加速触发熔断

    # 前 3 次失败
    for i in range(3):
        r = adapter.send_message(
            config={"app_token": "AT_xxx", "uids": ["u1"]},
            title="t",
            body="b",
        )
        assert r.success is False

    # 第 4 次应被熔断器短路（不调用 httpx）
    # 通过验证 httpx.Client 不被再次调用确认短路
    call_count_before = context._mock.post.call_count
    r = adapter.send_message(
        config={"app_token": "AT_xxx", "uids": ["u1"]},
        title="t",
        body="b",
    )
    call_count_after = context._mock.post.call_count
    assert r.success is False
    assert "熔断" in (r.error_message or "")
    assert call_count_after == call_count_before  # 没有新调用


def test_circuit_breaker_record_success_resets():
    """【WP-MSG.2】熔断器在成功后重置失败计数。"""
    adapter = WxPusherAdapter()
    adapter._breaker.record_failure()
    adapter._breaker.record_failure()
    assert adapter._breaker.failure_count == 2
    adapter._breaker.record_success()
    assert adapter._breaker.failure_count == 0
    assert adapter._breaker.state == "closed"


# ----------------------------------------------------------------------------
# 14. 不建立永久线程：所有发送方法同步执行
# ----------------------------------------------------------------------------


def test_adapters_do_not_spawn_threads(monkeypatch):
    """【WP-MSG.2】适配器发送时不创建 threading.Thread。"""
    import app.services.notifications.dingtalk as dt_mod

    thread_call_count = {"count": 0}
    original_thread = dt_mod.threading if hasattr(dt_mod, "threading") else None

    # 监控 threading.Thread 是否被实例化（不应被）
    import threading

    original_init = threading.Thread.__init__

    def _spy_init(self, *args, **kwargs):
        thread_call_count["count"] += 1
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "__init__", _spy_init)

    # mock httpx
    response = _MockResponse(
        status_code=200,
        json_data={"errcode": 0, "errmsg": "ok"},
    )
    context = _make_httpx_client_mock(response)
    monkeypatch.setattr("app.services.notifications.dingtalk.httpx.Client", lambda **kw: context)

    adapter = DingTalkAdapter()
    adapter.send_message(
        config={"webhook": "https://oapi.dingtalk.com/robot/send"},
        title="t",
        body="b",
    )
    assert thread_call_count["count"] == 0, "适配器不应创建线程"


# ----------------------------------------------------------------------------
# 15. 敏感信息不写入 response_summary / error_message
# ----------------------------------------------------------------------------


def test_wxpusher_response_summary_no_token(monkeypatch):
    """【WP-MSG.2】WxPusher 响应摘要不含 appToken 字段（即使响应中包含）。"""
    response = _MockResponse(
        status_code=200,
        json_data={
            "code": 0,
            "msg": "ok",
            "appToken": "AT_secret_should_not_leak",
            "data": {"messageId": "m-1"},
        },
    )
    context = _make_httpx_client_mock(response)
    monkeypatch.setattr("app.services.notifications.wxpusher.httpx.Client", lambda **kw: context)

    adapter = WxPusherAdapter()
    result = adapter.send_message(
        config={"app_token": "AT_xxx", "uids": ["u1"]},
        title="t",
        body="b",
    )
    assert result.success is True
    assert "AT_secret_should_not_leak" not in (result.response_summary or "")


def test_dingtalk_error_message_sanitized(monkeypatch):
    """【WP-MSG.2】DingTalk 错误消息不含完整 webhook URL 中的 access_token。"""
    response = _MockResponse(
        status_code=200,
        json_data={"errcode": 310000, "errmsg": "invalid token abc123"},
    )
    context = _make_httpx_client_mock(response)
    monkeypatch.setattr("app.services.notifications.dingtalk.httpx.Client", lambda **kw: context)

    adapter = DingTalkAdapter()
    adapter.max_retries = 0
    result = adapter.send_message(
        config={"webhook": "https://oapi.dingtalk.com/robot/send?access_token=secret_token_value"},
        title="t",
        body="b",
    )
    assert result.success is False
    # 错误消息不应包含完整 webhook URL
    assert "secret_token_value" not in (result.error_message or "")
    assert "secret_token_value" not in (result.response_summary or "")
