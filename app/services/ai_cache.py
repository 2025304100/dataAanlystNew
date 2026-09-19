"""AI 响应缓存（WP-AI.2 多 Profile 主备降级）。

为相同解释请求按数据版本短期缓存，避免重复请求 AI。

设计要点：
- Key: hash(prompt + context_data_version)
- TTL: 可配置（默认 300 秒）
- 内存缓存（LRU）+ 可选 Redis（如果配置了）
- AI 失败不阻塞业务流程（get 时未命中返回 None，set 时异常吞掉）

project_memory 硬约束：
- AI 失败不阻塞任何业务流程
- 相同解释请求按数据版本短期缓存
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


# 默认 TTL（秒）
_DEFAULT_TTL = 300
# 默认最大缓存条目
_DEFAULT_MAXSIZE = 256


def _cache_key(prompt: str, context_version: str) -> str:
    """生成缓存 key（SHA256）。"""
    raw = f"{prompt}\x00{context_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AICache:
    """AI 响应缓存。

    内存缓存（LRU）+ 可选 Redis（如果配置了，第一阶段未实现）。
    """

    def __init__(
        self,
        *,
        ttl: int = _DEFAULT_TTL,
        maxsize: int = _DEFAULT_MAXSIZE,
    ) -> None:
        self._ttl = max(0, int(ttl))
        self._maxsize = max(1, int(maxsize))
        self._lock = threading.RLock()
        # OrderedDict: key -> (response, expire_at)
        self._store: OrderedDict[str, tuple[dict, float]] = OrderedDict()

    def get(self, prompt: str, context_version: str) -> dict | None:
        """获取缓存的响应，未命中或过期返回 None。"""
        key = _cache_key(prompt, context_version)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                logger.debug("AICache: miss for key=%s", key[:8])
                return None
            response, expire_at = entry
            if time.time() >= expire_at:
                # 过期，移除
                self._store.pop(key, None)
                logger.debug("AICache: expired for key=%s", key[:8])
                return None
            # 命中，移到末尾（LRU）
            self._store.move_to_end(key)
            logger.debug("AICache: hit for key=%s", key[:8])
            return response

    def set(
        self,
        prompt: str,
        context_version: str,
        response: dict,
        ttl: int | None = None,
    ) -> None:
        """设置缓存。ttl=None 使用默认 TTL。"""
        if not isinstance(response, dict):
            logger.warning("AICache: response must be dict, got %s", type(response))
            return
        key = _cache_key(prompt, context_version)
        effective_ttl = self._ttl if ttl is None else max(0, int(ttl))
        expire_at = time.time() + effective_ttl
        with self._lock:
            # 容量控制（LRU 淘汰）
            while len(self._store) >= self._maxsize and key not in self._store:
                self._store.popitem(last=False)
            self._store[key] = (response, expire_at)
            self._store.move_to_end(key)
            logger.debug("AICache: set key=%s ttl=%ss", key[:8], effective_ttl)

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            count = len(self._store)
            self._store.clear()
            logger.debug("AICache: cleared %s entries", count)

    def size(self) -> int:
        """返回当前缓存条目数。"""
        with self._lock:
            return len(self._store)

    def __repr__(self) -> str:
        return f"<AICache(size={self.size()}, ttl={self._ttl}, maxsize={self._maxsize})>"


# ── 单例 ────────────────────────────────────────────────────

_singleton: AICache | None = None
_singleton_lock = threading.Lock()


def get_ai_cache() -> AICache:
    """获取全局 AICache 单例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AICache()
    return _singleton


def reset_ai_cache() -> None:
    """重置单例（仅用于测试）。"""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = [
    "AICache",
    "get_ai_cache",
    "reset_ai_cache",
]
