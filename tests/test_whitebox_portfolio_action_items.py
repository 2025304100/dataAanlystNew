"""白盒测试 - 组合行动项聚合（P0，纯函数，不碰 DB）。

守护：三源归并、数据新鲜度同族去重、对账 guided、G7 文本透传、未知状态兜底、ready 口径。
"""
from __future__ import annotations

import pytest

from app.services.auto_trade_readiness import (
    BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED,
    BLOCKER_MARKET_DATA_STALE,
    BLOCKER_NO_TRADEABLE_RANGE,
    BLOCKER_SCHEDULE_DISABLED,
    BLOCKER_STRATEGY_SNAPSHOT_REQUIRED,
    WARNING_SIGNAL_EMPTY_TODAY,
    WARNING_SOURCE_ENV_OVERRIDE,
    AutoTradeReadinessResult,
    _blocker,
    _warning,
)
from app.services.portfolio_action_items import aggregate_action_items

pytestmark = pytest.mark.whitebox

NOW = "2026-10-13T00:00:00+00:00"


def _readiness(**over) -> AutoTradeReadinessResult:
    base = dict(
        ready=True,
        enabled=True,
        account_ready=True,
        data_ready=True,
        source_ready=True,
        schedule_ready=True,
        position_count=1,
        candidate_count=1,
        eligible_buy_count=1,
        auto_member_count=1,
        blockers=[],
        warnings=[],
        rule_configured=True,
    )
    base.update(over)
    return AutoTradeReadinessResult(**base)


def _agg(**kw):
    kw.setdefault("readiness", _readiness())
    kw.setdefault("hg1_state", "READY")
    kw.setdefault("g7_blockers", [])
    kw.setdefault("generated_at", NOW)
    return aggregate_action_items(**kw)


def test_healthy_produces_no_items():
    out = _agg()
    assert out["items"] == []
    assert out["summary"] == {
        "blockers": 0, "warnings": 0, "info": 0, "ready": True,
        "waiting_for_signal": False, "execution_state": "ready",
    }


def test_data_staleness_merges_readiness_and_hg1_into_one_blocker():
    readiness = _readiness(
        ready=False,
        data_ready=False,
        blockers=[_blocker(BLOCKER_MARKET_DATA_STALE, "过期")],
    )
    out = _agg(readiness=readiness, hg1_state="DATA_INCOMPLETE_PAUSED")

    assert out["summary"]["blockers"] == 1
    item = out["items"][0]
    assert item["id"] == "data-freshness"
    assert item["severity"] == "blocker"
    assert set(item["sources"]) == {"readiness", "hg1_status"}
    # 合并后保留可操作性最强的 readiness 动作。
    assert item["primary_action"] == {
        "action_type": "redirect",
        "target": "settings:data-center",
    }


def test_score_stale_also_merges_into_same_family():
    readiness = _readiness(
        ready=False,
        data_ready=False,
        blockers=[_blocker(BLOCKER_MARKET_DATA_STALE, "过期")],
    )
    out = _agg(readiness=readiness, hg1_state="SCORE_STALE")
    assert out["summary"]["blockers"] == 1
    assert out["items"][0]["id"] == "data-freshness"


def test_reconciliation_blocked_is_guided_in_drawer():
    readiness = _readiness(ready=False)
    out = _agg(readiness=readiness, hg1_state="RECONCILIATION_BLOCKED")
    item = next(i for i in out["items"] if i["code"] == "RECONCILIATION_BLOCKED")
    assert item["severity"] == "blocker"
    assert item["primary_action"] == {
        "action_type": "guided",
        "target": "governance:reconciliation",
    }


def test_admin_paused_points_to_governance_subtab():
    out = _agg(hg1_state="ADMIN_PAUSED")
    item = out["items"][0]
    assert item["severity"] == "blocker"
    assert item["primary_action"] == {"action_type": "subtab", "target": "governance:state"}


def test_running_states_emit_no_hg1_item():
    out = _agg(hg1_state="RUNNING_AUTO_SIMULATION")
    assert out["items"] == []
    assert out["summary"]["execution_state"] == "running"
    assert out["summary"]["ready"] is True


def test_backtest_is_busy_instead_of_ready_for_concurrent_auto_trade():
    out = _agg(hg1_state="RUNNING_BACKTEST")
    assert out["summary"]["ready"] is False
    assert out["summary"]["blockers"] == 1
    assert out["summary"]["execution_state"] == "blocked"


def test_g7_text_blockers_are_info_passthrough_without_action():
    out = _agg(hg1_state="READY", g7_blockers=["存在 2 笔待处理模拟订单"])
    item = out["items"][0]
    assert item["source"] == "g7_ops"
    assert item["severity"] == "info"
    assert item["primary_action"] is None
    assert "待处理模拟订单" in item["detail"]


def test_today_no_signal_is_downgraded_to_info():
    readiness = _readiness(warnings=[_warning(WARNING_SIGNAL_EMPTY_TODAY, "无信号")])
    out = _agg(readiness=readiness)
    assert out["summary"]["blockers"] == 0
    assert out["summary"]["warnings"] == 0
    assert out["summary"]["info"] == 1
    assert out["summary"]["ready"] is True
    assert out["items"][0]["severity"] == "info"


