"""白盒测试 - 告警中心 API (P1-3)。

覆盖 app/api/routes/alerts.py 路由层逻辑：
1. GET /alerts/rules 列表返回字段完整
2. POST /alerts/rules 创建告警规则
3. PATCH /alerts/rules/{id} 更新状态
4. DELETE /alerts/rules/{id} 删除
5. 边界：不存在的 id 返回 404
6. PATCH enabled 字段转 int 存储
7. config 字段 JSON 序列化/反序列化
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.routes.alerts import (
    create_alert_rule,
    delete_alert_rule,
    list_alert_rules,
    list_alert_events,
    trigger_evaluation,
    list_active_alerts,
    ack_alert,
    ack_all_alerts,
    update_alert_rule,
)
from app.models.alert import AlertEvent, AlertRule
from app.schemas.alert import AlertRuleCreate, AlertRuleUpdate

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. GET /alerts/rules 列表
# ============================================================================

def test_list_alert_rules_empty_returns_empty_list(db_session):
    """【P1-3 API 测试】无规则时 list_alert_rules 返回空列表。"""
    result = list_alert_rules(db=db_session)
    assert result == []


def test_list_alert_rules_returns_all_fields(db_session):
    """【P1-3 API 测试】list_alert_rules 返回的规则含全部必填字段。

    防止前端因缺字段报错：id/name/alert_type/enabled/severity/
    config_json/last_triggered_at/cooldown_minutes/created_at/updated_at。
    """
    db_session.add(AlertRule(
        name="评分骤降告警",
        alert_type="score_drop",
        enabled=1,
        severity="warn",
        config_json='{"threshold": 40}',
        cooldown_minutes=60,
    ))
    db_session.commit()

    result = list_alert_rules(db=db_session)
    assert len(result) == 1
    rule = result[0]
    required_fields = {
        "id", "name", "alert_type", "enabled", "severity",
        "config_json", "last_triggered_at", "cooldown_minutes",
        "created_at", "updated_at",
    }
    for field in required_fields:
        assert hasattr(rule, field), f"AlertRule 缺少字段 {field}"


# ============================================================================
# 2. POST /alerts/rules 创建
# ============================================================================

def test_create_alert_rule_success(db_session):
    """【P1-3 API 测试】create_alert_rule 成功创建规则并返回 AlertRule。"""
    payload = AlertRuleCreate(
        name="数据陈旧告警",
        alert_type="data_stale",
        enabled=True,
        severity="error",
        config={"stale_days": 7, "symbol_ids": [1, 2, 3]},
        cooldown_minutes=30,
    )

    result = create_alert_rule(payload, db=db_session)
    assert result.id is not None
    assert result.name == "数据陈旧告警"
    assert result.alert_type == "data_stale"
    assert result.enabled == 1  # bool → int 存储
    assert result.severity == "error"
    assert result.cooldown_minutes == 30
    # config_json 序列化
    assert result.config_json is not None
    parsed = json.loads(result.config_json)
    assert parsed["stale_days"] == 7
    assert parsed["symbol_ids"] == [1, 2, 3]


def test_create_alert_rule_enabled_false_stored_as_zero(db_session):
    """【P1-3 API 测试】enabled=False 应以 int 存储，值为 0。

    防止前端 enabled 字段类型不一致（bool vs int）导致 Switch 显示错误。
    """
    payload = AlertRuleCreate(
        name="禁用规则",
        alert_type="task_failed",
        enabled=False,
    )
    result = create_alert_rule(payload, db=db_session)
    assert result.enabled == 0


# ============================================================================
# 3. PATCH /alerts/rules/{id} 更新
# ============================================================================

def test_update_alert_rule_success(db_session):
    """【P1-3 API 测试】update_alert_rule 更新 severity 和 config。"""
    rule = AlertRule(name="原规则", alert_type="score_drop", enabled=1, severity="info")
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)

    payload = AlertRuleUpdate(
        severity="error",
        config={"threshold": 30},
    )
    result = update_alert_rule(rule.id, payload, db=db_session)
    assert result.severity == "error"
    parsed = json.loads(result.config_json)
    assert parsed["threshold"] == 30


def test_update_alert_rule_enabled_converts_to_int(db_session):
    """【P1-3 API 测试】PATCH enabled=True 应转为 int 1 存储。

    前端 Switch onChange 传 bool，但 DB 字段是 Integer。
    """
    rule = AlertRule(name="原规则", alert_type="score_drop", enabled=0)
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)

    payload = AlertRuleUpdate(enabled=True)
    result = update_alert_rule(rule.id, payload, db=db_session)
    assert result.enabled == 1


def test_update_alert_rule_not_found_raises_404(db_session):
    """【P1-3 API 测试】更新不存在的规则 id 应抛 HTTPException 404。"""
    payload = AlertRuleUpdate(severity="warn")
    with pytest.raises(HTTPException) as exc:
        update_alert_rule(99999, payload, db=db_session)
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


# ============================================================================
# 4. DELETE /alerts/rules/{id} 删除
# ============================================================================

def test_delete_alert_rule_success(db_session):
    """【P1-3 API 测试】delete_alert_rule 删除成功返回 {"deleted": id}。"""
    rule = AlertRule(name="待删除", alert_type="score_drop", enabled=1)
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    rule_id = rule.id

    result = delete_alert_rule(rule_id, db=db_session)
    assert result == {"deleted": rule_id}
    # 验证已删除
    assert db_session.get(AlertRule, rule_id) is None


def test_delete_alert_rule_not_found_raises_404(db_session):
    """【P1-3 API 测试】删除不存在的规则 id 应抛 HTTPException 404。"""
    with pytest.raises(HTTPException) as exc:
        delete_alert_rule(99999, db=db_session)
    assert exc.value.status_code == 404


# ============================================================================
# 5. GET /alerts/events 事件列表
# ============================================================================

def test_list_alert_events_returns_unacknowledged_count(db_session):
    """【P1-3 API 测试】list_alert_events 返回 events 列表 + unacknowledged_count。

    防止前端 unacknowledged_count 字段缺失导致红点不显示。
    """
    db_session.add(AlertEvent(
        rule_id=1,
        alert_type="score_drop",
        severity="warn",
        title="评分骤降",
        message="XXX 评分从 80 降到 50",
        acknowledged=0,
        data_json='{"old": 80, "new": 50}',
    ))
    db_session.add(AlertEvent(
        rule_id=1,
        alert_type="score_drop",
        severity="error",
        title="已确认告警",
        message="YYY 数据陈旧",
        acknowledged=1,
    ))
    db_session.commit()

    result = list_alert_events(limit=50, include_acknowledged=True, db=db_session)
    assert "events" in result
    assert "unacknowledged_count" in result
    assert len(result["events"]) == 2
    assert result["unacknowledged_count"] == 1  # 仅 1 条未确认


def test_list_alert_events_excludes_acknowledged_by_default(db_session):
    """【P1-3 API 测试】include_acknowledged=False（默认）应排除已确认事件。"""
    db_session.add(AlertEvent(
        rule_id=1,
        alert_type="data_stale",
        severity="warn",
        title="未确认",
        message="...",
        acknowledged=0,
    ))
    db_session.add(AlertEvent(
        rule_id=1,
        alert_type="data_stale",
        severity="warn",
        title="已确认",
        message="...",
        acknowledged=1,
    ))
    db_session.commit()

    result = list_alert_events(limit=50, include_acknowledged=False, db=db_session)
    assert len(result["events"]) == 1
    assert result["events"][0]["title"] == "未确认"


def test_list_alert_events_data_json_parse_failure_falls_back_empty(db_session):
    """【P1-3 API 测试】data_json 解析失败时应静默降级为 {}，不抛异常。"""
    db_session.add(AlertEvent(
        rule_id=1,
        alert_type="task_failed",
        severity="error",
        title="任务失败",
        message="...",
        acknowledged=0,
        data_json="{invalid json}",  # 故意写入非法 JSON
    ))
    db_session.commit()

    result = list_alert_events(limit=50, include_acknowledged=True, db=db_session)
    assert len(result["events"]) == 1
    assert result["events"][0]["data"] == {}  # 降级为空字典


# ============================================================================
# 6. POST /alerts/evaluate 触发评估（mock 服务层）
# ============================================================================

def test_trigger_evaluation_returns_evaluated_and_event_count():
    """【P1-3 API 测试】trigger_evaluation 返回 evaluated=True + new_events 计数。

    路由不依赖 db，直接调用服务层。mock 服务层返回 2 个事件。
    """
    with patch("app.api.routes.alerts.evaluate_all_rules", return_value=[{"id": 1}, {"id": 2}]):
        result = trigger_evaluation()
    assert result["evaluated"] is True
    assert result["new_events"] == 2
    assert len(result["events"]) == 2


# ============================================================================
# 7. POST /alerts/acknowledge/{event_id}（mock 服务层）
# ============================================================================

def test_ack_alert_success_returns_acknowledged_id():
    """【P1-3 API 测试】ack_alert 服务层返回 True 时返回 {"acknowledged": event_id}。"""
    with patch("app.api.routes.alerts.acknowledge_alert", return_value=True):
        result = ack_alert(42)
    assert result == {"acknowledged": 42}


def test_ack_alert_not_found_raises_404():
    """【P1-3 API 测试】ack_alert 服务层返回 False 时抛 HTTPException 404。"""
    with patch("app.api.routes.alerts.acknowledge_alert", return_value=False):
        with pytest.raises(HTTPException) as exc:
            ack_alert(99999)
    assert exc.value.status_code == 404


# ============================================================================
# 8. POST /alerts/acknowledge-all（mock 服务层）
# ============================================================================

def test_ack_all_alerts_returns_count():
    """【P1-3 API 测试】ack_all_alerts 返回 {"acknowledged_count": N}。"""
    with patch("app.api.routes.alerts.acknowledge_all_alerts", return_value=5):
        result = ack_all_alerts()
    assert result == {"acknowledged_count": 5}


def test_list_active_alerts_returns_events_and_count():
    """【P1-3 API 测试】list_active_alerts 返回 events + count 字段完整。"""
    fake_events = [{"id": 1, "title": "告警1"}, {"id": 2, "title": "告警2"}]
    with patch("app.api.routes.alerts.get_active_alerts", return_value=fake_events):
        result = list_active_alerts(limit=50)
    assert "events" in result
    assert "count" in result
    assert result["count"] == 2
