"""白盒测试 - universe 刷新与 watchdog 心跳稳定性守护 (P0 回归)。

验证：
1. _refresh_universe_with_timeout 的超时保护（防 prepare 阶段永久阻塞）
2. _refresh_cn_etf_universe 的 call_akshare_with_retry 包装
3. _watchdog_heartbeat 的阶段感知心跳（prepare 阶段不更新 updated_at）
4. watchdog 异常日志（替换静默吞）

这是 P0 稳定性修复的核心守护测试，防止：
- universe 刷新路径 timeout 被移除 → 全量同步卡在 prepare 阶段
- watchdog 在 prepare 阶段更新 updated_at → 掩盖卡死，_expire_stale_tasks 永不触发
- watchdog 异常被静默吞 → 问题不可观测
"""
from __future__ import annotations

import logging
import time
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services import discovery_tasks

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. _refresh_universe_with_timeout 超时保护
# ============================================================================

def test_refresh_universe_with_timeout_returns_none_on_block(monkeypatch):
    """【P0 稳定性回归】mock _refresh_discovery_universe 阻塞 → 120s 超时返回 None。

    场景：akshare 内部 pd.read_excel 永久阻塞，_refresh_discovery_universe 卡死。
    期望：_refresh_universe_with_timeout 在 UNIVERSE_REFRESH_TIMEOUT_SECONDS 后返回 None，
         不阻塞主流程，调用方标记 task=failed。
    """
    # 用极短超时加速测试
    # 注意：被测代码用 `with ThreadPoolExecutor` 块，超时后 __exit__ 会等待子线程完成。
    # 因此 block_sleep 必须略大于 timeout 但小于总时限，确保 with 块退出不会太久。
    monkeypatch.setattr(discovery_tasks, "UNIVERSE_REFRESH_TIMEOUT_SECONDS", 0.5)

    def _block_forever(db, payload):
        time.sleep(1.0)  # 模拟阻塞（略大于 timeout，但 with 块退出最多等 0.5s）
        return {"seen": 100, "created": 50}

    monkeypatch.setattr(discovery_tasks, "_refresh_discovery_universe", _block_forever)

    from app.schemas.discovery import DiscoveryTaskCreate
    payload = DiscoveryTaskCreate(
        scope="cn-stock",
        refresh_universe=True,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    start = time.time()
    result = discovery_tasks._refresh_universe_with_timeout(MagicMock(), payload)
    elapsed = time.time() - start

    assert result is None, "超时应返回 None，让调用方标记 task=failed"
    assert elapsed < 2.0, f"应在 0.5s 超时，实际耗时 {elapsed:.1f}s"


def test_refresh_universe_with_timeout_passes_through_on_success(monkeypatch):
    """【P0 稳定性回归】正常返回时透传结果。"""
    monkeypatch.setattr(discovery_tasks, "UNIVERSE_REFRESH_TIMEOUT_SECONDS", 5)

    expected = {"seen": 100, "created": 50}
    monkeypatch.setattr(
        discovery_tasks,
        "_refresh_discovery_universe",
        lambda db, payload: expected,
    )

    from app.schemas.discovery import DiscoveryTaskCreate
    payload = DiscoveryTaskCreate(
        scope="cn-stock",
        refresh_universe=True,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    result = discovery_tasks._refresh_universe_with_timeout(MagicMock(), payload)
    assert result == expected


# ============================================================================
# 2. _refresh_cn_etf_universe 重试包装
# ============================================================================

def test_refresh_cn_etf_universe_wraps_fund_etf_spot_em_with_retry(monkeypatch, db_session):
    """【P0 稳定性回归】_refresh_cn_etf_universe 应通过 call_akshare_with_retry 包装 fund_etf_spot_em。

    防止直接调用 ak.fund_etf_spot_em() 在 akshare 内部永久阻塞时无 timeout 保护。
    """
    call_records = []

    def _mock_call_akshare_with_retry(func, *args, **kwargs):
        call_records.append({
            "func_name": getattr(func, "__name__", "unknown"),
            "api_key": kwargs.get("api_key"),
            "max_attempts": kwargs.get("max_attempts"),
        })
        # 返回空 DataFrame 模拟成功
        import pandas as pd
        return pd.DataFrame()

    monkeypatch.setattr(
        discovery_tasks,
        "call_akshare_with_retry",
        _mock_call_akshare_with_retry,
    )

    discovery_tasks._refresh_cn_etf_universe(db_session)

    assert len(call_records) >= 1, "应调用 call_akshare_with_retry"
    assert call_records[0]["api_key"] == "fund_etf_spot_em"
    assert call_records[0]["max_attempts"] == 2


def test_refresh_cn_etf_universe_falls_back_to_sina_on_failure(monkeypatch, db_session):
    """【P0 稳定性回归】主源 fund_etf_spot_em 失败后应回退到 fund_etf_category_sina。"""
    call_records = []

    def _mock_call_akshare_with_retry(func, *args, **kwargs):
        func_name = getattr(func, "__name__", "unknown")
        call_records.append({"func_name": func_name, "api_key": kwargs.get("api_key")})
        if func_name == "fund_etf_spot_em":
            raise RuntimeError("simulated failure")
        # sina 备用源返回空 DataFrame
        import pandas as pd
        return pd.DataFrame()

    monkeypatch.setattr(
        discovery_tasks,
        "call_akshare_with_retry",
        _mock_call_akshare_with_retry,
    )

    discovery_tasks._refresh_cn_etf_universe(db_session)

    # 应该尝试了两个数据源
    func_names = [r["func_name"] for r in call_records]
    assert "fund_etf_spot_em" in func_names, "应先尝试主源"
    assert "fund_etf_category_sina" in func_names, "主源失败后应回退到 sina"


# ============================================================================
# 3. watchdog 阶段感知心跳
# ============================================================================

def test_watchdog_skips_heartbeat_in_prepare_stage(monkeypatch):
    """【P0 稳定性回归】prepare 阶段 watchdog 不应更新 updated_at。

    场景：任务在 prepare 阶段（universe 刷新）卡死。
    期望：watchdog 不更新 updated_at，让 _expire_stale_tasks（10min 阈值）能兜底中断。
    若 watchdog 在 prepare 阶段更新 updated_at，会掩盖卡死，任务永不中断。
    """
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    # mock task 在 prepare 阶段
    mock_task = MagicMock()
    mock_task.status = "running"
    mock_task.stage = "prepare"
    mock_task.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15)

    mock_db = MagicMock()
    mock_db.get.return_value = mock_task

    # 用 set() 让 while 循环只跑一次就退出
    stop_event = threading.Event()
    stop_event.set()  # 预先设置，让 wait 立即返回 True，循环不执行
    # 但为了测试，我们需要让循环执行一次。用未设置的 event，然后在循环内设置。
    stop_event = threading.Event()

    # 0.1s 后设置 stop_event，让 watchdog 跑一轮就退出
    def _set_stop():
        time.sleep(0.15)
        stop_event.set()

    setter = threading.Thread(target=_set_stop, daemon=True)
    setter.start()

    # mock SessionLocal 返回 mock_db
    monkeypatch.setattr(discovery_tasks, "SessionLocal", lambda: mock_db)

    discovery_tasks._watchdog_heartbeat("test-task-id", stop_event)
    setter.join(timeout=1.0)

    # 验证 updated_at 未被更新（commit 未被调用）
    assert not mock_db.commit.called, "prepare 阶段 watchdog 不应 commit（不更新 updated_at）"


def test_watchdog_updates_heartbeat_in_sync_stage(monkeypatch):
    """【P0 稳定性回归】sync 阶段 watchdog 应正常更新 updated_at。

    场景：任务在 sync 阶段（symbol 循环）正常运行。
    期望：watchdog 每 30s 更新 updated_at，防止 _expire_stale_tasks 误判。
    """
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    mock_task = MagicMock()
    mock_task.status = "running"
    mock_task.stage = "sync"
    mock_task.updated_at = old_time

    mock_db = MagicMock()
    mock_db.get.return_value = mock_task

    stop_event = threading.Event()

    def _set_stop():
        time.sleep(0.15)
        stop_event.set()

    setter = threading.Thread(target=_set_stop, daemon=True)
    setter.start()

    monkeypatch.setattr(discovery_tasks, "SessionLocal", lambda: mock_db)

    discovery_tasks._watchdog_heartbeat("test-task-id", stop_event)
    setter.join(timeout=1.0)

    # 验证 updated_at 被更新（commit 被调用）
    assert mock_db.commit.called, "sync 阶段 watchdog 应 commit（更新 updated_at）"
    # updated_at 应被设置为 _now()
    assert mock_task.updated_at > old_time, "updated_at 应被更新为新值"


def test_watchdog_logs_failure_instead_of_silent_swallow(monkeypatch, caplog):
    """【P0 稳定性回归】watchdog 异常应记录 WARNING 日志，而非静默吞。

    场景：watchdog 心跳时 DB 异常（如连接池耗尽）。
    期望：记录 WARNING 级别日志，便于诊断；不抛异常影响主流程。
    历史问题：原实现 except: pass 静默吞，watchdog 失败后 updated_at 不更新但无日志。
    """
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    # mock SessionLocal 抛异常
    def _raise_exception():
        raise RuntimeError("simulated DB error")

    monkeypatch.setattr(discovery_tasks, "SessionLocal", _raise_exception)

    stop_event = threading.Event()

    def _set_stop():
        time.sleep(0.15)
        stop_event.set()

    setter = threading.Thread(target=_set_stop, daemon=True)
    setter.start()

    with caplog.at_level(logging.WARNING, logger="app.services.discovery_tasks"):
        # 不应抛异常
        discovery_tasks._watchdog_heartbeat("test-task-id", stop_event)
    setter.join(timeout=1.0)

    # 验证有 WARNING 日志
    warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("heartbeat failed" in r.getMessage() for r in warning_logs), \
        "watchdog 异常应记录 WARNING 日志含 'heartbeat failed'"