def test_unknown_hg1_state_falls_back_safely():
    out = _agg(hg1_state="SOME_FUTURE_STATE")
    item = out["items"][0]
    assert item["severity"] == "blocker"
    assert item["primary_action"] is None
    # 主标题不裸抛英文枚举。
    assert item["code"] == "SOME_FUTURE_STATE"
    assert "SOME_FUTURE_STATE" not in item["title"]


def test_ready_flag_false_when_readiness_blocks():
    readiness = _readiness(
        ready=False,
        blockers=[_blocker(BLOCKER_MARKET_DATA_STALE, "过期")],
    )
    out = _agg(readiness=readiness, hg1_state="READY")
    assert out["summary"]["ready"] is False


def test_non_simulated_account_ready_tracks_hg1_state():
    # readiness=None（非模拟账户不跑就绪检查）时，READY 仍判为就绪。
    out = _agg(readiness=None, hg1_state="READY")
    assert out["summary"]["ready"] is True
    out2 = _agg(readiness=None, hg1_state="ADMIN_PAUSED")
    assert out2["summary"]["ready"] is False


def test_g7_structured_blockers_become_distinct_info_items():
    structured = [
        {
            "code": "G6_GATE_NOT_READY",
            "severity": "info",
            "title": "灰度准入条件未满足",
            "impact": "尚有未清条件",
            "detail": "组合状态为阻断",
            "fix_link": {"action_type": "none"},
        },
        {
            "code": "PENDING_SIM_ORDERS",
            "severity": "info",
            "title": "有待处理模拟订单",
            "impact": "存在 2 笔待处理模拟订单",
            "detail": "pending_order_count=2",
            "fix_link": {"action_type": "none"},
        },
    ]
    out = _agg(hg1_state="READY", g7_structured=structured)
    codes = {i["code"] for i in out["items"]}
    assert codes == {"G6_GATE_NOT_READY", "PENDING_SIM_ORDERS"}
    assert all(i["severity"] == "info" for i in out["items"])
    assert all(i["primary_action"] is None for i in out["items"])


def test_g7_structured_takes_precedence_over_text():
    # 同时给结构化与文本时，仅产出结构化项，不再合并成单条文本项。
    out = _agg(
        hg1_state="READY",
        g7_blockers=["旧文本"],
        g7_structured=[{"code": "ACTIVE_ALERTS", "severity": "info", "title": "有未确认告警",
                       "impact": "存在 1 条", "fix_link": {"action_type": "none"}}],
    )
    assert [i["code"] for i in out["items"]] == ["ACTIVE_ALERTS"]


def test_blockers_sort_before_warnings_before_info():
    readiness = _readiness(
        ready=False,
        warnings=[_warning(WARNING_SIGNAL_EMPTY_TODAY, "无信号")],
        blockers=[_blocker(BLOCKER_MARKET_DATA_STALE, "过期")],
    )
    out = _agg(readiness=readiness, hg1_state="ADMIN_PAUSED", g7_blockers=["x"])
    severities = [i["severity"] for i in out["items"]]
    assert severities == sorted(severities, key={"blocker": 0, "warning": 1, "info": 2}.get)


@pytest.mark.parametrize("state", ["ADMIN_PAUSED", "RECONCILIATION_BLOCKED", "PENDING_INITIAL_REVIEW", "INTERRUPTED"])
def test_hg1_block_is_never_hidden_by_healthy_readiness(state):
    out = _agg(hg1_state=state)
    assert out["summary"]["ready"] is False
    assert out["summary"]["blockers"] == 1


@pytest.mark.parametrize("dimension,state", [
    ("status_score", "SCORE_STALE"),
    ("status_data", "DATA_INCOMPLETE_PAUSED"),
    ("status_model", "MODEL_INACTIVE"),
    ("status_reconciliation", "RECONCILIATION_BLOCKED"),
])
def test_each_dimension_block_is_visible_even_when_hg1_is_ready(dimension, state):
    out = _agg(dimension_states={dimension: state})
    assert out["summary"]["ready"] is False
    assert out["summary"]["blockers"] == 1
    item = out["items"][0]
    assert item["code"] == state
    assert item["detail"] == f"{dimension}={state}"
    if state == "MODEL_INACTIVE":
        assert item["primary_action"] == {"action_type": "open-modal", "target": "model-binding-review"}


def test_legacy_and_dimension_reconciliation_are_one_guided_action():
    out = _agg(hg1_state="RECONCILIATION_BLOCKED", dimension_states={
        "status_reconciliation": "RECONCILIATION_BLOCKED",
        "status_model": "MODEL_INACTIVE",
    })
    assert out["summary"]["blockers"] == 2
    reconciliation = [item for item in out["items"] if item["code"] == "RECONCILIATION_BLOCKED"]
    assert len(reconciliation) == 1
    assert reconciliation[0]["primary_action"]["action_type"] == "guided"


