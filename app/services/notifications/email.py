"""邮件适配器（SMTP，WP-MSG.2）。

通过 Python 标准库 smtplib + email.mime 发送邮件。

配置字段：
- smtp_host：SMTP 服务器主机
- smtp_port：SMTP 端口（默认 465 for SSL，587 for STARTTLS）
- use_tls：是否启用 STARTTLS（默认 False）
- use_ssl：是否使用 SSL 直连（默认 True）
- username：SMTP 用户名（通常为邮箱地址）
- password：SMTP 密码或授权码（敏感）
- from_addr：发件人邮箱
- to_addrs：收件人邮箱列表
- timeout：超时秒数（默认 10）

project_memory 硬约束：
- 超时默认 10 秒，由 smtplib.SMTP.timeout 控制
- password 全部脱敏为 ****（mask_smtp_password）
- 不建立永久线程（smtplib 同步调用）
"""
from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from app.services.notifications.base import ChannelAdapter, SendResult
from app.utils.secret_mask import mask_smtp_password


class EmailAdapter(ChannelAdapter):
    """邮件 SMTP 适配器。

    配置示例：
        {
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "use_ssl": true,
            "username": "alerts@example.com",
            "password": "smtp-password-123",
            "from_addr": "alerts@example.com",
            "to_addrs": ["trader1@example.com", "trader2@example.com"]
        }
    """

    channel_type = "email"
    default_timeout = 10.0
    max_retries = 3

    def validate_config(self, config: dict) -> list[str]:
        """校验配置。"""
        errors: list[str] = []
        if not config.get("smtp_host"):
            errors.append("smtp_host 不能为空")
        port = config.get("smtp_port")
        if port in (None, "", 0):
            errors.append("smtp_port 不能为空")
        else:
            try:
                p = int(port)
                if not (1 <= p <= 65535):
                    errors.append("smtp_port 必须在 1-65535 范围内")
            except (TypeError, ValueError):
                errors.append("smtp_port 必须为整数")

        if not config.get("username"):
            errors.append("username 不能为空")
        if not config.get("password"):
            errors.append("password 不能为空")
        if not config.get("from_addr"):
            errors.append("from_addr 不能为空")

        to_addrs = config.get("to_addrs")
        if not to_addrs:
            errors.append("to_addrs 不能为空")
        elif not isinstance(to_addrs, list):
            errors.append("to_addrs 必须为列表")
        elif not all(isinstance(a, str) and a for a in to_addrs):
            errors.append("to_addrs 必须为非空字符串列表")

        return errors

    def send_test(self, config: dict) -> SendResult:
        """发送测试邮件。"""
        return self.send_message(
            config=config,
            title="【测试】邮件渠道连通性测试",
            body="这是一条来自 dataAanlystNew 的邮件测试消息。",
        )

    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送邮件。"""
        errors = self.validate_config(config)
        if errors:
            return SendResult(
                success=False,
                error_code="INVALID_CONFIG",
                error_message=self._truncate_response("; ".join(errors)),
            )

        smtp_host = str(config["smtp_host"])
        smtp_port = int(config["smtp_port"])
        use_tls = bool(config.get("use_tls", False))
        use_ssl = bool(config.get("use_ssl", True))
        username = str(config["username"])
        password = str(config["password"])
        from_addr = str(config["from_addr"])
        to_addrs = list(config["to_addrs"])
        timeout = self._resolve_timeout(config)

        # 构造 MIME 邮件：同时提供 HTML（markdown 转 html 简化）与纯文本
        msg = MIMEMultipart("alternative")
        msg["Subject"] = title
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)

        # 纯文本部分：优先 body_text，否则 body
        text_part = body_text if body_text else body
        msg.attach(MIMEText(text_part, "plain", "utf-8"))
        # HTML 部分（简化：仅转义后包裹 <pre>，避免引入 markdown 依赖）
        html_part = f"<pre>{_escape_html(body)}</pre>"
        msg.attach(MIMEText(html_part, "html", "utf-8"))

        def _send() -> SendResult:
            # 不建立永久线程：smtplib 同步调用，受 timeout 控制
            ctx = ssl.create_default_context()
            if use_ssl:
                # SSL 直连（端口 465）
                with smtplib.SMTP_SSL(
                    smtp_host, smtp_port, context=ctx, timeout=timeout
                ) as server:
                    server.login(username, password)
                    server.sendmail(from_addr, to_addrs, msg.as_string())
            else:
                # 明文 / STARTTLS（端口 587/25）
                with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as server:
                    server.ehlo()
                    if use_tls:
                        server.starttls(context=ctx)
                        server.ehlo()
                    server.login(username, password)
                    server.sendmail(from_addr, to_addrs, msg.as_string())

            return SendResult(
                success=True,
                status_code=250,  # SMTP 250 OK
                response_summary=self._truncate_response(
                    f"已发送至 {len(to_addrs)} 个收件人"
                ),
            )

        return self._retry_send(_send, config)

    def mask_config(self, config: dict) -> dict:
        """返回脱敏后的配置。"""
        masked: dict[str, Any] = {}
        if "smtp_host" in config:
            masked["smtp_host"] = config.get("smtp_host")
        if "smtp_port" in config:
            masked["smtp_port"] = config.get("smtp_port")
        if "use_tls" in config:
            masked["use_tls"] = config.get("use_tls")
        if "use_ssl" in config:
            masked["use_ssl"] = config.get("use_ssl")
        if "username" in config:
            masked["username"] = config.get("username")
        if "password" in config:
            # SMTP 密码全部 ****
            masked["password"] = mask_smtp_password(str(config.get("password") or ""))
        if "from_addr" in config:
            masked["from_addr"] = config.get("from_addr")
        if "to_addrs" in config:
            masked["to_addrs"] = list(config.get("to_addrs") or [])
        if "timeout" in config:
            masked["timeout"] = config.get("timeout")
        return masked


def _escape_html(text: str) -> str:
    """简易 HTML 转义。"""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


__all__ = ["EmailAdapter"]
