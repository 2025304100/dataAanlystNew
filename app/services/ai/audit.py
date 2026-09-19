"""AI 审计记录优化（WP-AI.6 结构化输出与审计）。

提供审计记录的创建、摘要、查询、导出能力。

关键约束：
- 审计记录只保存必要上下文摘要，不保存：
  - 完整 K 线数据
  - 敏感配置（API Key/Webhook/密码）
  - 完整行情数据
- export_audit_records 确保脱敏，不包含任何敏感信息
- AI 审计失败不阻塞业务流程（create_audit_record 异常吞掉并返回 None）

project_memory 硬约束：
- 审计记录不保存完整 K 线和敏感配置
- 永不返回明文 Secret
- AI 失败不阻塞业务
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_session import AIActionAudit
from app.services.ai_session_service import add_action_audit

logger = logging.getLogger(__name__)


# 审计上下文中禁止保存的敏感字段名（不区分大小写匹配）
_SENSITIVE_KEY_PATTERNS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "webhook",
    "credential",
    "authorization",
    "auth_header",
    "app_token",
    "app_secret",
    "private_key",
)

# 审计上下文中禁止保存的数据字段（完整 K 线/行情等大字段）
_LARGE_DATA_KEY_PATTERNS = (
    "bars",
    "kline",
    "klines",
    "candles",
    "ohlcv",
    "quotes",
    "tick",
    "ticks",
    "orderbook",
    "depth",
    "market_data",
    "full_data",
    "raw_data",
    "complete_data",
)


def _is_sensitive_key(key: str) -> bool:
    """判断 key 是否为敏感字段（不区分大小写）。"""
    if not key:
        return False
    lower = key.lower()
    return any(pat in lower for pat in _SENSITIVE_KEY_PATTERNS)


def _is_large_data_key(key: str) -> bool:
    """判断 key 是否为大字段（完整 K 线/行情等）。"""
    if not key:
        return False
    lower = key.lower()
    return any(pat in lower for pat in _LARGE_DATA_KEY_PATTERNS)


def _scrub_value(value: Any, *, max_size: int) -> Any:
    """递归清洗值：移除敏感字段与大字段，截断超长字符串。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(str(k)):
                result[k] = "[REDACTED]"
                continue
            if _is_large_data_key(str(k)):
                result[k] = "[STRIPPED]"
                continue
            result[k] = _scrub_value(v, max_size=max_size)
        return result
    if isinstance(value, list):
        # 列表过长时只保留前若干项 + 计数
        if len(value) > 20:
            head = [_scrub_value(v, max_size=max_size) for v in value[:5]]
            return head + [f"... ({len(value) - 5} more items)"]
        return [_scrub_value(v, max_size=max_size) for v in value]
    if isinstance(value, str):
        if len(value) > max_size:
            return value[:max_size] + f"... [truncated, total {len(value)} chars]"
        return value
    return value


def summarize_context_for_audit(context_pack: Any) -> dict:
    """将上下文包压缩为审计摘要（WP-AI.6）。

    支持两种输入：
    1. ContextPack 对象（含 source_page/references/metadata 等属性）
    2. dict（直接作为上下文包处理）

    输出只保留必要摘要字段，不包含：
    - 完整 K 线数据（bars/kline/ohlcv 等）
    - 敏感配置（api_key/secret/token/webhook 等）
    - 完整行情数据

    Args:
        context_pack: 上下文包对象或字典

    Returns:
        审计摘要字典
    """
    max_size = int(getattr(settings, "AI_AUDIT_MAX_CONTEXT_SIZE", 2048))

    # 处理对象型上下文包（duck-typing）
    if not isinstance(context_pack, dict):
        summary: dict[str, Any] = {}
        for attr in ("source_page", "references", "metadata", "symbols", "data_as_of"):
            value = getattr(context_pack, attr, None)
            if value is None:
                continue
            summary[attr] = value
        # 如果对象有 to_dict 方法，优先使用
        to_dict_fn = getattr(context_pack, "to_dict", None)
        if callable(to_dict_fn) and not summary:
            try:
                summary = to_dict_fn()
            except Exception as exc:
                logger.debug("summarize_context_for_audit to_dict failed: %s", exc)
        context_dict = summary
    else:
        context_dict = dict(context_pack)

    # 清洗敏感字段与大字段
    scrubbed = _scrub_value(context_dict, max_size=max_size)

    # 提取/标准化关键字段
    metadata = scrubbed.get("metadata") if isinstance(scrubbed, dict) else None
    if isinstance(metadata, dict):
        result = {
            "source_page": scrubbed.get("source_page"),
            "references": scrubbed.get("references"),
            "data_as_of": metadata.get("data_as_of"),
            "model_version": metadata.get("model_version"),
            "rule_version": metadata.get("rule_version"),
            "provider_used": metadata.get("provider_used"),
        }
    else:
        result = {
            "source_page": scrubbed.get("source_page") if isinstance(scrubbed, dict) else None,
            "references": scrubbed.get("references") if isinstance(scrubbed, dict) else None,
            "data_as_of": scrubbed.get("data_as_of") if isinstance(scrubbed, dict) else None,
            "model_version": scrubbed.get("model_version") if isinstance(scrubbed, dict) else None,
            "rule_version": scrubbed.get("rule_version") if isinstance(scrubbed, dict) else None,
            "provider_used": scrubbed.get("provider_used") if isinstance(scrubbed, dict) else None,
        }

    # 移除 None 值（保持摘要紧凑）
    return {k: v for k, v in result.items() if v is not None}