def _waiting_readiness():
    return _readiness(
        ready=False, position_count=0, eligible_buy_count=0,
        blockers=[_blocker(BLOCKER_NO_TRADEABLE_RANGE, "今日无可执行买入计划")],
        warnings=[_warning(WARNING_SIGNAL_EMPTY_TODAY, "今日无买入信号")],
    )


def test_valid_candidate_range_without_a_signal_is_normal_waiting_not_executable():
    readiness = _waiting_readiness()
    out = _agg(readiness=readiness)
    assert out["summary"]["blockers"] == 0
    assert out["summary"]["execution_state"] == "waiting_signal"
    assert out["summary"]["waiting_for_signal"] is True
    assert out["summary"]["ready"] is False
    assert out["items"][0]["severity"] == "info"
    # The display must not alter the real executor's admission result.
    assert readiness.ready is False
    assert readiness.blockers[0].code == BLOCKER_NO_TRADEABLE_RANGE


def test_manual_diagnostic_can_wait_without_a_schedule_but_scheduled_view_is_blocked():
    readiness = _waiting_readiness()
    readiness.schedule_ready = False
    readiness.warnings.append(_warning(BLOCKER_SCHEDULE_DISABLED, "尚未创建调度"))
    manual = _agg(readiness=readiness, for_schedule=False)
    assert manual["summary"]["execution_state"] == "waiting_signal"
    scheduled = _agg(readiness=readiness, for_schedule=True)
    assert scheduled["summary"]["execution_state"] == "blocked"
    assert scheduled["summary"]["waiting_for_signal"] is False


@pytest.mark.parametrize("mode,auto_members,scope_count,waiting", [
    ("members_only", 0, 0, False),
    ("members_only", 1, 1, True),
    ("portfolio", 0, 1, True),
    ("portfolio", 0, 0, False),
    ("legacy_scan", 1, 1, False),
])
def test_waiting_requires_an_execution_scope_for_the_selected_source(mode, auto_members, scope_count, waiting):
    readiness = _waiting_readiness()
    readiness.source_mode = mode
    readiness.auto_member_count = auto_members
    readiness.details = {"data_scope_symbol_count": scope_count}
    out = _agg(readiness=readiness)
    assert out["summary"]["waiting_for_signal"] is waiting
    assert (out["summary"]["execution_state"] == "waiting_signal") is waiting


@pytest.mark.parametrize("extra", [
    {"execution_blockers": [_blocker(BLOCKER_STRATEGY_SNAPSHOT_REQUIRED, "缺策略快照")]},
    {"execution_blockers": [_blocker(BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED, "来源熔断")]},
    {"dimension_states": {"status_model": "MODEL_INACTIVE"}},
    {"hg1_state": "RECONCILIATION_BLOCKED"},
])
def test_missing_real_prerequisites_cannot_be_mislabeled_waiting(extra):
    out = _agg(readiness=_waiting_readiness(), **extra)
    assert out["summary"]["waiting_for_signal"] is False
    assert out["summary"]["execution_state"] == "blocked"
    assert out["summary"]["ready"] is False


def test_source_emergency_warning_and_actual_execution_block_merge():
    out = _agg(
        readiness=_readiness(warnings=[_warning(WARNING_SOURCE_ENV_OVERRIDE, "来源熔断")]),
        execution_blockers=[_blocker(BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED, "真实执行被阻断")],
    )
    assert len(out["items"]) == 1
    assert out["items"][0]["severity"] == "blocker"
    assert out["items"][0]["code"] == BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED
    assert out["checks"]["source_ready"] is False


def test_binding_review_pending_change_is_the_same_model_action_as_dimension_lock():
    review = {"requires_confirmation": True, "snapshot_id": "new-applied-snapshot"}
    out = _agg(
        dimension_states={"status_model": "MODEL_INACTIVE"},
        binding_review=review,
    )
    assert out["summary"]["blockers"] == 1
    assert out["summary"]["ready"] is False
    assert out["binding_review"] == review
    assert out["items"][0]["primary_action"] == {
        "action_type": "open-modal", "target": "model-binding-review",
    }


def test_invalid_existing_binding_blocks_configuration_without_inventing_acceptance_requirement():
    review = {"requires_confirmation": False, "snapshot_id": "retired-model-snapshot", "blockers": ["模型已退役"]}
    out = _agg(binding_review=review)
    assert out["summary"]["ready"] is False
    assert out["summary"]["blockers"] == 1
    assert out["items"][0]["code"] == "STRATEGY_BINDING_INVALID"
    assert out["items"][0]["primary_action"] == {"action_type": "subtab", "target": "strategy"}
    assert out["binding_review"]["requires_confirmation"] is False


def test_missing_snapshot_review_does_not_double_count_execution_snapshot_blocker():
    out = _agg(
        binding_review={"requires_confirmation": False, "snapshot_id": None, "blockers": ["尚未保存应用"]},
        execution_blockers=[_blocker(BLOCKER_STRATEGY_SNAPSHOT_REQUIRED, "缺应用快照")],
    )
    assert out["summary"]["blockers"] == 1
    assert out["items"][0]["code"] == BLOCKER_STRATEGY_SNAPSHOT_REQUIRED
