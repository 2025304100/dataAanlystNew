"""消息内容脱敏（WP-MSG.5）。

消息只含必要标的/状态/跳转 ID，不推送数据库连接/Token/完整策略配置/技术堆栈。
第三方内容长度超限时自动摘要，保留站内完整详情。

project_memory 硬约束：
- 消息只含必要标的、状态、跳转 ID，不推送数据库连接/Token/完整策略配置/技术堆栈（spec line 128）
- 第三方内容超限时自动摘要，保留站内完整详情（spec line 129）
- API/日志/导出/前端状态中均不出现完整 Token、Webhook 签名 Secret、SMTP 密码（spec line 131）
"""
from __future__ import annotations

from app.schemas.error_sanitizer import sanitize_message


# 禁止出现在消息中的敏感字段名模式（小写匹配）
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "database_url",
    "db_url",
    "connection_string",
    "webhook_secret",
    "smtp_password",
)


# 允许出现在消息中的字段白名单
ALLOWED_KEYS: frozenset[str] = frozenset({
    "title", "body", "body_text",
    "symbol", "symbol_id", "symbol_name",
    "price", "quantity", "status", "action",
    "jump_url", "jump_id", "portfolio_id", "watchlist_id",
    "severity", "event_type", "source_type",
    "timestamp", "rule_name",
})


def sanitize_payload(payload: dict) -> dict:
    """脱敏 payload，移除敏感字段。

    只保留白名单字段，并对字符串值调用 sanitize_message 移除敏感模式。

    Args:
        payload: 原始 payload 字典

    Returns:
        脱敏后的 payload；输入非 dict 时返回空 dict
    """
    if not isinstance(payload, dict):
        return {}

    sanitized: dict = {}
    for key, value in payload.items():
        key_lower = key.lower() if isinstance(key, str) else ""
        # 跳过敏感字段（字段名包含敏感模式）
        if any(pattern in key_lower for pattern in SENSITIVE_PATTERNS):
            continue
        # 只保留白名单字段
        if key in ALLOWED_KEYS:
            sanitized[key] = _sanitize_value(value)

    return sanitized


def _sanitize_value(value):
    """脱敏值，递归处理 dict/list/str。"""
    if isinstance(value, dict):
        return sanitize_payload(value)
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    if isinstance(value, str):
        return sanitize_message(value)
    return value


def truncate_content(content: str, max_length: int = 500) -> str:
    """截断内容到指定长度，用于第三方渠道摘要。

    第三方内容长度超限时自动摘要，保留站内完整详情。
    截断时末尾追加 "..."（占 3 字符），总长度不超过 max_length。

    Args:
        content: 原始内容
        max_length: 最大长度（含末尾 "..."）

    Returns:
        截断后的内容；未超长则原样返回
    """
    if len(content) <= max_length:
        return content
    if max_length <= 3:
        return "..."[:max_length]
    return content[:max_length - 3] + "..."


def build_summary(title: str, body: str, max_length: int = 500) -> str:
    """构建摘要（用于第三方渠道长度限制）。

    格式：{title}\\n\\n{body}（截断到 max_length）

    Args:
        title: 标题
        body: 正文
        max_length: 最大长度

    Returns:
        截断后的摘要字符串
    """
    full = f"{title}\n\n{body}"
    return truncate_content(full, max_length)


def get_in_app_payload(payload: dict) -> dict:
    """获取站内消息完整 payload（不脱敏不截断）。

    站内消息保留完整详情，第三方渠道用摘要。

    Args:
        payload: 原始 payload

    Returns:
        原始 payload（站内消息保留完整内容）
    """
    return payload


__all__ = [
    "SENSITIVE_PATTERNS",
    "ALLOWED_KEYS",
    "sanitize_payload",
    "truncate_content",
    "build_summary",
    "get_in_app_payload",
]