def create_audit_record(
    db: Session,
    session_id: int,
    message_id: int,
    context_pack: dict,
    action_type: str,
    suggested_payload: dict,
) -> AIActionAudit | None:
    """创建审计记录（WP-AI.6）。

    关键：只保存上下文摘要，不保存：
    - 完整 K 线数据
    - 敏感配置（API Key/Webhook/密码）
    - 完整行情数据

    AI 审计失败不阻塞业务流程：异常时记录日志并返回 None。

    Args:
        db: 数据库会话
        session_id: 会话 ID（用于日志关联，不直接写入审计记录）
        message_id: 关联的 AIMessage.id
        context_pack: 上下文包（将压缩为摘要后存入 preview_result）
        action_type: 动作类型
        suggested_payload: AI 建议的 payload

    Returns:
        创建后的 AIActionAudit，失败返回 None
    """
    if not getattr(settings, "AI_AUDIT_ENABLED", True):
        logger.debug("create_audit_record skipped: AI_AUDIT_ENABLED=False")
        return None

    try:
        # 压缩上下文为摘要（移除敏感字段与大字段）
        summary = summarize_context_for_audit(context_pack)
        summary_json = json.dumps(summary, ensure_ascii=False)

        # suggested_payload 也需要清洗敏感字段
        scrubbed_payload = _scrub_value(
            suggested_payload if isinstance(suggested_payload, dict) else {},
            max_size=int(getattr(settings, "AI_AUDIT_MAX_CONTEXT_SIZE", 2048)),
        )

        return add_action_audit(
            db,
            message_id=message_id,
            action_type=action_type,
            suggested_payload=scrubbed_payload,
            preview_result=summary,
        )
    except Exception as exc:
        logger.warning(
            "create_audit_record failed (session_id=%s, message_id=%s): %s",
            session_id, message_id, exc,
        )
        return None


def get_audit_history(
    db: Session,
    session_id: int,
    limit: int = 50,
) -> list[AIActionAudit]:
    """获取会话的审计历史（WP-AI.6）。

    通过 JOIN AIMessage 过滤出指定会话的审计记录，按创建时间倒序。

    Args:
        db: 数据库会话
        session_id: 会话 ID
        limit: 返回最大条数

    Returns:
        AIActionAudit 列表
    """
    from app.models.ai_session import AIMessage

    stmt = (
        select(AIActionAudit)
        .join(AIMessage, AIActionAudit.message_id == AIMessage.id)
        .where(AIMessage.session_id == session_id)
        .order_by(AIActionAudit.created_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    return list(db.execute(stmt).scalars().all())


def export_audit_records(
    db: Session,
    session_id: int,
) -> list[dict]:
    """导出审计记录（脱敏）（WP-AI.6）。

    确保导出结果不包含任何敏感信息：
    - 再次对 suggested_payload / preview_result / final_result 做脱敏清洗
    - 不包含 API Key/Secret/Webhook/密码等字段

    Args:
        db: 数据库会话
        session_id: 会话 ID

    Returns:
        脱敏后的审计记录字典列表
    """
    max_size = int(getattr(settings, "AI_AUDIT_MAX_CONTEXT_SIZE", 2048))
    records = get_audit_history(db, session_id, limit=500)

    exported: list[dict] = []
    for audit in records:
        # 解析 JSON 字段
        suggested_payload = _safe_json_load(audit.suggested_payload)
        preview_result = _safe_json_load(audit.preview_result)
        final_result = _safe_json_load(audit.final_result)

        # 再次脱敏（防止历史数据中残留敏感字段）
        record = {
            "id": audit.id,
            "message_id": audit.message_id,
            "action_type": audit.action_type,
            "suggested_payload": _scrub_value(suggested_payload, max_size=max_size),
            "preview_result": _scrub_value(preview_result, max_size=max_size),
            "user_confirmed": audit.user_confirmed,
            "confirmed_at": audit.confirmed_at.isoformat() if audit.confirmed_at else None,
            "final_result": _scrub_value(final_result, max_size=max_size),
            "rejected_reason": audit.rejected_reason,
            "created_at": audit.created_at.isoformat() if audit.created_at else None,
        }
        exported.append(record)
    return exported


def _safe_json_load(value: str | None) -> Any:
    """安全解析 JSON 字符串，失败时返回原始字符串或 None。"""
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


__all__ = [
    "create_audit_record",
    "summarize_context_for_audit",
    "get_audit_history",
    "export_audit_records",
]
