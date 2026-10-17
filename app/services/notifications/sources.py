"""消息来源定义（WP-MSG.4）。

消息来源分组：系统运行、数据与接口、因子与模型、机会与观察、组合与交易、绩效与复盘。
"""
from __future__ import annotations


# 消息来源分组与事件类型
MESSAGE_SOURCES: dict[str, dict] = {
    "system": {
        "name": "系统运行",
        "events": [
            {"type": "score_drop", "name": "评分下降", "default_severity": "warn"},
            {"type": "data_stale", "name": "数据过期告警", "default_severity": "warn"},
            {"type": "task_failed", "name": "任务失败告警", "default_severity": "error"},
            {"type": "system_error", "name": "系统错误", "default_severity": "error"},
            {"type": "system_warning", "name": "系统警告", "default_severity": "warn"},
            {"type": "task_complete", "name": "任务完成", "default_severity": "info"},
            {"type": "data_expired", "name": "数据过期", "default_severity": "warn"},
            {"type": "api_failure", "name": "接口失败", "default_severity": "error"},
        ],
    },
    "data": {
        "name": "数据与接口",
        "events": [
            {"type": "data_update", "name": "数据更新", "default_severity": "info"},
            {"type": "data_quality", "name": "数据质量", "default_severity": "warn"},
            {"type": "api_rate_limit", "name": "接口限流", "default_severity": "warn"},
        ],
    },
    "factor": {
        "name": "因子与模型",
        "events": [
            {"type": "factor_update", "name": "因子更新", "default_severity": "info"},
            {"type": "model_run", "name": "模型运行", "default_severity": "info"},
            {"type": "model_failure", "name": "模型失败", "default_severity": "error"},
        ],
    },
    "opportunity": {
        "name": "机会与观察",
        "events": [
            {"type": "discovery_new", "name": "候选新发现", "default_severity": "info"},
            {"type": "signal_matched", "name": "观察信号满足", "default_severity": "info"},
            {"type": "opportunity_expired", "name": "机会过期", "default_severity": "warn"},
        ],
    },
    "portfolio": {
        "name": "组合与交易",
        "events": [
            {"type": "trade_executed", "name": "成交", "default_severity": "info"},
            {"type": "auto_trade_blocked", "name": "自动交易阻断", "default_severity": "warn"},
            {"type": "drawdown_warning", "name": "回撤预警", "default_severity": "error"},
            {"type": "max_loss_warning", "name": "最大亏损预警", "default_severity": "error"},
            {"type": "position_changed", "name": "持仓变化", "default_severity": "info"},
        ],
    },
    "review": {
        "name": "绩效与复盘",
        "events": [
            {"type": "review_complete", "name": "复盘完成", "default_severity": "info"},
            {"type": "performance_alert", "name": "绩效预警", "default_severity": "warn"},
        ],
    },
}

# Only these events have production call sites. The rest are inspectable plans,
# not selectable operational subscriptions.
CONNECTED_EVENT_TYPES = frozenset({
    "score_drop", "data_stale", "task_failed", "discovery_new",
    "trade_executed", "auto_trade_blocked", "drawdown_warning", "max_loss_warning",
})
for _source in MESSAGE_SOURCES.values():
    for _event in _source["events"]:
        _event["supported"] = _event["type"] in CONNECTED_EVENT_TYPES
    _source["supported"] = any(event["supported"] for event in _source["events"])


def supported_source_choices() -> set[str]:
    return set(CONNECTED_EVENT_TYPES) | {key for key, value in MESSAGE_SOURCES.items() if value["supported"]}


def list_sources() -> dict:
    """列出所有消息来源分组。"""
    return MESSAGE_SOURCES


def get_source(source_type: str) -> dict | None:
    """获取消息来源分组。"""
    return MESSAGE_SOURCES.get(source_type)


def get_event_type(source_type: str, event_type: str) -> dict | None:
    """获取事件类型定义。"""
    source = MESSAGE_SOURCES.get(source_type)
    if source is None:
        return None
    for event in source["events"]:
        if event["type"] == event_type:
            return event
    return None


__all__ = ["MESSAGE_SOURCES", "list_sources", "get_source", "get_event_type"]
