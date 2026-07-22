"""白盒测试 - WP-MSG.4 消息来源与推送策略 + 渠道状态机。

覆盖：
1.  MESSAGE_SOURCES 完整性（6 个分组）
2.  get_source / get_event_type
3.  can_transition 状态流转合法性
4.  is_selectable_for_policy 渠道可选性
5.  get_selection_hint 不可选提示
6.  match_policies 基本匹配
7.  match_policies source_type 不匹配
8.  match_policies severity 不匹配
9.  match_policies scope 匹配
10. match_policies 渠道不可选
11. emit_event 基本流程
12. emit_event 多渠道
13. emit_event 无匹配策略
14. emit_event 模板渲染
15. emit_event 防重
"""
from __future__ import annotations

import json

import pytest

from app.models.notification import (
    NotificationChannel,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
    NotificationTemplate,
)
from app.services.notifications.channel_state import (
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_PENDING_TEST,
    STATE_TEST_FAILED,
    STATE_TEST_SUCCESS,
    STATE_UNCONFIGURED,
    can_transition,
    get_selection_hint,
    is_selectable_for_policy,
)
from app.services.notifications.policies import emit_event, match_policies
from app.services.notifications.sources import (
    MESSAGE_SOURCES,
    get_event_type,
    get_source,
    list_sources,
)


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
    template_id: int | None = None,
) -> NotificationPolicy:
    """创建并提交一个 policy。"""
    policy = NotificationPolicy(
        name=name,
        enabled=enabled,
        source_types_json=json.dumps(source_types or [], ensure_ascii=False),
        min_severity=min_severity,
        scope_type=scope_type,
        scope_ids_json=json.dumps(scope_ids or [], ensure_ascii=False) if scope_ids else None,
        delivery_mode="instant",
        template_id=template_id,
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


# ----------------------------------------------------------------------------
# 1. MESSAGE_SOURCES 完整性
# ----------------------------------------------------------------------------


def test_message_sources_has_six_groups():
    """【WP-MSG.4】MESSAGE_SOURCES 包含 6 个分组。"""
    expected = {"system", "data", "factor", "opportunity", "portfolio", "review"}
    assert set(MESSAGE_SOURCES.keys()) == expected


def test_message_sources_each_group_has_events():
    """【WP-MSG.4】每个分组有非空 events 列表。"""
    for key, group in MESSAGE_SOURCES.items():
        assert "name" in group, f"{key} 缺少 name"
        assert "events" in group, f"{key} 缺少 events"
        assert isinstance(group["events"], list) and len(group["events"]) > 0
        for ev in group["events"]:
            assert "type" in ev and "name" in ev and "default_severity" in ev


def test_list_sources_returns_dict():
    """【WP-MSG.4】list_sources 返回 MESSAGE_SOURCES。"""
    assert list_sources() is MESSAGE_SOURCES


# ----------------------------------------------------------------------------
# 2. get_source / get_event_type
# ----------------------------------------------------------------------------


def test_get_source_returns_group():
    """【WP-MSG.4】get_source 返回存在的分组。"""
    src = get_source("opportunity")
    assert src is not None
    assert src["name"] == "机会与观察"


def test_get_source_returns_none_for_unknown():
    """【WP-MSG.4】get_source 对未知类型返回 None。"""
    assert get_source("unknown") is None


def test_get_event_type_returns_event():
    """【WP-MSG.4】get_event_type 返回存在的事件。"""
    ev = get_event_type("opportunity", "discovery_new")
    assert ev is not None
    assert ev["default_severity"] == "info"


def test_get_event_type_returns_none_for_unknown_event():
    """【WP-MSG.4】get_event_type 对未知事件返回 None。"""
    assert get_event_type("opportunity", "unknown") is None


def test_get_event_type_returns_none_for_unknown_source():
    """【WP-MSG.4】get_event_type 对未知来源返回 None。"""
    assert get_event_type("unknown", "any") is None


# ----------------------------------------------------------------------------
# 3. can_transition
# ----------------------------------------------------------------------------


def test_can_transition_legal():
    """【WP-MSG.4】合法的状态流转。"""
    assert can_transition(STATE_UNCONFIGURED, STATE_PENDING_TEST) is True
    assert can_transition(STATE_PENDING_TEST, STATE_TEST_SUCCESS) is True
    assert can_transition(STATE_PENDING_TEST, STATE_TEST_FAILED) is True
    assert can_transition(STATE_TEST_SUCCESS, STATE_ENABLED) is True
    assert can_transition(STATE_ENABLED, STATE_DISABLED) is True
    assert can_transition(STATE_DISABLED, STATE_ENABLED) is True
    assert can_transition(STATE_TEST_FAILED, STATE_PENDING_TEST) is True


def test_can_transition_illegal_skip_test():
    """【WP-MSG.4】未配置 → 已启用 不允许（必须先测试）。"""
    assert can_transition(STATE_UNCONFIGURED, STATE_ENABLED) is False


def test_can_transition_illegal_failed_to_enabled():
    """【WP-MSG.4】测试失败 → 已启用 不允许（必须重新测试）。"""
    assert can_transition(STATE_TEST_FAILED, STATE_ENABLED) is False


def test_can_transition_illegal_unconfigured_to_test_success():
    """【WP-MSG.4】未配置 → 测试成功 不允许。"""
    assert can_transition(STATE_UNCONFIGURED, STATE_TEST_SUCCESS) is False


# ----------------------------------------------------------------------------
# 4. is_selectable_for_policy
# ----------------------------------------------------------------------------


def test_is_selectable_in_app_enabled():
    """【WP-MSG.4】in_app 渠道启用 → 可选。"""
    ch = NotificationChannel(
        name="ia", channel_type="in_app", enabled=True, status="enabled"
    )
    assert is_selectable_for_policy(ch) is True


def test_is_selectable_in_app_not_enabled():
    """【WP-MSG.4】in_app 渠道未启用 → 不可选。"""
    ch = NotificationChannel(
        name="ia", channel_type="in_app", enabled=False, status="enabled"
    )
    assert is_selectable_for_policy(ch) is False


def test_is_selectable_wxpusher_enabled():
    """【WP-MSG.4】wxpusher 状态=enabled + enabled=True → 可选。"""
    ch = NotificationChannel(
        name="wx", channel_type="wxpusher", enabled=True, status="enabled"
    )
    assert is_selectable_for_policy(ch) is True


def test_is_selectable_wxpusher_test_success_not_enabled():
    """【WP-MSG.4】wxpusher 测试成功但未启用 → 不可选。"""
    ch = NotificationChannel(
        name="wx", channel_type="wxpusher", enabled=False, status="test_success"
    )
    assert is_selectable_for_policy(ch) is False


def test_is_selectable_wxpusher_test_failed():
    """【WP-MSG.4】wxpusher 测试失败 → 不可选。"""
    ch = NotificationChannel(
        name="wx", channel_type="wxpusher", enabled=True, status="test_failed"
    )
    assert is_selectable_for_policy(ch) is False


def test_is_selectable_wxpusher_unconfigured():
    """【WP-MSG.4】wxpusher 未配置 → 不可选。"""
    ch = NotificationChannel(
        name="wx", channel_type="wxpusher", enabled=False, status="unconfigured"
    )
    assert is_selectable_for_policy(ch) is False


def test_is_selectable_wxpusher_disabled():
    """【WP-MSG.4】wxpusher 已禁用 → 不可选。"""
    ch = NotificationChannel(
        name="wx", channel_type="wxpusher", enabled=False, status="disabled"
    )
    assert is_selectable_for_policy(ch) is False


# ----------------------------------------------------------------------------
# 5. get_selection_hint
# ----------------------------------------------------------------------------


def _channel(status: str, *, channel_type: str = "wxpusher", enabled: bool = False) -> NotificationChannel:
    return NotificationChannel(
        name="t", channel_type=channel_type, enabled=enabled, status=status
    )


def test_get_selection_hint_unconfigured():
    """【WP-MSG.4】未配置 → 提示去配置。"""
    hint = get_selection_hint(_channel("unconfigured"))
    assert "未配置" in hint
    assert "请先完成配置" in hint


def test_get_selection_hint_pending_test():
    """【WP-MSG.4】已配置待测试 → 提示立即测试。"""
    hint = get_selection_hint(_channel("pending_test"))
    assert "已配置待测试" in hint
    assert "请立即测试" in hint


def test_get_selection_hint_test_failed():
    """【WP-MSG.4】测试失败 → 提示重新测试。"""
    hint = get_selection_hint(_channel("test_failed"))
    assert "测试失败" in hint
    assert "重新测试" in hint


def test_get_selection_hint_test_success_not_enabled():
    """【WP-MSG.4】测试成功但未启用 → 提示启用。"""
    hint = get_selection_hint(_channel("test_success", enabled=False))
    assert "测试成功" in hint
    assert "请启用" in hint


def test_get_selection_hint_enabled():
    """【WP-MSG.4】已启用 → 无提示。"""
    hint = get_selection_hint(_channel("enabled", enabled=True))
    assert hint == ""


def test_get_selection_hint_disabled():
    """【WP-MSG.4】已禁用 → 提示启用。"""
    hint = get_selection_hint(_channel("disabled", enabled=False))
    assert "已禁用" in hint


def test_get_selection_hint_in_app_not_enabled():
    """【WP-MSG.4】in_app 未启用 → 提示启用。"""
    ch = NotificationChannel(
        name="ia", channel_type="in_app", enabled=False, status="enabled"
    )
    hint = get_selection_hint(ch)
    assert "启用" in hint


def test_get_selection_hint_in_app_enabled():
    """【WP-MSG.4】in_app 启用 → 无提示。"""
    ch = NotificationChannel(
        name="ia", channel_type="in_app", enabled=True, status="enabled"
    )
    assert get_selection_hint(ch) == ""


# ----------------------------------------------------------------------------
# 6. match_policies 基本匹配
# ----------------------------------------------------------------------------


def test_match_policies_basic(db_session):
    """【WP-MSG.4】匹配 1 个策略 + 2 个渠道。"""
    in_app = _make_channel(db_session, name="ia-basic", channel_type="in_app")
    wx = _make_channel(
        db_session, name="wx-basic", channel_type="wxpusher", enabled=True, status="enabled"
    )
    policy = _make_policy(
        db_session,
        name="p-basic",
        source_types=["opportunity"],
        min_severity="info",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)
    _link(db_session, policy_id=policy.id, channel_id=wx.id)

    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="info",
    )
    assert len(matched) == 1
    p, channels = matched[0]
    assert p.id == policy.id
    assert len(channels) == 2
    channel_ids = {ch.id for ch in channels}
    assert channel_ids == {in_app.id, wx.id}


