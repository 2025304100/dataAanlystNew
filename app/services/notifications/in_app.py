"""站内消息适配器（WP-MSG.2）。

站内消息默认渠道，不依赖第三方 SDK，无网络调用。
消息内容随 NotificationDelivery.response_summary 一起持久化（由 dispatcher 完成），
本适配器只负责校验与返回 SendResult。

project_memory 硬约束：
- 不建立永久线程（无网络调用，无需重试/熔断复杂逻辑）
- 错误消息不暴露敏感信息
"""
from __future__ import annotations

import time

from app.services.notifications.base import ChannelAdapter, SendResult


class InAppAdapter(ChannelAdapter):
    """站内消息适配器。

    站内消息无需配置（不依赖外部服务），发送总是成功；
    消息正文（body/body_text）由调用方决定，本适配器将 title 与 body 摘要
    回填到 SendResult.response_summary，供 dispatcher 写入
    NotificationDelivery.response_summary 字段。

    AlertEvent 表为告警专用（rule_id 必填），不适合作为通用站内消息存储，
    因此本阶段不直接复用 AlertEvent；dispatcher 在 WP-MSG.3 阶段会将
    站内消息内容写入 NotificationDelivery.response_summary。
    """

    channel_type = "in_app"
    # 站内消息无网络调用，使用较短超时
    default_timeout = 1.0
    max_retries = 0

    def validate_config(self, config: dict) -> list[str]:
        """站内消息无需配置，校验总通过。"""
        return []

    def send_test(self, config: dict) -> SendResult:
        """发送测试站内消息（直接返回成功，由 dispatcher 负责落库）。"""
        start = time.monotonic()
        return SendResult(
            success=True,
            status_code=200,
            response_summary=self._truncate_response(
                "【测试】站内消息渠道连通性正常"
            ),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送站内消息。

        直接返回成功，并将 title/body 摘要放入 response_summary，
        供 dispatcher 写入 NotificationDelivery 表。
        """
        start = time.monotonic()
        content = body_text if body_text else body
        summary = f"[站内] {title}\n{content}"
        return SendResult(
            success=True,
            status_code=200,
            response_summary=self._truncate_response(summary),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def mask_config(self, config: dict) -> dict:
        """站内消息无敏感配置，返回空 dict。"""
        return {}


__all__ = ["InAppAdapter"]
