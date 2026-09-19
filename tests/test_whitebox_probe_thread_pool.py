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

def test_probe_logs_start_and_done(monkeypatch, caplog):
    """【P0 稳定性回归】probe_api 应记录 start 和 done INFO 日志。

    历史问题：原实现无任何日志，探测卡住时无法诊断。
    修复：添加 logger.info("probe %s start") 和 logger.info("probe %s done: ...")。
    """
    # mock 依赖
    mock_entry = {"probe_args": {}}
    monkeypatch.setattr(
        akshare_apis,
        "get_registry_entry",
        lambda api_key: mock_entry,
    )

    mock_func = MagicMock(return_value=MagicMock(empty=False))
    monkeypatch.setattr(akshare_apis.ak, "test_api", mock_func, raising=False)

    # mock record_probe_result 和 db
    monkeypatch.setattr(akshare_apis, "record_probe_result", lambda *a, **kw: None)
    mock_db = MagicMock()

    with caplog.at_level(logging.INFO, logger="app.api.routes.akshare_apis"):
        asyncio.run(akshare_apis.probe_api("test_api", mock_db))

    info_logs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("probe test_api start" in r.getMessage() for r in info_logs), \
        "应有 'probe test_api start' INFO 日志"
    assert any("probe test_api done" in r.getMessage() for r in info_logs), \
        "应有 'probe test_api done' INFO 日志"


def test_probe_logs_timeout(monkeypatch, caplog):
    """【P0 稳定性回归】probe_api 超时应记录 WARNING 日志。

    历史问题：原实现超时只写到 DB，无日志，运维无法从日志发现超时事件。
    修复：添加 logger.warning("probe %s TIMEOUT after %ds")。
    """
    # mock 依赖
    mock_entry = {"probe_args": {}}
    monkeypatch.setattr(
        akshare_apis,
        "get_registry_entry",
        lambda api_key: mock_entry,
    )

    # probe_api 在调用 _run_probe 前会检查 getattr(ak, api_key, None)，
    # 需要先 mock ak.test_api 属性，否则抛 HTTPException(500)
    monkeypatch.setattr(akshare_apis.ak, "test_api", MagicMock(), raising=False)

    # mock _run_probe 永久阻塞，触发 asyncio.wait_for 超时
    def _block_forever(func, probe_args):
        import time
        time.sleep(100)

    monkeypatch.setattr(akshare_apis, "_run_probe", _block_forever)

    # 用极短超时加速测试
    monkeypatch.setattr(akshare_apis, "_PROBE_TIMEOUT_SECONDS", 0.3)

    monkeypatch.setattr(akshare_apis, "record_probe_result", lambda *a, **kw: None)
    mock_db = MagicMock()

    with caplog.at_level(logging.WARNING, logger="app.api.routes.akshare_apis"):
        asyncio.run(akshare_apis.probe_api("test_api", mock_db))

    warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("TIMEOUT" in r.getMessage() for r in warning_logs), \
        "超时应有 WARNING 日志含 'TIMEOUT'"
