"""白盒测试 - 探测端点独立线程池守护 (P0 回归)。

验证：
1. _PROBE_EXECUTOR 存在且 max_workers=8（防止回退到 asyncio.to_thread 耗尽默认池）
2. probe_api 的日志输出（start/done/timeout/exc）

这是 P0 稳定性修复的守护测试，防止：
- 探测端点回退到 asyncio.to_thread → 子线程泄漏耗尽 anyio 默认 40 线程池
- 探测无日志 → 卡住时无法诊断
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from app.api.routes import akshare_apis

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. _PROBE_EXECUTOR 常量守护
# ============================================================================

def test_probe_uses_independent_executor():
    """【P0 稳定性回归】akshare_apis 模块应定义 _PROBE_EXECUTOR 独立线程池。

    历史问题：原实现用 asyncio.to_thread 共享 anyio 默认 40 线程池，
    探测超时后子线程泄漏累积，耗尽线程池后所有 async 路由卡死。
    修复：改用独立 ThreadPoolExecutor(max_workers=8)，限制最大泄漏数。
    """
    assert hasattr(akshare_apis, "_PROBE_EXECUTOR"), \
        "akshare_apis 应定义 _PROBE_EXECUTOR 独立线程池"
    assert akshare_apis._PROBE_EXECUTOR is not None, \
        "_PROBE_EXECUTOR 不应为 None"


def test_probe_executor_max_workers_is_8():
    """【P0 稳定性回归】_PROBE_EXECUTOR 的 max_workers 应为 8。

    8 个并发足够覆盖批量探测（3 个一组），同时限制最大泄漏数为 8，
    避免耗尽 FastAPI 默认线程池（40）。
    """
    executor = akshare_apis._PROBE_EXECUTOR
    # ThreadPoolExecutor 的 _max_workers 是内部属性，但可访问
    max_workers = getattr(executor, "_max_workers", None)
    assert max_workers == 8, (
        f"_PROBE_EXECUTOR._max_workers 应为 8, 实际: {max_workers}"
    )


# ============================================================================
# 2. probe_api 日志输出
# ============================================================================


@pytest.fixture
def private_probe_executor(monkeypatch):
    """给日志契约用例一个干净的私有线程池。

    产品侧 `_PROBE_EXECUTOR` 只有 8 个 worker（这本身就是 P0 修复的限定泄漏数），
    全量跑里前面的用例会拿真实 akshare 调用把它占住几十秒，新任务排队超过
    `_PROBE_TIMEOUT_SECONDS`（默认 50s）后 probe_api 只能记 TIMEOUT、永远等不到
    done —— 于是“日志契约”用例变成“共享线程池是否空闲”的受害者。
    “默认池 max_workers=8”仍由上面的守卫用例把门，这里不放宽。
    """
    from concurrent.futures import ThreadPoolExecutor