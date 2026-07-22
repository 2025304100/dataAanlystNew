"""钉钉机器人适配器（WP-MSG.2）。

通过钉钉自定义机器人 Webhook 发送 Markdown 消息。

配置字段：
- webhook：钉钉机器人 Webhook URL（敏感，含 access_token 查询参数）
- secret：签名密钥（可选，敏感；启用后请求需带 timestamp + sign 查询参数）
- keyword：自定义关键词（可选；若机器人设置了关键词安全策略，消息必须包含该词）
- timeout：超时秒数（可选，默认 10）

签名算法：HMAC-SHA256(timestamp + "\\n" + secret)，base64 编码后作为 sign 查询参数。

project_memory 硬约束：
- 超时默认 10 秒
- webhook URL 脱敏（mask_url 替换查询参数 value 为 ****）
- secret 脱敏（mask_webhook_secret）
- 不建立永久线程
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any

import httpx

from app.services.notifications.base import ChannelAdapter, SendResult
from app.utils.secret_mask import mask_url, mask_webhook_secret


class DingTalkAdapter(ChannelAdapter):
    """钉钉机器人适配器。

    配置示例：
        {
            "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
            "secret": "SECxxxx",
            "keyword": "机会中心"
        }
    """

    channel_type = "dingtalk"
    default_timeout = 10.0
    max_retries = 3

    def validate_config(self, config: dict) -> list[str]:
        """校验配置：webhook 必须非空。"""
        errors: list[str] = []
        webhook = config.get("webhook")
        if not webhook or not str(webhook).strip():
            errors.append("webhook 不能为空")
        elif not str(webhook).startswith(("http://", "https://")):
            errors.append("webhook 必须为合法的 http(s) URL")
        return errors

    def send_test(self, config: dict) -> SendResult:
        """发送测试消息。"""
        # keyword 安全策略：若配置 keyword，测试消息必须包含
        keyword = config.get("keyword") or "测试"
        return self.send_message(
            config=config,
            title="【测试】钉钉渠道连通性测试",
            body=f"{keyword} - 这是一条来自 dataAanlystNew 的钉钉测试消息。",
        )

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送钉钉消息（markdown 类型）。"""
        errors = self.validate_config(config)
        if errors:
            return SendResult(
                success=False,
                error_code="INVALID_CONFIG",
                error_message=self._truncate_response("; ".join(errors)),
            )

        webhook = str(config["webhook"])
        secret = config.get("secret")
        keyword = config.get("keyword")
        timeout = self._resolve_timeout(config)

        # 关键词安全策略：消息正文必须包含 keyword
        text = body if body else (body_text or "")
        if keyword:
            if keyword not in title and keyword not in text:
                # 自动追加 keyword，避免被钉钉拒绝
                text = f"{keyword}\n{text}"

        # markdown 消息体
        request_body: dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }

        # 签名（如果配置了 secret）
        url = webhook
        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(
                base64.b64encode(hmac_code).decode("utf-8")
            )
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={timestamp}&sign={sign}"

        def _send() -> SendResult:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=request_body)
            status_code = resp.status_code
            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {}
            # 钉钉返回 errcode=0 表示成功
            ok = (
                status_code == 200
                and isinstance(resp_data, dict)
                and resp_data.get("errcode") == 0
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
                error_code=str(resp_data.get("errcode", status_code)),
                error_message=self._truncate_response(
                    str(resp_data.get("errmsg", "钉钉返回失败"))
                ),
            )

        return self._retry_send(_send, config)

    def mask_config(self, config: dict) -> dict:
        """返回脱敏后的配置。"""
        masked: dict[str, Any] = {}
        if "webhook" in config:
            # webhook 中的 access_token 查询参数脱敏
            masked["webhook"] = mask_url(str(config.get("webhook") or ""))
        if "secret" in config:
            masked["secret"] = mask_webhook_secret(str(config.get("secret") or ""))
        if "keyword" in config:
            # keyword 非密钥，原样保留
            masked["keyword"] = config.get("keyword")
        if "timeout" in config:
            masked["timeout"] = config.get("timeout")
        return masked

    def _safe_response_summary(self, resp_data: dict) -> str:
        """从响应中提取脱敏摘要。"""
        try:
            # 钉钉响应不含敏感字段，但仍截断
            return self._truncate_response(
                f'{{"errcode":{resp_data.get("errcode")},'
                f'"errmsg":"{resp_data.get("errmsg")}"}}'
            )
        except Exception:
            return self._truncate_response(str(resp_data))


__all__ = ["DingTalkAdapter"]
