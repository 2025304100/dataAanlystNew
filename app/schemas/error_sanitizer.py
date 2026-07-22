"""错误消息脱敏工具（WP-S.6）。

复用 `app.services.external_data_gateway._sanitize_error` 的脱敏思路，
扩展覆盖更多模式：MySQL 连接串、password=、API Key、Webhook、SMTP、Bearer Token、邮箱密码组合。

project_memory 硬约束：
- MySQL 连接错误不得暴露明文密码
- 后台日志不含 API Key、Webhook、SMTP 密码或完整用户数据

使用方式：

    from app.schemas.error_sanitizer import sanitize_message

    safe_msg = sanitize_message(f"DB error: mysql://root:secret123@host/db")
    # "DB error: mysql://root:***@host/db"
"""
from __future__ import annotations

import re


# 敏感模式列表：(编译后的正则, 替换模板)
# 顺序：先匹配最具体的（如完整 URL），再匹配通用的（如 password=xxx）
SENSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # MySQL 连接字符串中的密码：mysql://user:password@host
    (re.compile(r"(mysql://[^:]+:)[^@]+(@)"), r"\1***\2"),
    # PostgreSQL / 通用 DB URL：postgresql://user:password@host
    (re.compile(r"(postgresql://[^:]+:)[^@]+(@)"), r"\1***\2"),
    # password=xxx / pwd=xxx（不区分大小写）
    (re.compile(r"(password|pwd)=\S+", re.IGNORECASE), r"\1=***"),
    # API Key（sk- / ak- / AKSC 等前缀 + 至少 20 位字母数字）
    (re.compile(r"(sk-|ak-|AKSC)[A-Za-z0-9]{20,}"), r"\1***"),
    # Webhook URL 中的 token（webhook.site / hooks.slack.com/services/T...）
    (re.compile(r"(webhook\.site/|hooks\.slack\.com/services/T)[A-Za-z0-9/]+"), r"\1***"),
    # SMTP 密码：smtp...password=xxx 或 smtp...password: xxx
    (re.compile(r"(smtp.*?password)[=:]\s*\S+", re.IGNORECASE), r"\1=***"),
    # Authorization Bearer token
    (re.compile(r"(Authorization:\s*Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE), r"\1***"),
    # 邮箱密码组合：user@email.com:password
    (re.compile(r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+):\S+"), r"\1:***"),
    # 通用 Token 字段：token=xxx / api_key=xxx / apikey=xxx / secret=xxx
    (re.compile(r"(token|api_key|apikey|secret|access_token|refresh_token)=\S+", re.IGNORECASE), r"\1=***"),
]


def sanitize_message(msg: str) -> str:
    """脱敏错误消息：移除敏感信息。

    依次应用 `SENSITIVE_PATTERNS` 中的所有正则模式，将密码、API Key、Token 等
    替换为 `***`，避免敏感信息泄漏到日志或 API 响应。

    Args:
        msg: 原始错误消息（可能包含敏感信息）

    Returns:
        脱敏后的消息；输入为 None/空时返回空字符串
    """
    if not msg:
        return ""
    # 确保 msg 是字符串（处理 Exception 对象等）
    if not isinstance(msg, str):
        msg = str(msg)
    for pattern, replacement in SENSITIVE_PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg


__all__ = ["SENSITIVE_PATTERNS", "sanitize_message"]
