"""JSON 解析公共工具。

统一处理 config_json / snapshot_json 等 JSON 字段的安全解析，
避免各模块重复 try/except + fallback 样板代码。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def safe_loads_json(
    raw: str | None,
    default: Any = None,
    *,
    context: str | None = None,
    log_level: int = logging.WARNING,
) -> Any:
    """安全解析 JSON 字符串，失败时返回 default 并记录日志。

    Args:
        raw: 待解析的 JSON 字符串。None 或空字符串直接返回 default。
        default: 解析失败时的返回值，默认 None。
        context: 可选的上下文标识（如规则 ID、对象类型），用于日志定位。
        log_level: 解析失败时的日志级别，默认 WARNING。

    Returns:
        解析后的 dict/list，或 default。

    风控加固：
        - 区分 JSONDecodeError（数据损坏，warning）与其他异常（TypeError 等，warning）
        - 失败时记录原始片段（前 200 字符），便于运维定位
        - 不抛异常，调用方拿到 default 自行处理
    """
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.log(
            log_level,
            "JSON 解析失败 (JSONDecodeError)%s，原始片段: %r",
            f" context={context}" if context else "",
            raw[:200],
        )
        return default
    except Exception:
        logger.log(
            log_level,
            "JSON 解析失败 (unexpected error)%s",
            f" context={context}" if context else "",
            exc_info=True,
        )
        return default
