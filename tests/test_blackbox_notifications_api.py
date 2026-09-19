"""黑盒测试 - 通知消息管理 HTTP API（UAT-P0.1）。

通过 FastAPI TestClient + get_db 依赖覆盖，端到端验证 app/api/routes/notifications.py：
1. 渠道 CRUD + 测试发送 + 脱敏
2. 策略 CRUD + 状态机（未配置渠道不能被选中）
3. 模板 CRUD + 预览 + 变量转义
4. 发送记录查询 + 筛选
5. 错误协议（404/400/409 返回统一错误格式）

project_memory 硬约束：
- API/响应不出现完整 Token、Webhook Secret、SMTP 密码
- 错误使用统一错误协议，中文文案
- 不破坏已验收的 P0/P1/P2/P3 功能
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.notification import (
    CHANNEL_STATUS_DISABLED,
    CHANNEL_STATUS_PENDING_TEST,
    CHANNEL_STATUS_TEST_SUCCESS,
    NotificationChannel,
    NotificationOutbox,
)
from app.services.notifications.dispatcher import Dispatcher
from app.services.notifications.policies import emit_event


pytestmark = pytest.mark.blackbox


# ----------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------


@pytest.fixture()
def client(db_session):
    """构造 TestClient，依赖覆盖让 get_db 返回测试 session。"""
    from app.main import app

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _assert_unified_error(resp, expected_status: int) -> dict:
    """断言响应为统一错误协议格式，返回解析后的 JSON。"""
    assert resp.status_code == expected_status, (
        f"期望 {expected_status}，实际 {resp.status_code}：{resp.text}"
    )
    body = resp.json()
    # 统一错误协议必填字段
    for key in ("error_code", "user_message", "impact", "retryable", "correlation_id"):
        assert key in body, f"统一错误响应缺少字段 {key}：{body}"
    # user_message 必须为中文（非裸露技术文案）
    assert body["user_message"], "user_message 不能为空"
    assert body["correlation_id"], "correlation_id 不能为空"
    # 禁止裸露技术文案
    assert body["user_message"] not in ("Request failed", "HTTP 500", "NoneType")
    return body


# ----------------------------------------------------------------------------
# 1. 渠道 Channels CRUD + 测试发送 + 脱敏
# ----------------------------------------------------------------------------


def test_create_in_app_channel(client):
    """创建 in_app 渠道：初始状态 test_success，响应不含 config_encrypted_json。"""
    resp = client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-in-app", "channel_type": "in_app", "config": {}},
    )
    assert resp.status_code == 200, resp.text
    ch = resp.json()
    assert ch["name"] == "qa-in-app"
    assert ch["channel_type"] == "in_app"
    assert ch["enabled"] is False
    assert ch["status"] == CHANNEL_STATUS_TEST_SUCCESS
    # 永不返回加密配置
    assert "config_encrypted_json" not in ch
    # 必填字段完整（前端依赖）
    for key in (
        "id", "name", "channel_type", "enabled", "status", "config_mask_json",
        "verified_at", "last_test_at", "last_test_success", "last_error_code",
        "last_error_message", "created_at", "updated_at",
    ):
        assert key in ch, f"渠道响应缺少字段 {key}"


def test_create_wxpusher_channel_masks_token(client):
    """创建 wxpusher 渠道：响应 config_mask_json 脱敏，不含明文 app_token。"""
    raw_token = "app_token_super_secret_1234567890abcdef"
    resp = client.post(
        "/api/v1/notifications/channels",
        json={
            "name": "qa-wxpusher",
            "channel_type": "wxpusher",
            "config": {"app_token": raw_token, "uids": ["UID_001"]},
        },
    )
    assert resp.status_code == 200, resp.text
    ch = resp.json()
    assert ch["status"] == CHANNEL_STATUS_PENDING_TEST  # 第三方需测试
    assert "config_encrypted_json" not in ch
    # 脱敏配置存在且不含明文 token
    masked = json.loads(ch["config_mask_json"])
    assert "app_token" in masked
    assert raw_token not in json.dumps(masked), "明文 app_token 泄露到响应"
    assert "****" in masked["app_token"], "app_token 未脱敏"


def test_create_channel_duplicate_name_conflict(client):
    """重复渠道名 → 409 统一错误协议。"""
    client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-dup", "channel_type": "in_app", "config": {}},
    )
    resp = client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-dup", "channel_type": "in_app", "config": {}},
    )
    body = _assert_unified_error(resp, 409)
    assert body["error_code"] == "DB_INTEGRITY_VIOLATION"
    assert "qa-dup" in body["user_message"]


def test_create_channel_invalid_type(client):
    """不支持的渠道类型 → 400。"""
    resp = client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-bad", "channel_type": "telegram", "config": {}},
    )
    body = _assert_unified_error(resp, 400)
    assert body["error_code"] == "VALIDATION_ERROR"


def test_list_channels_with_filter(client):
    """列表 + enabled 筛选。"""
    client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-list-a", "channel_type": "in_app", "config": {}},
    )
    resp = client.get("/api/v1/notifications/channels?enabled=false")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert any(it["name"] == "qa-list-a" for it in items)
    # enabled=true 筛选应不含未启用项
    resp2 = client.get("/api/v1/notifications/channels?enabled=true")
    assert all(it["enabled"] is True for it in resp2.json())


def test_update_channel_enable_state_machine(client):
    """启用 in_app 渠道：enabled=True。"""
    r = client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-enable", "channel_type": "in_app", "config": {}},
    )
    cid = r.json()["id"]
    resp = client.patch(
        f"/api/v1/notifications/channels/{cid}",
        json={"enabled": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True


def test_enable_untested_third_party_channel_rejected(client):
    """未测试的第三方渠道不可启用 → 400（状态机约束）。"""
    r = client.post(
        "/api/v1/notifications/channels",
        json={
            "name": "qa-wx-untested",
            "channel_type": "wxpusher",
            "config": {"app_token": "tok_1234567890abcdef", "uids": ["U1"]},
        },
    )
    cid = r.json()["id"]
    resp = client.patch(
        f"/api/v1/notifications/channels/{cid}",
        json={"enabled": True},
    )
    body = _assert_unified_error(resp, 400)
    assert body["error_code"] == "PORTFOLIO_RULE_INVALID"


def test_test_channel_writes_delivery(client):
    """测试发送 in_app 渠道：成功 + 写入 notification_deliveries（可追踪）。"""
    r = client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-test-send", "channel_type": "in_app", "config": {}},
    )
    cid = r.json()["id"]
    resp = client.post(f"/api/v1/notifications/channels/{cid}/test")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["success"] is True
    assert result["delivery_id"] is not None  # 可追踪
    # 渠道状态更新
    ch = client.get("/api/v1/notifications/channels").json()
    target = [c for c in ch if c["id"] == cid][0]
    assert target["status"] == CHANNEL_STATUS_TEST_SUCCESS
    assert target["last_test_success"] is True
    assert target["verified_at"] is not None


def test_delete_channel_soft(client):
    """删除渠道（默认软删除）：禁用 + disabled。"""
    r = client.post(
        "/api/v1/notifications/channels",
        json={"name": "qa-del", "channel_type": "in_app", "config": {}},
    )
    cid = r.json()["id"]
    resp = client.delete(f"/api/v1/notifications/channels/{cid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    ch = [c for c in client.get("/api/v1/notifications/channels").json() if c["id"] == cid][0]
    assert ch["enabled"] is False
    assert ch["status"] == CHANNEL_STATUS_DISABLED


def test_get_channel_not_found(client):
    """不存在的渠道 → 404 统一错误协议。"""
    resp = client.delete("/api/v1/notifications/channels/999999")
    body = _assert_unified_error(resp, 404)
    assert body["error_code"] == "NOT_FOUND"


# ----------------------------------------------------------------------------
# 2. 策略 Policies CRUD + 状态机
# ----------------------------------------------------------------------------


def _make_enabled_in_app_channel(client, name: str) -> int:
    """创建并启用一个 in_app 渠道，返回 id（in_app 可被策略选中）。"""
    r = client.post(
        "/api/v1/notifications/channels",
        json={"name": name, "channel_type": "in_app", "config": {}},
    )
    cid = r.json()["id"]
    client.patch(f"/api/v1/notifications/channels/{cid}", json={"enabled": True})
    return cid


def test_create_policy_with_unconfigured_channel_rejected(client):
    """未测试/未启用的第三方渠道不能被策略选中 → 400。"""
    r = client.post(
        "/api/v1/notifications/channels",
        json={
            "name": "qa-pol-bad-ch",
            "channel_type": "wxpusher",
            "config": {"app_token": "tok_1234567890abcdef", "uids": ["U1"]},
        },
    )
    bad_cid = r.json()["id"]
    resp = client.post(
        "/api/v1/notifications/policies",
        json={
            "name": "qa-pol-bad",
            "source_types": ["system"],
            "channel_ids": [bad_cid],
        },
    )
    body = _assert_unified_error(resp, 400)
    assert body["error_code"] == "PORTFOLIO_RULE_INVALID"
    assert "qa-pol-bad-ch" in body["user_message"]


def test_policy_crud_with_selectable_channel(client):
    """策略 CRUD：使用已启用 in_app 渠道。"""
    cid = _make_enabled_in_app_channel(client, "qa-pol-ch")

    # 创建
    resp = client.post(
        "/api/v1/notifications/policies",
        json={
            "name": "qa-policy",
            "enabled": True,
            "source_types": ["system", "alert"],
            "min_severity": "warn",
            "scope_type": "all",
            "delivery_mode": "instant",
            "cooldown_minutes": 5,
            "channel_ids": [cid],
        },
    )
    assert resp.status_code == 200, resp.text
    pol = resp.json()
    assert pol["name"] == "qa-policy"
    assert pol["channel_ids"] == [cid]
    assert json.loads(pol["source_types_json"]) == ["system", "alert"]
    pid = pol["id"]

    # 列表
    resp = client.get("/api/v1/notifications/policies")
    assert any(p["id"] == pid for p in resp.json())

    # 更新
    resp = client.patch(
        f"/api/v1/notifications/policies/{pid}",
        json={"enabled": False, "cooldown_minutes": 10},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["enabled"] is False
    assert updated["cooldown_minutes"] == 10

    # 删除
    resp = client.delete(f"/api/v1/notifications/policies/{pid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # 删除后列表不含
    assert not any(p["id"] == pid for p in client.get("/api/v1/notifications/policies").json())


def test_policy_nonexistent_channel_rejected(client):
    """策略引用不存在的渠道 id → 404。"""
    resp = client.post(
        "/api/v1/notifications/policies",
        json={"name": "qa-pol-missing", "channel_ids": [888888]},
    )
    body = _assert_unified_error(resp, 404)
    assert body["error_code"] == "NOT_FOUND"


def test_update_policy_not_found(client):
    """更新不存在的策略 → 404。"""
    resp = client.patch(
        "/api/v1/notifications/policies/777777",
        json={"enabled": True},
    )
    _assert_unified_error(resp, 404)


# ----------------------------------------------------------------------------
# 3. 模板 Templates CRUD + 预览 + 变量转义
# ----------------------------------------------------------------------------


def test_template_crud(client):
    """模板 CRUD。"""
    # 创建
    resp = client.post(
        "/api/v1/notifications/templates",
        json={
            "name": "qa-tpl",
            "title_template": "告警：{symbol}",
            "body_template": "价格变动 {price}",
            "body_text_template": "价格变动 {price}",
            "variables_json": '[{"name":"symbol","description":"标的"},{"name":"price","description":"价格"}]',
            "is_active": True,
        },
    )
    assert resp.status_code == 200, resp.text
    tpl = resp.json()
    assert tpl["name"] == "qa-tpl"
    assert tpl["version"] == 1
    tid = tpl["id"]

    # 列表
    assert any(t["id"] == tid for t in client.get("/api/v1/notifications/templates").json())

    # 更新（version 递增）
    resp = client.patch(
        f"/api/v1/notifications/templates/{tid}",
        json={"is_active": False, "body_template": "新内容 {price}"},
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert resp.json()["version"] == 2

    # Explicit null clears optional fields instead of meaning "not provided".
    resp = client.patch(
        f"/api/v1/notifications/templates/{tid}",
        json={"body_text_template": None, "variables_json": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["body_text_template"] is None
    assert resp.json()["variables_json"] is None

    # 删除
    resp = client.delete(f"/api/v1/notifications/templates/{tid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_template_preview_variable_escaping(client):
    """模板预览：变量值转义，防止 Markdown/Webhook 注入。"""
    r = client.post(
        "/api/v1/notifications/templates",
        json={
            "name": "qa-tpl-escape",
            "title_template": "标题 {sym}",
            "body_template": "正文 {sym}",
        },
    )
    tid = r.json()["id"]
    # 注入载荷：Markdown 特殊字符 + 方括号链接
    evil = "**bold** [link](http://evil) <script>x</script>"
    resp = client.post(
        f"/api/v1/notifications/templates/{tid}/preview",
        json={"variables": {"sym": evil}},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # 原始注入串不应原样出现（应被反斜杠转义）
    assert "**bold**" not in out["title"], "Markdown 注入未转义"
    assert "[link]" not in out["body"], "链接注入未转义"
    assert "<script>" not in out["title"], "HTML 注入未转义"
    # 转义后应包含反斜杠前缀
    assert "\\" in out["title"]


def test_template_preview_supports_double_brace_placeholders(client):
    """历史双大括号模板通过 API 预览时应正确渲染。"""
    response = client.post(
        "/api/v1/notifications/templates",
        json={
            "name": "qa-tpl-double-braces",
            "title_template": "标题 {{symbol}}",
            "body_template": "正文 {{ symbol }} / {price}",
        },
    )
    template_id = response.json()["id"]

    response = client.post(
        f"/api/v1/notifications/templates/{template_id}/preview",
        json={"variables": {"symbol": "000001", "price": "10.50"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "标题 000001"
    assert response.json()["body"] == "正文 000001 / 10.50"


def test_template_preview_not_found(client):
    """预览不存在的模板 → 404。"""
    resp = client.post(
        "/api/v1/notifications/templates/666666/preview",
        json={"variables": {}},
    )
    _assert_unified_error(resp, 404)


def test_create_template_duplicate_name(client):
    """模板名重复 → 409。"""
    client.post(
        "/api/v1/notifications/templates",
        json={"name": "qa-tpl-dup", "title_template": "t", "body_template": "b"},
    )
    resp = client.post(
        "/api/v1/notifications/templates",
        json={"name": "qa-tpl-dup", "title_template": "t", "body_template": "b"},
    )
    body = _assert_unified_error(resp, 409)
    assert body["error_code"] == "DB_INTEGRITY_VIOLATION"


# ----------------------------------------------------------------------------
# 4. 发送记录 Deliveries / Outbox 查询 + 筛选
# ----------------------------------------------------------------------------


def test_list_deliveries_with_source_type_filter(client):
    """测试发送后，deliveries 可按 source_type=system 查到。"""
    cid = _make_enabled_in_app_channel(client, "qa-delivery-ch")
    client.post(f"/api/v1/notifications/channels/{cid}/test")

    # 按 source_type=system 筛选
    resp = client.get("/api/v1/notifications/deliveries?source_type=system")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)
    assert len(items) >= 1
    assert all(it["channel_id"] == cid for it in items)
    # 字段完整性
    for key in (
        "id", "outbox_id", "channel_id", "attempt_number", "status",
        "status_code", "response_summary", "error_code", "error_message",
        "duration_ms", "sent_at", "created_at",
    ):
        assert key in items[0], f"delivery 响应缺少字段 {key}"


def test_list_deliveries_by_channel_filter(client):
    """按 channel_id 筛选 deliveries。"""
    cid = _make_enabled_in_app_channel(client, "qa-delivery-ch2")
    client.post(f"/api/v1/notifications/channels/{cid}/test")
    resp = client.get(f"/api/v1/notifications/deliveries?channel_id={cid}")
    assert resp.status_code == 200
    assert all(it["channel_id"] == cid for it in resp.json())


def test_list_outbox_and_detail(client):
    """outbox 列表 + 详情 deliveries。"""
    cid = _make_enabled_in_app_channel(client, "qa-outbox-ch")
    client.post(f"/api/v1/notifications/channels/{cid}/test")

    resp = client.get("/api/v1/notifications/outbox?source_type=system")
    assert resp.status_code == 200
    outboxes = resp.json()
    assert len(outboxes) >= 1
    oid = outboxes[0]["id"]

    # 详情：该 outbox 的 deliveries
    resp = client.get(f"/api/v1/notifications/outbox/{oid}/deliveries")
    assert resp.status_code == 200
    deliveries = resp.json()
    assert len(deliveries) >= 1
    assert deliveries[0]["outbox_id"] == oid


def test_outbox_retry_invalid_state_rejected(client):
    """outbox 非 failed/dead_letter 不可重发 → 400。"""
    cid = _make_enabled_in_app_channel(client, "qa-retry-ch")
    client.post(f"/api/v1/notifications/channels/{cid}/test")
    # 测试发送后 outbox 状态为 sent，不可重发
    oid = client.get("/api/v1/notifications/outbox").json()[0]["id"]
    resp = client.post(f"/api/v1/notifications/outbox/{oid}/retry")
    body = _assert_unified_error(resp, 400)
    assert body["error_code"] == "VALIDATION_ERROR"


def test_outbox_retry_not_found(client):
    """重发不存在的 outbox → 404。"""
    resp = client.post("/api/v1/notifications/outbox/555555/retry")
    _assert_unified_error(resp, 404)


def test_list_deliveries_invalid_date_format(client):
    """非法日期格式 → 400。"""
    resp = client.get("/api/v1/notifications/deliveries?date_from=not-a-date")
    body = _assert_unified_error(resp, 400)
    assert body["error_code"] == "VALIDATION_ERROR"


# ----------------------------------------------------------------------------
# 5. Real in-app inbox integration
# ----------------------------------------------------------------------------


def test_inbox_uses_real_in_app_outbox_and_persists_read_state(client, db_session):
    channel_id = _make_enabled_in_app_channel(client, "qa-real-inbox")
    test_send = client.post(f"/api/v1/notifications/channels/{channel_id}/test")
    assert test_send.status_code == 200, test_send.text

    response = client.get("/api/v1/notifications/inbox")
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 1
    item = items[0]
    assert item["channel_id"] == channel_id
    assert item["event_type"] == "channel_test"
    assert item["title"] != "风险预警：组合回撤达 8.3%"
    assert item["read"] is False

    marked = client.put(f"/api/v1/notifications/inbox/{item['id']}/read")
    assert marked.status_code == 200, marked.text
    assert marked.json()["read"] is True

    persisted = client.get("/api/v1/notifications/inbox").json()
    assert persisted[0]["read"] is True
    db_session.expire_all()
    assert db_session.get(NotificationOutbox, item["id"]).read_at is not None


def test_inbox_filters_external_channels_and_read_all_static_route(client, db_session):
    in_app_id = _make_enabled_in_app_channel(client, "qa-inbox-in-app")
    client.post(f"/api/v1/notifications/channels/{in_app_id}/test")

    external = NotificationChannel(
        name="qa-inbox-webhook",
        channel_type="webhook",
        enabled=True,
        status="enabled",
    )
    db_session.add(external)
    db_session.flush()
    db_session.add(
        NotificationOutbox(
            event_key="qa:external:1",
            source_type="system",
            source_id=None,
            event_type="external_only",
            severity="info",
            payload_json=json.dumps({"title": "external", "body": "hidden"}),
            channel_id=external.id,
            status="pending",
        )
    )
    db_session.commit()

    items = client.get("/api/v1/notifications/inbox").json()
    assert len(items) == 1
    assert items[0]["channel_id"] == in_app_id

    mark_all = client.put("/api/v1/notifications/inbox/read-all")
    assert mark_all.status_code == 200, mark_all.text
    assert mark_all.json()["count"] == 1
    assert client.get("/api/v1/notifications/inbox").json()[0]["read"] is True

    view_all = client.post("/api/v1/notifications/inbox/view-all")
    assert view_all.status_code == 200
    assert view_all.json()["total"] == 1


def test_empty_inbox_returns_empty_list_without_seed_data(client):
    response = client.get("/api/v1/notifications/inbox")
    assert response.status_code == 200
    assert response.json() == []


def test_full_message_chain_event_policy_dispatcher_inbox(client, db_session):
    """渠道配置 -> 推送策略 -> 业务事件 -> dispatcher -> 右侧 inbox。"""
    channel_id = _make_enabled_in_app_channel(client, "qa-full-chain-in-app")
    policy_response = client.post(
        "/api/v1/notifications/policies",
        json={
            "name": "qa-full-chain-policy",
            "enabled": True,
            "source_types": ["system"],
            "min_severity": "info",
            "scope_type": "all",
            "delivery_mode": "instant",
            "channel_ids": [channel_id],
        },
    )
    assert policy_response.status_code == 200, policy_response.text

    created = emit_event(
        db_session,
        source_type="system",
        source_id=901,
        event_type="full_chain_probe",
        severity="warn",
        title="全链路验证消息",
        body="这条消息来自启用的站内消息渠道。",
    )
    assert len(created) == 1
    assert created[0].status == "pending"
    # Business emitters enqueue within the caller's transaction; the
    # background dispatcher observes the event after that transaction commits.
    db_session.commit()

    processed = Dispatcher().run_once()
    assert processed == 1

    inbox = client.get("/api/v1/notifications/inbox").json()
    assert len(inbox) == 1
    item = inbox[0]
    assert item["title"] == "全链路验证消息"
    assert item["description"] == "这条消息来自启用的站内消息渠道。"
    assert item["channel_id"] == channel_id
    assert item["status"] == "sent"
    assert item["read"] is False

    deliveries = client.get("/api/v1/notifications/deliveries").json()
    assert any(
        row["outbox_id"] == item["id"] and row["status"] == "success"
        for row in deliveries
    )

    marked = client.put(f"/api/v1/notifications/inbox/{item['id']}/read")
    assert marked.status_code == 200, marked.text
    assert client.get("/api/v1/notifications/inbox").json()[0]["read"] is True