# ----------------------------------------------------------------------------
# 7. match_policies source_type 不匹配
# ----------------------------------------------------------------------------


def test_match_policies_source_type_mismatch(db_session):
    """【WP-MSG.4】source_type 不在策略范围 → 无匹配。"""
    in_app = _make_channel(db_session, name="ia-st-mismatch")
    policy = _make_policy(
        db_session,
        name="p-st-mismatch",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="portfolio",
        event_type="trade_executed",
        severity="info",
    )
    assert matched == []


# ----------------------------------------------------------------------------
# 8. match_policies severity 不匹配
# ----------------------------------------------------------------------------


def test_match_policies_severity_below_minimum(db_session):
    """【WP-MSG.4】事件 severity 低于 min_severity → 无匹配。"""
    in_app = _make_channel(db_session, name="ia-sev")
    policy = _make_policy(
        db_session,
        name="p-sev",
        source_types=["opportunity"],
        min_severity="error",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="info",  # 低于 error
    )
    assert matched == []


def test_match_policies_severity_equal_or_above(db_session):
    """【WP-MSG.4】事件 severity 等于/高于 min_severity → 匹配。"""
    in_app = _make_channel(db_session, name="ia-sev-ok")
    policy = _make_policy(
        db_session,
        name="p-sev-ok",
        source_types=["opportunity"],
        min_severity="warn",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    # warn == warn → 匹配
    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="warn",
    )
    assert len(matched) == 1

    # critical > warn → 匹配
    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="critical",
    )
    assert len(matched) == 1


