"""WxPusher 适配器（WP-MSG.2）。

WxPusher 是基于微信公众号的推送服务，通过 HTTP API 向指定用户/主题发送消息。

配置字段：
- app_token：WxPusher 应用 Token（敏感）
- uids：用户 ID 列表（与 topic_ids 至少一个）
- topic_ids：主题 ID 列表（与 uids 至少一个）
- content_type：内容类型（默认 1=文本，可选 2=html、3=markdown）
- timeout：超时秒数（可选，默认 10）

API：https://wxpusher.zjiecloud.com/api/send/message
（实际为 https://wxpusher.zjiecloud.com/api/send/message，
官方文档根域名为 wxpusher.zjiecode.com，下面使用 zjiecloud.com）

project_memory 硬约束：
- 超时默认 10 秒，最大 60 秒
- 错误消息不暴露完整 AppToken
- 不建立永久线程
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.notifications.base import ChannelAdapter, SendResult
from app.utils.secret_mask import mask_token

WXPUSHER_API_URL = "https://wxpusher.zjiecloud.com/api/send/message"


class WxPusherAdapter(ChannelAdapter):
    """WxPusher 适配器。

    配置示例：
        {
            "app_token": "AT_xxxxxxxxxxxxxxxx",
            "uids": ["UID_xxx"],
            "topic_ids": [123],
            "content_type": 3,
            "timeout": 10
        }
    """

    channel_type = "wxpusher"
    default_timeout = 10.0
    max_retries = 3

    def validate_config(self, config: dict) -> list[str]:
        """校验配置。

        - app_token 必须非空
        - uids 或 topic_ids 至少一个非空
        """
        errors: list[str] = []
        if not config.get("app_token"):
            errors.append("app_token 不能为空")

        uids = config.get("uids") or []
        topic_ids = config.get("topic_ids") or []
        if not uids and not topic_ids:
            errors.append("uids 与 topic_ids 至少需配置一个")

        # uid/topic_id 必须是 list
        if uids and not isinstance(uids, list):
            errors.append("uids 必须为列表")
        if topic_ids and not isinstance(topic_ids, list):
            errors.append("topic_ids 必须为列表")

        return errors

    def send_test(self, config: dict) -> SendResult:
        """发送测试消息。"""
        return self.send_message(
            config=config,
            title="【测试】WxPusher 渠道连通性测试",
            body="这是一条来自 dataAanlystNew 的 WxPusher 测试消息。",
        )

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送消息。

        实现要点：
        - 使用 httpx 同步客户端，限定超时
        - 通过 _retry_send 包装重试与熔断
        - 不建立后台线程
        """
        # 发送前再次校验，避免无效配置触发网络调用
        errors = self.validate_config(config)
        if errors:
            return SendResult(
                success=False,
                error_code="INVALID_CONFIG",
                error_message=self._truncate_response("; ".join(errors)),
            )

        app_token = config["app_token"]
        uids = list(config.get("uids") or [])
        topic_ids = list(config.get("topic_ids") or [])
        # content_type 默认 3=markdown，与项目模板格式一致
        content_type = int(config.get("content_type", 3))
        timeout = self._resolve_timeout(config)

        # 消息内容：优先 markdown body，否则纯文本
        content = body if body else (body_text or "")

        request_body: dict[str, Any] = {
            "appToken": app_token,
            "content": content,
            "summary": self._truncate_response(title, max_len=20),
            "contentType": content_type,
        }
        if uids:
            request_body["uids"] = [str(u) for u in uids]
        if topic_ids:
            request_body["topicIds"] = [int(t) for t in topic_ids]

        def _send() -> SendResult:
            # 同步调用，超时由 httpx 控制；不建立永久线程
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(WXPUSHER_API_URL, json=request_body)
            # WxPusher HTTP 200 且 code=0 表示成功
            status_code = resp.status_code
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {}
            ok = (
                status_code == 200
                and isinstance(resp_data, dict)
                and resp_data.get("code") == 0
            )
            # 响应摘要脱敏：移除可能的 token 字段
            safe_summary = self._safe_response_summary(resp_data)
            if ok:
                return SendResult(
                    success=True,
                    status_code=status_code,
                    response_summary=safe_summary,
                )
            return SendResult(
                success=False,
                status_code=status_code,
                response_summary=safe_summary,
                error_code=str(resp_data.get("code", status_code)),
                error_message=self._truncate_response(
                    str(resp_data.get("msg", "WxPusher 返回失败"))
                ),
            )

        return self._retry_send(_send, config)

    def mask_config(self, config: dict) -> dict:
        """返回脱敏后的配置。

        - app_token：使用 mask_token 脱敏
        - uids/topic_ids：保留（用户标识，非密钥）
        - content_type/timeout：原样保留
        """
        masked: dict[str, Any] = {}
        if "app_token" in config:
            masked["app_token"] = mask_token(str(config.get("app_token") or ""))
        if "uids" in config:
            masked["uids"] = list(config.get("uids") or [])
        if "topic_ids" in config:
            masked["topic_ids"] = list(config.get("topic_ids") or [])
        if "content_type" in config:
            masked["content_type"] = config.get("content_type")
        if "timeout" in config:
            masked["timeout"] = config.get("timeout")
        return masked

    def _safe_response_summary(self, resp_data: dict) -> str:
        """从响应中提取脱敏摘要（移除 token 字段）。"""
        try:
            safe = {
                k: v for k, v in resp_data.items()
                if k.lower() not in ("apptoken", "app_token", "token")
            }
            return self._truncate_response(json.dumps(safe, ensure_ascii=False))
        except Exception:
            return self._truncate_response(str(resp_data))


__all__ = ["WxPusherAdapter"]
