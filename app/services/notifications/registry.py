"""渠道适配器注册表（WP-MSG.2）。

集中注册所有渠道适配器实例，供 dispatcher / API 路由按 channel_type 获取。

project_memory 硬约束：
- 适配器实例为单例，熔断器状态在进程内共享
- 不建立永久线程
"""
from __future__ import annotations

from app.services.notifications.base import ChannelAdapter
from app.services.notifications.dingtalk import DingTalkAdapter
from app.services.notifications.email import EmailAdapter
from app.services.notifications.in_app import InAppAdapter
from app.services.notifications.onebot import OneBotAdapter
from app.services.notifications.webhook import WebhookAdapter
from app.services.notifications.wxpusher import WxPusherAdapter


# 单例实例字典：进程内共享熔断器状态
_ADAPTERS: dict[str, ChannelAdapter] = {
    "in_app": InAppAdapter(),
    "wxpusher": WxPusherAdapter(),
    "dingtalk": DingTalkAdapter(),
    "onebot": OneBotAdapter(),
    "email": EmailAdapter(),
    "webhook": WebhookAdapter(),
}


def get_adapter(channel_type: str) -> ChannelAdapter | None:
    """获取渠道适配器。

    Args:
        channel_type: 渠道类型（in_app/wxpusher/dingtalk/onebot/email/webhook）

    Returns:
        适配器实例；未知类型返回 None
    """
    return _ADAPTERS.get(channel_type)


def list_adapters() -> dict[str, ChannelAdapter]:
    """列出所有适配器（返回副本，防止外部修改内部注册表）。"""
    return dict(_ADAPTERS)


__all__ = ["get_adapter", "list_adapters"]