# ----------------------------------------------------------------------------
# 9. match_policies scope 匹配
# ----------------------------------------------------------------------------


def test_match_policies_scope_match(db_session):
    """【WP-MSG.4】scope_id 在策略范围 → 匹配。"""
    in_app = _make_channel(db_session, name="ia-scope-ok")
    policy = _make_policy(
        db_session,
        name="p-scope-ok",
        source_types=["portfolio"],
        scope_type="portfolio",
        scope_ids=[1, 2],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="portfolio",
        event_type="trade_executed",
        severity="info",
        scope_type="portfolio",
        scope_id=1,
    )
    assert len(matched) == 1


def test_match_policies_scope_id_not_in_list(db_session):
    """【WP-MSG.4】scope_id 不在策略范围 → 无匹配。"""
    in_app = _make_channel(db_session, name="ia-scope-miss")
    policy = _make_policy(
        db_session,
        name="p-scope-miss",
        source_types=["portfolio"],
        scope_type="portfolio",
        scope_ids=[1, 2],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="portfolio",
        event_type="trade_executed",
        severity="info",
        scope_type="portfolio",
        scope_id=3,  # 不在 [1, 2]
    )
    assert matched == []


def test_match_policies_scope_type_mismatch(db_session):
    """【WP-MSG.4】scope_type 不匹配 → 无匹配。"""
    in_app = _make_channel(db_session, name="ia-scope-type")
    policy = _make_policy(
        db_session,
        name="p-scope-type",
        source_types=["portfolio"],
        scope_type="portfolio",
        scope_ids=[1, 2],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="portfolio",
        event_type="trade_executed",
        severity="info",
        scope_type="watchlist",  # 与策略 portfolio 不匹配
        scope_id=1,
    )
    assert matched == []


