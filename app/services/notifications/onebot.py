"""QQ / OneBot 适配器（WP-MSG.2）。

OneBot 11/12 标准的 HTTP POST 推送适配器，适用于 go-cqhttp / NapCat / Lagrange 等
OneBot v11 实现。

配置字段：
- webhook：OneBot HTTP POST 上报地址（含 access_token 时由 header 携带）
- access_token：鉴权 Token（敏感）
- target_type：目标类型，"group" 或 "user"
- target_id：群号或 QQ 号
- message_type：消息类型，默认 "group"/"private"（依据 target_type 自动推断，可显式覆盖）
- timeout：超时秒数（默认 10）

API：发送至 webhook URL，请求体 OneBot 标准：
    {
        "action": "send_group_msg",
        "params": {
            "group_id": 123456,
            "message": [{"type": "text", "data": {"text": "..."}}]
        }
    }

project_memory 硬约束：
- 超时默认 10 秒
- webhook / access_token 脱敏
- 不建立永久线程
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.notifications.base import ChannelAdapter, SendResult
from app.utils.secret_mask import mask_token, mask_url


class OneBotAdapter(ChannelAdapter):
    """OneBot (QQ) 适配器。

    配置示例：
        {
            "webhook": "http://127.0.0.1:5700/send_group_msg",
            "access_token": "abcdef123456",
            "target_type": "group",
            "target_id": 123456789
        }
    """

    channel_type = "onebot"
    default_timeout = 10.0
    max_retries = 3

    def validate_config(self, config: dict) -> list[str]:
        """校验配置。"""
        errors: list[str] = []
        webhook = config.get("webhook")
        if not webhook or not str(webhook).strip():
            errors.append("webhook 不能为空")
        elif not str(webhook).startswith(("http://", "https://")):
            errors.append("webhook 必须为合法的 http(s) URL")

        target_type = config.get("target_type")
        if target_type not in ("group", "user"):
            errors.append('target_type 必须为 "group" 或 "user"')

        target_id = config.get("target_id")
        if target_id in (None, "", 0):
            errors.append("target_id 不能为空")
        else:
            try:
                if isinstance(target_id, str):
                    int(target_id)
                else:
                    int(target_id)
            except (TypeError, ValueError):
                errors.append("target_id 必须为整数（群号/QQ号）")

        return errors

    def send_test(self, config: dict) -> SendResult:
        """发送测试消息。"""
        return self.send_message(
            config=config,
            title="【测试】OneBot 渠道连通性测试",
            body="这是一条来自 dataAanlystNew 的 OneBot 测试消息。",
        )

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送 OneBot 消息。"""
        errors = self.validate_config(config)
        if errors:
            return SendResult(
                success=False,
                error_code="INVALID_CONFIG",
                error_message=self._truncate_response("; ".join(errors)),
            )

        webhook = str(config["webhook"])
        access_token = config.get("access_token")
        target_type = config.get("target_type", "group")
        target_id = config.get("target_id")
        # target_id 统一为 int
        target_id_int = int(target_id) if not isinstance(target_id, int) else target_id
        message_type = config.get("message_type")
        if not message_type:
            message_type = "group" if target_type == "group" else "private"

        timeout = self._resolve_timeout(config)

        # 组装消息文本：title + body
        text = body if body else (body_text or "")
        if title:
            text = f"{title}\n{text}"

        # OneBot 11 标准请求体
        action = "send_group_msg" if message_type == "group" else "send_private_msg"
        params_key = "group_id" if message_type == "group" else "user_id"
        request_body: dict[str, Any] = {
            "action": action,
            "params": {
                params_key: target_id_int,
                "message": [{"type": "text", "data": {"text": text}}],
            },
        }

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        def _send() -> SendResult:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(webhook, json=request_body, headers=headers)
            status_code = resp.status_code
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {}
            # OneBot 返回 status="ok" 表示成功
            ok = (
                status_code == 200
                and isinstance(resp_data, dict)
                and resp_data.get("status") == "ok"
            )
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
                error_code=str(resp_data.get("retcode", status_code)),
                error_message=self._truncate_response(
                    str(resp_data.get("msg", "OneBot 返回失败"))
                ),
            )

        return self._retry_send(_send, config)

    def mask_config(self, config: dict) -> dict:
        """返回脱敏后的配置。"""
        masked: dict[str, Any] = {}
        if "webhook" in config:
            masked["webhook"] = mask_url(str(config.get("webhook") or ""))
        if "access_token" in config:
            masked["access_token"] = mask_token(str(config.get("access_token") or ""))
        if "target_type" in config:
            masked["target_type"] = config.get("target_type")
        if "target_id" in config:
            masked["target_id"] = config.get("target_id")
        if "message_type" in config:
            masked["message_type"] = config.get("message_type")
        if "timeout" in config:
            masked["timeout"] = config.get("timeout")
        return masked

    def _safe_response_summary(self, resp_data: dict) -> str:
        """从响应中提取脱敏摘要。"""
        try:
            safe = {
                k: v for k, v in resp_data.items()
                if k.lower() not in ("access_token", "token", "authorization")
            }
            return self._truncate_response(json.dumps(safe, ensure_ascii=False))
        except Exception:
            return self._truncate_response(str(resp_data))


__all__ = ["OneBotAdapter"]
