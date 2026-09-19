"""白盒测试 - discovery 任务超时机制集成验证（P0 稳定性回归）。

守护：
1. _sync_one_symbol_with_timeout 在 _sync_one_symbol 阻塞时抛出 TimeoutError
2. _sync_one_symbol_with_timeout 在 _sync_one_symbol 正常返回时透传结果
3. _watchdog_heartbeat 在任务进入终态时立即退出
4. _watchdog_heartbeat 在任务运行时更新 updated_at（心跳）

这些是 P0 稳定性修复的核心：防止单 symbol 卡死阻塞整个任务 +
watchdog 心跳防止 _expire_stale_tasks 误判正常任务为 stale。
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. _sync_one_symbol_with_timeout 超时机制
# ============================================================================

def test_sync_one_symbol_with_timeout_raises_on_block(monkeypatch):
    """【P0 稳定性回归】mock _sync_one_symbol 阻塞 → 90s 超时后抛 TimeoutError。

    场景：数据源接受连接不响应，_sync_one_symbol 内部 akshare 调用永久卡死。
    修复：_sync_one_symbol_with_timeout 用 ThreadPoolExecutor + future.result(timeout=)
    包装，超时后跳过该 symbol，继续下一个。
    """
    from app.services import discovery_tasks

    # 调小超时到 0.5s（避免测试等待 90s）
    monkeypatch.setattr(discovery_tasks, "SYNC_ONE_SYMBOL_TIMEOUT_SECONDS", 0.5)

    # v2 修改：子线程使用独立 Session，需 mock DatabaseManager
    mock_session = MagicMock()
    mock_db_manager = MagicMock()
    mock_db_manager.session_factory = lambda: mock_session
    monkeypatch.setattr("app.db.manager.DatabaseManager.get", lambda: mock_db_manager)

    # mock _sync_one_symbol 阻塞 2s（远超 0.5s 超时，但 with 块退出不会太久）
    def blocking_sync(*args, **kwargs):
        time.sleep(2)
        return ({"status": "ok"}, False)

    monkeypatch.setattr(discovery_tasks, "_sync_one_symbol", blocking_sync)

    # 调用应抛 TimeoutError（concurrent.futures.TimeoutError 在 Python 3.11+ 等同于 TimeoutError）
    # 注意：v2 在函数内添加了 symbol.symbol 日志，需传 mock symbol 而非 None
    mock_symbol = MagicMock()
    mock_symbol.symbol = "TEST"
    with pytest.raises((TimeoutError, concurrent.futures.TimeoutError)):
        discovery_tasks._sync_one_symbol_with_timeout(
            db=None, symbol=mock_symbol, payload=None, portfolio_id=None
        )


def test_sync_one_symbol_with_timeout_returns_on_success(monkeypatch):
    """【P0 稳定性回归】mock _sync_one_symbol 立即返回 → 正常透传结果，不超时。

    场景：_sync_one_symbol 正常完成（数据源响应正常）。
    验证：超时包装不影响正常调用，结果正确透传。
    """
    from app.services import discovery_tasks

    # v2 修改：子线程使用独立 Session，需 mock DatabaseManager
    mock_session = MagicMock()
    mock_db_manager = MagicMock()
    mock_db_manager.session_factory = lambda: mock_session
    monkeypatch.setattr("app.db.manager.DatabaseManager.get", lambda: mock_db_manager)

    expected_result = ({"status": "ok", "symbol": "TEST"}, True)

    def quick_sync(*args, **kwargs):
        return expected_result

    monkeypatch.setattr(discovery_tasks, "_sync_one_symbol", quick_sync)

    # 注意：v2 在函数内添加了 symbol.symbol 日志，需传 mock symbol 而非 None
    mock_symbol = MagicMock()
    mock_symbol.symbol = "TEST"
    result = discovery_tasks._sync_one_symbol_with_timeout(
        db=None, symbol=mock_symbol, payload=None, portfolio_id=None
    )
    assert result == expected_result, "正常调用应透传结果"


# ============================================================================
# 2. _watchdog_heartbeat 心跳机制
# ============================================================================

def test_watchdog_heartbeat_exits_on_terminal_state(monkeypatch):
    """【P0 稳定性回归】mock 条件 UPDATE rowcount=0 + db.get 返回终态 task → watchdog 退出。

    风控加固后 watchdog 改为条件 UPDATE 只更新 updated_at 字段（避免整行覆盖 worker 写入）：
    WHERE status NOT IN 终态 AND stage != 'prepare'。终态任务 UPDATE rowcount=0，
    再 db.get 查状态判断是否该退出。

    场景：任务进入终态（done/failed/cancelled/expired），watchdog 不应继续心跳。
    验证：watchdog 检测到终态后立即 return，线程退出。
    """
    from app.services import discovery_tasks

    # 调小心跳间隔到 0.1s（避免测试等待 30s）
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    # mock task 为终态
    mock_task = MagicMock()
    mock_task.status = "done"

    # mock db.execute 返回 rowcount=0（因 status=done 不符合 WHERE 条件）
    mock_result = MagicMock()
    mock_result.rowcount = 0
    mock_db = MagicMock()
    mock_db.execute.return_value = mock_result
    mock_db.get.return_value = mock_task

    monkeypatch.setattr(discovery_tasks, "SessionLocal", lambda: mock_db)

    stop_event = threading.Event()
    t = threading.Thread(
        target=discovery_tasks._watchdog_heartbeat,
        args=("test-task-done", stop_event),
        daemon=True,
    )
    t.start()

    # watchdog 应在第一次心跳（0.1s）后检测到终态并退出
    t.join(timeout=2.0)
    assert not t.is_alive(), "watchdog 应在检测到终态后立即退出"


def test_watchdog_heartbeat_updates_updated_at(monkeypatch):
    """【P0 稳定性回归】风控加固后 watchdog 用条件 UPDATE 更新 updated_at 字段。

    场景：任务 running 中，watchdog 每 30s 用条件 UPDATE 更新 updated_at 防止
    _expire_stale_tasks 误判。条件 UPDATE 避免整行覆盖 worker 写入的 status/stage。

    验证：watchdog 执行心跳后，db.execute 被调用（执行 UPDATE）+ db.commit 被调用。
    """
    from app.services import discovery_tasks

    # 调小心跳间隔到 0.1s
    monkeypatch.setattr(discovery_tasks, "_WATCHDOG_HEARTBEAT_SECONDS", 0.1)

    # mock UPDATE rowcount=1（表示成功更新 1 行，任务非终态非 prepare）
    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_db = MagicMock()
    mock_db.execute.return_value = mock_result

    monkeypatch.setattr(discovery_tasks, "SessionLocal", lambda: mock_db)

    stop_event = threading.Event()
    t = threading.Thread(
        target=discovery_tasks._watchdog_heartbeat,
        args=("test-task-running", stop_event),
        daemon=True,
    )
    t.start()

    # 等待足够时间让 watchdog 执行至少一次心跳（0.1s 间隔 + 余量）
    time.sleep(0.5)
    stop_event.set()
    t.join(timeout=2.0)

    # 验证 db.execute 被调用（执行条件 UPDATE）
    assert mock_db.execute.called, "watchdog 应调用 db.execute 执行条件 UPDATE"
    # 验证 db.commit 被调用
    assert mock_db.commit.called, "watchdog 应调用 db.commit"
    # 验证 rowcount=1 表示成功更新
    assert mock_result.rowcount == 1, "条件 UPDATE 应成功更新 1 行"