def test_match_policies_scope_all_ignores_scope_id(db_session):
    """【WP-MSG.4】scope_type='all' 时不检查 scope_id。"""
    in_app = _make_channel(db_session, name="ia-scope-all")
    policy = _make_policy(
        db_session,
        name="p-scope-all",
        source_types=["opportunity"],
        scope_type="all",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="info",
        scope_type="portfolio",
        scope_id=999,
    )
    assert len(matched) == 1


# ----------------------------------------------------------------------------
# 10. match_policies 渠道不可选
# ----------------------------------------------------------------------------


def test_match_policies_unselectable_channel_excluded(db_session):
    """【WP-MSG.4】wxpusher 未配置 → 渠道被排除，匹配列表为空。"""
    wx_unconfig = _make_channel(
        db_session,
        name="wx-unconfig",
        channel_type="wxpusher",
        enabled=False,
        status="unconfigured",
    )
    policy = _make_policy(
        db_session,
        name="p-unselectable",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=wx_unconfig.id)

    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="info",
    )
    # 渠道不可选，应该过滤掉 → 整体策略不返回（无可用渠道）
    assert matched == []


def test_match_policies_disabled_policy_skipped(db_session):
    """【WP-MSG.4】禁用的策略不参与匹配。"""
    in_app = _make_channel(db_session, name="ia-disabled-policy")
    policy = _make_policy(
        db_session,
        name="p-disabled",
        enabled=False,
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    matched = match_policies(
        db_session,
        source_type="opportunity",
        event_type="discovery_new",
        severity="info",
    )
    assert matched == []


# ----------------------------------------------------------------------------
# 11. emit_event 基本流程
# ----------------------------------------------------------------------------


def test_emit_event_basic(db_session):
    """【WP-MSG.4】emit_event 创建 1 条 Outbox 记录。"""
    in_app = _make_channel(db_session, name="ia-emit-basic")
    policy = _make_policy(
        db_session,
        name="p-emit-basic",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=100,
        event_type="discovery_new",
        severity="info",
        title="新候选",
        body="600000 触发",
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.event_key == "opportunity:discovery_new:100"
    assert outbox.channel_id == in_app.id
    assert outbox.policy_id == policy.id
    assert outbox.source_type == "opportunity"
    assert outbox.source_id == 100
    assert outbox.event_type == "discovery_new"
    payload = json.loads(outbox.payload_json)
    assert payload["title"] == "新候选"
    assert payload["body"] == "600000 触发"


# ----------------------------------------------------------------------------
# 12. emit_event 多渠道
# ----------------------------------------------------------------------------


def test_emit_event_multi_channel(db_session):
    """【WP-MSG.4】emit_event 关联多个渠道 → 创建多条 Outbox。"""
    in_app = _make_channel(db_session, name="ia-multi")
    wx = _make_channel(
        db_session, name="wx-multi", channel_type="wxpusher", enabled=True, status="enabled"
    )
    policy = _make_policy(
        db_session,
        name="p-multi",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)
    _link(db_session, policy_id=policy.id, channel_id=wx.id)

    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=200,
        event_type="discovery_new",
        severity="info",
        title="t",
        body="b",
    )
    assert len(created) == 2
    channel_ids = {o.channel_id for o in created}
    assert channel_ids == {in_app.id, wx.id}


# ----------------------------------------------------------------------------
# 13. emit_event 无匹配策略
# ----------------------------------------------------------------------------


def test_emit_event_no_matching_policy(db_session):
    """【WP-MSG.4】无策略 → 返回空列表。"""
    in_app = _make_channel(db_session, name="ia-no-policy")
    # 不创建任何 policy

    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=300,
        event_type="discovery_new",
        severity="info",
        title="t",
        body="b",
    )
    assert created == []


