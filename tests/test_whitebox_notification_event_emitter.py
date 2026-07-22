"""白盒测试 - WP-MSG.7 业务事件接入。

覆盖：
1.  emit_alert_event 创建 Outbox
2.  emit_task_complete 成功 → severity=info
3.  emit_task_complete 失败 → severity=error
4.  emit_trade_executed 创建 Outbox，payload 含 symbol/action/quantity/price
5.  emit_data_expired → severity=warn
6.  emit_discovery_new 创建 Outbox，payload 含 symbol/score
7.  emit_signal_matched 创建 Outbox
8.  emit_auto_trade_blocked → severity=warn
9.  emit_drawdown_warning 严重（≥1.5 倍阈值）→ severity=critical
10. emit_drawdown_warning 普通 → severity=error
11. 无匹配策略时不创建 Outbox
12. AlertEvent 不被修改（data_json 不含第三方发送结果）

project_memory 硬约束：
- 现有 AlertEvent 继续作为正式告警事实，不把每个第三方发送结果塞进 data_json
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.alert import AlertEvent, AlertRule
from app.models.notification import (
    NotificationChannel,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
)
from app.services.notifications.event_emitter import (
    emit_alert_event,
    emit_auto_trade_blocked,
    emit_data_expired,
    emit_discovery_new,
    emit_drawdown_warning,
    emit_signal_matched,
    emit_task_complete,
    emit_trade_executed,
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


def _list_outboxes(db_session) -> list[NotificationOutbox]:
    return list(
        db_session.execute(select(NotificationOutbox)).scalars().all()
    )


# ----------------------------------------------------------------------------
# 1. emit_alert_event 创建 Outbox
# ----------------------------------------------------------------------------


def test_emit_alert_event_creates_outbox(db_session):
    """【WP-MSG.7】emit_alert_event 匹配策略后创建 Outbox。"""
    in_app = _make_channel(db_session, name="ia-alert")
    policy = _make_policy(
        db_session,
        name="p-alert",
        source_types=["system"],
        min_severity="info",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_alert_event(
        db_session,
        alert_event_id=1001,
        alert_type="score_drop",
        severity="warn",
        title="评分跌破阈值",
        body="600000 评分 35 低于阈值 40",
        symbol_id=42,
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.source_type == "system"
    assert outbox.source_id == 1001
    assert outbox.event_type == "score_drop"
    assert outbox.severity == "warn"
    assert outbox.channel_id == in_app.id
    assert outbox.policy_id == policy.id
    payload = json.loads(outbox.payload_json)
    assert payload["title"] == "评分跌破阈值"
    assert "600000" in payload["body"]


# ----------------------------------------------------------------------------
# 2. emit_task_complete 成功 → severity=info
# ----------------------------------------------------------------------------


def test_emit_task_complete_success_severity_info(db_session):
    """【WP-MSG.7】emit_task_complete(success=True) → severity=info。"""
    in_app = _make_channel(db_session, name="ia-task-ok")
    policy = _make_policy(
        db_session,
        name="p-task-ok",
        source_types=["system"],
        min_severity="info",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_task_complete(
        db_session,
        task_id=2002,
        task_name="discovery_mining",
        success=True,
        duration_seconds=12.5,
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.severity == "info"
    assert outbox.event_type == "task_complete"
    assert outbox.source_type == "system"
    assert outbox.source_id == 2002
    payload = json.loads(outbox.payload_json)
    assert "discovery_mining" in payload["title"]
    assert "成功" in payload["body"]
    assert "12.5" in payload["body"]


# ----------------------------------------------------------------------------
# 3. emit_task_complete 失败 → severity=error
# ----------------------------------------------------------------------------


def test_emit_task_complete_failure_severity_error(db_session):
    """【WP-MSG.7】emit_task_complete(success=False) → severity=error。"""
    in_app = _make_channel(db_session, name="ia-task-fail")
    policy = _make_policy(
        db_session,
        name="p-task-fail",
        source_types=["system"],
        min_severity="error",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_task_complete(
        db_session,
        task_id=2003,
        task_name="macro_update",
        success=False,
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.severity == "error"
    assert outbox.event_type == "task_complete"
    payload = json.loads(outbox.payload_json)
    assert "失败" in payload["body"]


# ----------------------------------------------------------------------------
# 4. emit_trade_executed 创建 Outbox，payload 含 symbol/action/quantity/price
# ----------------------------------------------------------------------------


def test_emit_trade_executed_creates_outbox(db_session):
    """【WP-MSG.7】emit_trade_executed 创建 Outbox，payload 含交易要素。"""
    in_app = _make_channel(db_session, name="ia-trade")
    policy = _make_policy(
        db_session,
        name="p-trade",
        source_types=["portfolio"],
        min_severity="info",
        scope_type="portfolio",
        scope_ids=[500],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_trade_executed(
        db_session,
        trade_id=3003,
        portfolio_id=500,
        symbol_id=42,
        symbol="600000",
        action="buy",
        quantity=100.0,
        price=10.5,
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.source_type == "portfolio"
    assert outbox.event_type == "trade_executed"
    assert outbox.severity == "info"
    # scope 仅用于策略匹配，不存储在 outbox 上
    payload = json.loads(outbox.payload_json)
    assert "600000" in payload["title"]
    assert "buy" in payload["body"]
    assert "100.0" in payload["body"]
    assert "10.5" in payload["body"]


# ----------------------------------------------------------------------------
# 5. emit_data_expired → severity=warn
# ----------------------------------------------------------------------------


def test_emit_data_expired_severity_warn(db_session):
    """【WP-MSG.7】emit_data_expired → severity=warn。"""
    in_app = _make_channel(db_session, name="ia-expired")
    policy = _make_policy(
        db_session,
        name="p-expired",
        source_types=["data"],
        min_severity="warn",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_data_expired(
        db_session,
        data_type="daily_bar",
        symbol_id=42,
        expired_at="2026-07-21T10:00:00",
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.severity == "warn"
    assert outbox.event_type == "data_expired"
    assert outbox.source_type == "data"
    payload = json.loads(outbox.payload_json)
    assert "daily_bar" in payload["title"]
    assert "2026-07-21" in payload["body"]


# ----------------------------------------------------------------------------
# 6. emit_discovery_new 创建 Outbox，payload 含 symbol/score
# ----------------------------------------------------------------------------


def test_emit_discovery_new_creates_outbox(db_session):
    """【WP-MSG.7】emit_discovery_new 创建 Outbox，payload 含 symbol/score。"""
    in_app = _make_channel(db_session, name="ia-discovery")
    policy = _make_policy(
        db_session,
        name="p-discovery",
        source_types=["opportunity"],
        min_severity="info",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_discovery_new(
        db_session,
        candidate_id=4004,
        symbol_id=42,
        symbol="000001",
        score=85.5,
        reason="trend+momentum",
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.source_type == "opportunity"
    assert outbox.event_type == "discovery_new"
    assert outbox.source_id == 4004
    payload = json.loads(outbox.payload_json)
    assert "000001" in payload["title"]
    # body 中应包含 score（85.50）
    assert "85.50" in payload["body"]
    assert "trend+momentum" in payload["body"]


# ----------------------------------------------------------------------------
# 7. emit_signal_matched 创建 Outbox
# ----------------------------------------------------------------------------


def test_emit_signal_matched_creates_outbox(db_session):
    """【WP-MSG.7】emit_signal_matched 创建 Outbox。"""
    in_app = _make_channel(db_session, name="ia-signal")
    policy = _make_policy(
        db_session,
        name="p-signal",
        source_types=["opportunity"],
        min_severity="info",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_signal_matched(
        db_session,
        signal_id=5005,
        symbol_id=42,
        symbol="002594",
        signal_name="金叉",
        portfolio_id=600,
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.source_type == "opportunity"
    assert outbox.event_type == "signal_matched"
    assert outbox.source_id == 5005
    payload = json.loads(outbox.payload_json)
    assert "002594" in payload["title"]
    assert "金叉" in payload["title"]


# ----------------------------------------------------------------------------
# 8. emit_auto_trade_blocked → severity=warn
# ----------------------------------------------------------------------------


def test_emit_auto_trade_blocked_severity_warn(db_session):
    """【WP-MSG.7】emit_auto_trade_blocked → severity=warn。"""
    in_app = _make_channel(db_session, name="ia-block")
    policy = _make_policy(
        db_session,
        name="p-block",
        source_types=["portfolio"],
        min_severity="warn",
        scope_type="portfolio",
        scope_ids=[700],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_auto_trade_blocked(
        db_session,
        portfolio_id=700,
        symbol_id=42,
        symbol="600519",
        reason="单标的仓位上限",
        rule_name="max_position_pct",
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.severity == "warn"
    assert outbox.event_type == "auto_trade_blocked"
    assert outbox.source_type == "portfolio"
    payload = json.loads(outbox.payload_json)
    assert "600519" in payload["title"]
    assert "单标的仓位上限" in payload["body"]
    assert "max_position_pct" in payload["body"]


# ----------------------------------------------------------------------------
# 9. emit_drawdown_warning 严重（≥1.5 倍阈值）→ severity=critical
# ----------------------------------------------------------------------------


def test_emit_drawdown_warning_critical(db_session):
    """【WP-MSG.7】drawdown_pct=30, threshold=20（1.5 倍）→ severity=critical。"""
    in_app = _make_channel(db_session, name="ia-dd-crit")
    policy = _make_policy(
        db_session,
        name="p-dd-crit",
        source_types=["portfolio"],
        min_severity="error",
        scope_type="portfolio",
        scope_ids=[800],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_drawdown_warning(
        db_session,
        portfolio_id=800,
        drawdown_pct=30.0,
        threshold_pct=20.0,
        current_value=700000.0,
    )
    assert len(created) == 1
    outbox = created[0]
    assert outbox.severity == "critical"
    assert outbox.event_type == "drawdown_warning"
    payload = json.loads(outbox.payload_json)
    assert "30.00" in payload["body"]
    assert "20.00" in payload["body"]


# ----------------------------------------------------------------------------
# 10. emit_drawdown_warning 普通 → severity=error
# ----------------------------------------------------------------------------


def test_emit_drawdown_warning_error(db_session):
    """【WP-MSG.7】drawdown_pct=22, threshold=20 → severity=error（未达 1.5 倍）。"""
    in_app = _make_channel(db_session, name="ia-dd-err")
    policy = _make_policy(
        db_session,
        name="p-dd-err",
        source_types=["portfolio"],
        min_severity="error",
        scope_type="portfolio",
        scope_ids=[800],
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    created = emit_drawdown_warning(
        db_session,
        portfolio_id=800,
        drawdown_pct=22.0,
        threshold_pct=20.0,
    )
    assert len(created) == 1
    outbox = created[0]
    # 22 < 30（20*1.5）→ error
    assert outbox.severity == "error"
    payload = json.loads(outbox.payload_json)
    assert "22.00" in payload["body"]


# ----------------------------------------------------------------------------
# 11. 无匹配策略时不创建 Outbox
# ----------------------------------------------------------------------------


def test_emit_alert_event_no_matching_policy(db_session):
    """【WP-MSG.7】无匹配策略时返回空列表，不创建 Outbox。"""
    # 创建一个 channel，但不创建任何 policy
    _make_channel(db_session, name="ia-no-policy")

    outboxes_before = _list_outboxes(db_session)
    created = emit_alert_event(
        db_session,
        alert_event_id=9999,
        alert_type="score_drop",
        severity="warn",
        title="无策略匹配",
        body="不应创建 Outbox",
    )
    assert created == []
    outboxes_after = _list_outboxes(db_session)
    assert len(outboxes_after) == len(outboxes_before)


# ----------------------------------------------------------------------------
# 12. AlertEvent 不被修改（data_json 不含第三方发送结果）
# ----------------------------------------------------------------------------


def test_alert_event_not_modified_by_emit(db_session):
    """【WP-MSG.7】AlertEvent 作为正式告警事实，data_json 不被 emit_alert_event 修改。

    场景：
    - 先创建一条 AlertEvent，data_json 中含明确的业务字段
    - 调用 emit_alert_event 触发通知
    - 断言 AlertEvent 的 data_json 仍然只含原始业务字段，
      不含任何第三方发送结果（如 wxpusher_response、delivery_status 等）
    """
    # 准备：创建 AlertRule + AlertEvent（data_json 只含业务字段）
    rule = AlertRule(
        name="rule-test",
        alert_type="score_drop",
        enabled=1,
        severity="warn",
        config_json='{"threshold": 40}',
        cooldown_minutes=60,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)

    original_data = {"priority_score": 35.5, "threshold": 40, "trade_date": "2026-07-20"}
    event = AlertEvent(
        rule_id=rule.id,
        alert_type="score_drop",
        severity="warn",
        title="评分跌破阈值",
        message="600000 评分 35.5 低于阈值 40",
        symbol_id=42,
        data_json=json.dumps(original_data, ensure_ascii=False),
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    original_data_json = event.data_json

    # 准备一个匹配的 policy + channel，使 emit_alert_event 真正会创建 Outbox
    in_app = _make_channel(db_session, name="ia-ae-preserve")
    policy = _make_policy(
        db_session,
        name="p-ae-preserve",
        source_types=["system"],
        min_severity="info",
    )
    _link(db_session, policy_id=policy.id, channel_id=in_app.id)

    # 调用 emit_alert_event
    created = emit_alert_event(
        db_session,
        alert_event_id=event.id,
        alert_type="score_drop",
        severity="warn",
        title="评分跌破阈值",
        body="600000 评分 35.5 低于阈值 40",
        symbol_id=42,
    )
    assert len(created) == 1

    # 断言 AlertEvent 字段未被修改
    db_session.refresh(event)
    assert event.data_json == original_data_json
    parsed = json.loads(event.data_json)
    # 原始业务字段仍在
    assert parsed["priority_score"] == 35.5
    assert parsed["threshold"] == 40
    # 不应出现任何第三方发送结果字段
    forbidden_keys = {
        "wxpusher_response", "dingtalk_response", "delivery_status",
        "sent_at", "channel_response", "third_party_result",
    }
    assert not (forbidden_keys & set(parsed.keys())), (
        f"AlertEvent.data_json 不应包含第三方发送结果字段：{forbidden_keys & set(parsed.keys())}"
    )

    # Outbox 中可以含发送状态，但 AlertEvent 中不应有
    outbox = created[0]
    outbox_payload = json.loads(outbox.payload_json)
    assert "title" in outbox_payload
    assert "body" in outbox_payload
