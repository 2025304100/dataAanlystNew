"""敏感字段脱敏与加密工具（WP-MSG.1）。

提供 Token / Webhook Secret / SMTP 密码 / URL 查询参数的脱敏函数，
用于 NotificationChannel.config_mask_json 字段，确保前端展示与日志不
暴露完整敏感信息。同时提供配置 JSON 的加密/解密占位实现。

project_memory 硬约束：
- 错误消息不暴露敏感信息（不出现完整 Token/Webhook Secret/SMTP 密码）
- 敏感字段用 Secret Store 引用或加密存储

第一阶段策略：
- 脱敏：Token/Webhook Secret 保留前 4 + 后 4，中间 ****；SMTP 密码全部 ****；
  URL 查询参数 value 替换为 ****。
- 加密：base64 编码占位，后续可替换为 AES-256-GCM。
- 重点：结构正确 + 脱敏展示；加密强度可后续加强。
"""
from __future__ import annotations

import base64
import os
from urllib.parse import parse_qsl, urlparse, urlunparse


# ── 脱敏函数 ────────────────────────────────────────────────────


def mask_token(token: str) -> str:
    """脱敏 Token：保留前 4 + 后 4，中间 ****。

    Args:
        token: 原始 Token 字符串

    Returns:
        脱敏后的 Token；空值返回空字符串；
        长度 < 8 时全部 ****（避免保留过少字符导致可推测）；
        长度 8-12 时保留前 2 + 后 2。
    """
    if not token:
        return ""
    if not isinstance(token, str):
        token = str(token)
    if len(token) < 8:
        return "****"
    if len(token) <= 12:
        return f"{token[:2]}****{token[-2:]}"
    return f"{token[:4]}****{token[-4:]}"


def mask_webhook_secret(secret: str) -> str:
    """脱敏 Webhook Secret：保留前 4 + 后 4，中间 ****。

    与 Token 相同策略。
    """
    return mask_token(secret)


def mask_smtp_password(pwd: str) -> str:
    """脱敏 SMTP 密码：全部 ****（不保留任何字符）。

    SMTP 密码可能被多处复用，采用最严格策略：无论长度，全部替换为 ****。
    """
    if not pwd:
        return ""
    return "****"


def mask_url(url: str) -> str:
    """脱敏 URL：保留 scheme/host/path，查询参数 value 替换为 ****。

    Args:
        url: 原始 URL

    Returns:
        脱敏后的 URL；查询参数 key 保留，value 替换为 ****。

    Example:
        >>> mask_url("https://example.com/webhook?token=abc123&uid=456")
        'https://example.com/webhook?token=****&uid=****'
    """
    if not url:
        return ""
    if not isinstance(url, str):
        url = str(url)
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        # 查询参数 value 全部替换为 ****，key 保留
        # 手动拼接 query 避免 urlencode 对 **** 做 URL 编码（%2A%2A%2A%2A）
        masked_params = [
            (key, "****")
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        masked_query = "&".join(f"{key}={value}" for key, value in masked_params)
        return urlunparse(parsed._replace(query=masked_query))
    except Exception:
        # URL 解析失败，保守脱敏：返回 ****
        return "****"


# ── 加密工具（第一阶段：base64 占位）──────────────────────────


def _get_secret_key() -> str:
    """从环境变量获取加密密钥标识（占位实现）。

    后续可替换为从 Secret Store 获取的 AES 密钥。
    """
    return os.environ.get("NOTIFICATION_SECRET_KEY", "default-dev-key-not-for-production")


def encrypt_config(config_json: str) -> str:
    """加密配置 JSON 字符串（占位实现）。

    第一阶段使用 base64 编码，便于调试与解密验证。
    后续可替换为 AES-256-GCM 等强加密算法。

    Args:
        config_json: 原始配置 JSON 字符串

    Returns:
        加密后的字符串（base64 编码）；空值返回空字符串
    """
    if not config_json:
        return ""
    if not isinstance(config_json, str):
        config_json = str(config_json)
    return base64.b64encode(config_json.encode("utf-8")).decode("ascii")


def decrypt_config(encrypted: str) -> str:
    """解密配置 JSON 字符串（占位实现）。

    与 encrypt_config 配套使用。

    Args:
        encrypted: 加密后的字符串（base64 编码）

    Returns:
        原始配置 JSON 字符串；解密失败返回空字符串
    """
    if not encrypted:
        return ""
    try:
        return base64.b64decode(encrypted.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


__all__ = [
    "mask_token",
    "mask_webhook_secret",
    "mask_smtp_password",
    "mask_url",
    "encrypt_config",
    "decrypt_config",
]