# ----------------------------------------------------------------------------
# 14. emit_event 模板渲染
# ----------------------------------------------------------------------------


def test_emit_event_renders_template(db_session):
    """【WP-MSG.4】emit_event 使用模板渲染 title/body。"""
    template = NotificationTemplate(
        name="t-render",
        title_template="{symbol} 触发",
        body_template="价格 {price}",
        body_text_template="价格 {price}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)

    in_app = _make_channel(db_session, name="ia-tpl")
    policy = _make_policy(
        db_session,
        name="p-tpl",
        source_types=["opportunity"],
        template_id=template.id,
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=400,
        event_type="discovery_new",
        severity="info",
        title="原始标题",  # 应被模板覆盖
        body="原始正文",
        template_variables={"symbol": "000001", "price": "10.5"},
    )
    assert len(created) == 1
    outbox = created[0]
    payload = json.loads(outbox.payload_json)
    # 模板变量已渲染（注意 escape_value 会转义数字以外的特殊字符，但 000001 / 10.5 不受影响）
    assert "000001" in payload["title"]
    assert "10.5" in payload["body"]


def test_emit_event_template_with_special_chars_escaped(db_session):
    """【WP-MSG.4】模板变量中的特殊字符被转义，防止注入。"""
    template = NotificationTemplate(
        name="t-escape",
        title_template="{user}",
        body_template="{msg}",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)

    in_app = _make_channel(db_session, name="ia-escape")
    policy = _make_policy(
        db_session,
        name="p-escape",
        source_types=["opportunity"],
        template_id=template.id,
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_event(
        db_session,
        source_type="opportunity",
        source_id=401,
        event_type="discovery_new",
        severity="info",
        title="x",
        body="y",
        template_variables={"user": "admin", "msg": "try *injection* [link]"},
    )
    assert len(created) == 1
    outbox = created[0]
    payload = json.loads(outbox.payload_json)
    # 特殊字符被反斜杠转义
    assert "\\*" in payload["body"]
    assert "\\[" in payload["body"]


# ----------------------------------------------------------------------------
# 15. emit_event 防重
# ----------------------------------------------------------------------------


def test_emit_event_dedup_by_event_key(db_session):
    """【WP-MSG.4】相同 source_id 重复 emit_event → 第二次被防重。"""
    in_app = _make_channel(db_session, name="ia-dedup")
    policy = _make_policy(
        db_session,
        name="p-dedup",
        source_types=["opportunity"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    first = emit_event(
        db_session,
        source_type="opportunity",
        source_id=500,
        event_type="discovery_new",
        severity="info",
        title="第一次",
        body="b",
    )
    assert len(first) == 1

    # 相同 source_id 重复触发 → enqueue 防重返回 None
    second = emit_event(
        db_session,
        source_type="opportunity",
        source_id=500,
        event_type="discovery_new",
        severity="info",
        title="第二次",
        body="b2",
    )
    assert second == []

    # 验证 DB 中只有 1 条 Outbox
    from sqlalchemy import select

    outboxes = db_session.execute(
        select(NotificationOutbox).where(
            NotificationOutbox.source_id == 500
        )
    ).scalars().all()
    assert len(outboxes) == 1
    # 内容应是第一次的
    payload = json.loads(outboxes[0].payload_json)
    assert payload["title"] == "第一次"


def test_emit_event_no_source_id_not_dedup(db_session):
    """【WP-MSG.4】source_id 为空时 event_key 含时间戳，不防重。"""
    in_app = _make_channel(db_session, name="ia-no-src")
    policy = _make_policy(
        db_session,
        name="p-no-src",
        source_types=["system"],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    # 第一次：source_id=None，event_key 含时间戳
    first = emit_event(
        db_session,
        source_type="system",
        source_id=None,
        event_type="system_error",
        severity="error",
        title="e1",
        body="b1",
    )
    assert len(first) == 1

    # 第二次：source_id=None，时间戳不同 → 不防重
    second = emit_event(
        db_session,
        source_type="system",
        source_id=None,
        event_type="system_error",
        severity="error",
        title="e2",
        body="b2",
    )
    assert len(second) == 1
    assert first[0].id != second[0].id
