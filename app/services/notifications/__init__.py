"""通知渠道适配器包（WP-MSG.2）。

提供 6 类渠道适配器：in_app / wxpusher / dingtalk / onebot / email / webhook，
统一实现 ChannelAdapter 抽象接口。

project_memory 硬约束：
- 适配器统一使用有限超时、重试、熔断，不建立无法取消的永久线程
- 消息只含必要标的/状态/跳转 ID，不含数据库连接/Token/完整策略配置/技术堆栈
- API/日志/导出/前端状态中均不出现完整 Token/Webhook 签名 Secret/SMTP 密码
- 模板变量转义，防止 Markdown/Webhook 注入
- 错误消息不暴露敏感信息
"""
from __future__ import annotations

from app.services.notifications.base import ChannelAdapter, SendResult
from app.services.notifications.registry import get_adapter, list_adapters

__all__ = [
    "ChannelAdapter",
    "SendResult",
    "get_adapter",
    "list_adapters",
]
