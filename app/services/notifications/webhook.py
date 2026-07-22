"""通用 Webhook 适配器（WP-MSG.2）。

支持任意 HTTP(S) Webhook，支持 GET / POST / PUT 方法，
可携带 Authorization Header 与自定义请求模板。

配置字段：
- url：目标 URL（含查询参数时 sensitive）
- method：HTTP 方法，默认 "POST"，可选 GET/PUT
- auth_header：鉴权 Header 完整值（如 "Bearer xxx" 或 "xxx"）（敏感）
- request_template：JSON 字符串模板，含 {title} / {body} / {body_text} 占位符
- timeout：超时秒数（默认 10）

project_memory 硬约束：
- 超时默认 10 秒
- url 脱敏（mask_url）
- auth_header 脱敏（mask_token）
- 不建立永久线程
- 模板变量转义，防止 Webhook 注入
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.services.notifications.base import ChannelAdapter, SendResult
from app.services.notifications.template_renderer import render_template
from app.utils.secret_mask import mask_token, mask_url


class WebhookAdapter(ChannelAdapter):
    """通用 Webhook 适配器。

    配置示例：
        {
            "url": "https://example.com/webhook",
            "method": "POST",
            "auth_header": "Bearer abcdef123456",
            "request_template": "{\"title\":\"{title}\",\"text\":\"{body}\"}"
        }
    """

    channel_type = "webhook"
    default_timeout = 10.0
    max_retries = 3

    DEFAULT_TEMPLATE = '{"title":"{title}","body":"{body}"}'

    def validate_config(self, config: dict) -> list[str]:
        """校验配置。"""
        errors: list[str] = []
        url = config.get("url")
        if not url or not str(url).strip():
            errors.append("url 不能为空")
        elif not str(url).startswith(("http://", "https://")):
            errors.append("url 必须为合法的 http(s) URL")

        method = (config.get("method") or "POST").upper()
        if method not in ("GET", "POST", "PUT"):
            errors.append('method 必须为 "GET" / "POST" / "PUT"')

        # request_template 校验：若提供需为合法 JSON 字符串
        tpl = config.get("request_template")
        if tpl:
            if not isinstance(tpl, str):
                errors.append("request_template 必须为字符串")
            else:
                try:
                    json.loads(tpl)
                except (TypeError, ValueError):
                    errors.append("request_template 必须为合法 JSON 字符串")

        return errors

    def send_test(self, config: dict) -> SendResult:
        """发送测试消息。"""
        return self.send_message(
            config=config,
            title="【测试】Webhook 渠道连通性测试",
            body="这是一条来自 dataAanlystNew 的 Webhook 测试消息。",
        )

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送 Webhook 请求。"""
        errors = self.validate_config(config)
        if errors:
            return SendResult(
                success=False,
                error_code="INVALID_CONFIG",
                error_message=self._truncate_response("; ".join(errors)),
            )

        url = str(config["url"])
        method = (config.get("method") or "POST").upper()
        auth_header = config.get("auth_header")
        timeout = self._resolve_timeout(config)

        # 渲染请求体模板：变量值自动转义防止注入
        template = config.get("request_template") or self.DEFAULT_TEMPLATE
        variables = {
            "title": title,
            "body": body,
            "body_text": body_text if body_text is not None else "",
        }
        # payload 中除 title/body 外的额外变量也参与渲染
        if payload:
            for k, v in payload.items():
                if k not in variables:
                    variables[k] = v

        rendered = render_template(template, variables)
        try:
            request_body = json.loads(rendered)
        except (TypeError, ValueError):
            # 渲染后不是合法 JSON，退化为字符串
            request_body = rendered

        headers: dict[str, str] = {}
        if auth_header:
            # 同时支持 "Bearer xxx" 与裸 token
            headers["Authorization"] = auth_header
        if method in ("POST", "PUT"):
            headers["Content-Type"] = "application/json"

        def _send() -> SendResult:
            with httpx.Client(timeout=timeout) as client:
                if method == "GET":
                    resp = client.get(url, headers=headers)
                elif method == "PUT":
                    resp = client.put(url, json=request_body, headers=headers)
                else:
                    resp = client.post(url, json=request_body, headers=headers)

            status_code = resp.status_code
            # 2xx 视为成功
            ok = 200 <= status_code < 300
            try:
                resp_text = resp.text
            except Exception:
                resp_text = ""
            safe_summary = self._safe_response_summary(resp_text)
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
                error_code=str(status_code),
                error_message=self._truncate_response(
                    f"HTTP {status_code} 错误"
                ),
            )

        return self._retry_send(_send, config)

    def mask_config(self, config: dict) -> dict:
        """返回脱敏后的配置。"""
        masked: dict[str, Any] = {}
        if "url" in config:
            masked["url"] = mask_url(str(config.get("url") or ""))
        if "method" in config:
            masked["method"] = config.get("method")
        if "auth_header" in config:
            # auth_header 形如 "Bearer abc123" 或裸 token，整段脱敏
            masked["auth_header"] = mask_token(str(config.get("auth_header") or ""))
        if "request_template" in config:
            # 模板不含敏感信息，原样保留
            masked["request_template"] = config.get("request_template")
        if "timeout" in config:
            masked["timeout"] = config.get("timeout")
        return masked

    def _safe_response_summary(self, resp_text: str) -> str:
        """从响应文本中提取脱敏摘要。"""
        from app.schemas.error_sanitizer import sanitize_message
        # 先用 sanitize_message 移除可能的 token/secret 模式
        safe = sanitize_message(resp_text or "")
        return self._truncate_response(safe)


__all__ = ["WebhookAdapter"]
