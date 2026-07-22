"""渠道状态机（WP-MSG.4）。

状态流转：
  unconfigured → pending_test → test_success → enabled
                 ↓
              test_failed（独立状态，可重新测试）

  enabled → disabled（手动禁用或鉴权失败）
  disabled → enabled（手动恢复）
  test_failed → pending_test（重新测试）

project_memory 硬约束：
- 渠道状态机：未配置 → 已配置待测试 → 测试成功 → 已启用；测试失败为独立状态
- 只有"测试成功+已启用"的第三方渠道才能在策略中勾选；未满足时选项禁用，
  提供"去配置""立即测试"入口
"""
from __future__ import annotations

from app.models.notification import (
    CHANNEL_STATUS_DISABLED,
    CHANNEL_STATUS_ENABLED,
    CHANNEL_STATUS_PENDING_TEST,
    CHANNEL_STATUS_TEST_FAILED,
    CHANNEL_STATUS_TEST_SUCCESS,
    CHANNEL_STATUS_UNCONFIGURED,
    CHANNEL_TYPE_IN_APP,
    NotificationChannel,
)


# 状态枚举（与模型中的常量保持一致）
STATE_UNCONFIGURED = CHANNEL_STATUS_UNCONFIGURED
STATE_PENDING_TEST = CHANNEL_STATUS_PENDING_TEST
STATE_TEST_SUCCESS = CHANNEL_STATUS_TEST_SUCCESS
STATE_TEST_FAILED = CHANNEL_STATUS_TEST_FAILED
STATE_ENABLED = CHANNEL_STATUS_ENABLED
STATE_DISABLED = CHANNEL_STATUS_DISABLED


# 状态流转图：from_state -> {允许的 to_state 集合}
TRANSITIONS: dict[str, set[str]] = {
    STATE_UNCONFIGURED: {STATE_PENDING_TEST},  # 配置完成后进入待测试
    STATE_PENDING_TEST: {STATE_TEST_SUCCESS, STATE_TEST_FAILED},
    STATE_TEST_SUCCESS: {STATE_ENABLED, STATE_DISABLED},
    STATE_TEST_FAILED: {STATE_PENDING_TEST},  # 重新测试
    STATE_ENABLED: {STATE_DISABLED},
    STATE_DISABLED: {STATE_ENABLED},
}


def can_transition(from_state: str, to_state: str) -> bool:
    """检查状态流转是否合法。"""
    return to_state in TRANSITIONS.get(from_state, set())


def is_selectable_for_policy(channel: NotificationChannel) -> bool:
    """渠道是否可被策略选中。

    spec line 272：只有"测试成功+已启用"的第三方渠道才能在策略中勾选。
    in_app 渠道默认可选（无需测试）。

    Args:
        channel: 渠道实例

    Returns:
        True 表示可在策略中勾选；False 表示不可勾选
    """
    # in_app 渠道无需测试，只要启用即可
    if channel.channel_type == CHANNEL_TYPE_IN_APP:
        return bool(channel.enabled)

    # 第三方渠道必须 status=enabled 且 enabled=True
    return channel.status == STATE_ENABLED and bool(channel.enabled)


def get_selection_hint(channel: NotificationChannel) -> str:
    """获取渠道不可选时的提示。

    提供明确的下一步操作指引（"去配置"/"立即测试"等入口的语义提示）。
    错误消息不暴露敏感信息。

    Args:
        channel: 渠道实例

    Returns:
        提示文案；可选时返回空字符串
    """
    # in_app 渠道：只需启用
    if channel.channel_type == CHANNEL_TYPE_IN_APP:
        if not channel.enabled:
            return "请先启用站内消息渠道"
        return ""

    status = channel.status
    if status == STATE_UNCONFIGURED:
        return "渠道未配置，请先完成配置"
    if status == STATE_PENDING_TEST:
        return "渠道已配置待测试，请立即测试"
    if status == STATE_TEST_FAILED:
        return "渠道测试失败，请检查配置后重新测试"
    if status == STATE_TEST_SUCCESS and not channel.enabled:
        return "渠道测试成功，请启用"
    if status == STATE_ENABLED:
        return ""
    if status == STATE_DISABLED:
        return "渠道已禁用，请启用"
    return ""


__all__ = [
    "STATE_UNCONFIGURED",
    "STATE_PENDING_TEST",
    "STATE_TEST_SUCCESS",
    "STATE_TEST_FAILED",
    "STATE_ENABLED",
    "STATE_DISABLED",
    "TRANSITIONS",
    "can_transition",
    "is_selectable_for_policy",
    "get_selection_hint",
]
