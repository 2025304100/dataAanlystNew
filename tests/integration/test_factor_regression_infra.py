"""Task 1: 因子中心评价全面回归 - 基础设施验证测试 (TR-1.1 ~ TR-1.3)。"""
from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# TR-1.1: TestClient + 路由挂载验证
# ══════════════════════════════════════════════════════════════════════════════

def test_TR_1_1_test_client_and_routes(client, app):
    """验证 TestClient 可用，factor-evaluation 两个核心端点返回 200。"""
    resp_runs = client.get("/factor-evaluation/runs")
    assert resp_runs.status_code in {200}, (
        f"GET /factor-evaluation/runs expected 200, got {resp_runs.status_code}. "
        f"body={resp_runs.text[:500]}"
    )

    resp_tasks = client.get("/factor-evaluation/tasks")
    assert resp_tasks.status_code == 200, (
        f"GET /factor-evaluation/tasks expected 200, got {resp_tasks.status_code}. "
        f"body={resp_tasks.text[:500]}"
    )

    fe_paths = sorted([
        getattr(r, "path", "") for r in app.routes
        if isinstance(getattr(r, "path", ""), str) and "/factor-evaluation/" in getattr(r, "path", "")
    ])
    print("\n[TR-1.1] factor-evaluation 相关路由 path 列表:")
    for p in fe_paths:
        print(f"  - {p}")

    expected_suffixes = {
        "/factor-evaluation/preflight",
        "/factor-evaluation/tasks",
        "/factor-evaluation/tasks/{task_id}",
        "/factor-evaluation/tasks/{task_id}/cancel",
        "/factor-evaluation/runs",
        "/factor-evaluation/runs/{run_id}",
    }
    actual_suffixes = set()
    for p in fe_paths:
        for suf in expected_suffixes:
            if p.endswith(suf):
                actual_suffixes.add(suf)
    missing = expected_suffixes - actual_suffixes
    assert not missing, f"缺失的路由 suffix={missing}, 当前已挂载={fe_paths}"


# ══════════════════════════════════════════════════════════════════════════════
# TR-1.2: isolated_db_session 事务隔离验证
# ══════════════════════════════════════════════════════════════════════════════

def test_TR_1_2_test_isolated_db_session(isolated_db_session):
    """验证 isolated_db_session rollback 后数据完全隔离（count=0）。"""
    from app.models.async_task import AsyncTaskRecord

    session = isolated_db_session

    t1 = AsyncTaskRecord(
        id="tr12-task-001",
        task_type="wp5_evaluation",
        status="queued",
        stage="prepare",
        percent=0,
        message="",
        total=0,
        processed=0,
        ok_count=0,
        failed_count=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    t2 = AsyncTaskRecord(
        id="tr12-task-002",
        task_type="wp5_evaluation",
        status="done",
        stage="done",
        percent=100,
        message="ok",
        total=10,
        processed=10,
        ok_count=10,
        failed_count=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add_all([t1, t2])
    session.flush()

    from sqlalchemy import select
    count_after_add = session.execute(
        select(AsyncTaskRecord).where(AsyncTaskRecord.id.in_(["tr12-task-001", "tr12-task-002"]))
    ).scalars().all()
    assert len(count_after_add) == 2, f"flush 后应能查到 2 条，实际 {len(count_after_add)}"

    session.rollback()

    count_after_rollback = session.execute(
        select(AsyncTaskRecord).where(AsyncTaskRecord.id.in_(["tr12-task-001", "tr12-task-002"]))
    ).scalars().all()
    assert len(count_after_rollback) == 0, (
        f"rollback 后 count 应为 0，实际 {len(count_after_rollback)}，证明 session 隔离未生效"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TR-1.3: wait_for_task_status 成功路径验证
# ══════════════════════════════════════════════════════════════════════════════

def test_TR_1_3_test_wait_for_task_status_success(monkeypatch):
    """验证 wait_for_task_status 能在状态轮询变化后成功返回且不抛 TimeoutError。"""
    from tests.integration.conftest import wait_for_task_status

    call_count = {"n": 0}
    start_ts = {"t": None}

    def fake_get_evaluation_task(task_id: str):
        call_count["n"] += 1
        if start_ts["t"] is None:
            start_ts["t"] = time.monotonic()
        elapsed = time.monotonic() - start_ts["t"]
        if call_count["n"] == 1:
            return {"task_id": task_id, "status": "processing", "percent": 50}
        if elapsed >= 0.5 or call_count["n"] >= 3:
            return {"task_id": task_id, "status": "done", "percent": 100}
        return {"task_id": task_id, "status": "processing", "percent": 50 + call_count["n"] * 10}

    from app.services.factors import wp5_eval_task
    monkeypatch.setattr(wp5_eval_task, "get_evaluation_task", fake_get_evaluation_task)

    t0 = time.monotonic()
    result = wait_for_task_status("abc", {"done"}, timeout=5, poll_interval=0.2)
    elapsed = time.monotonic() - t0

    assert result is not None, "wait_for_task_status 不应返回 None"
    status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    assert status == "done", f"最终 status 应为 'done'，实际 {status}"
    assert elapsed < 1.0, (
        f"应在 1 秒内完成轮询（预计 0.5s 左右），实际耗时 {elapsed:.3f}s"
    )

    with pytest.raises(TimeoutError) as exc_info:
        call_count["n"] = 0
        start_ts["t"] = None

        def always_processing(task_id: str):
            return {"task_id": task_id, "status": "processing", "percent": 99}

        monkeypatch.setattr(wp5_eval_task, "get_evaluation_task", always_processing)
        wait_for_task_status("xyz", {"done"}, timeout=0.5, poll_interval=0.1)

    assert "Task xyz" in str(exc_info.value), "TimeoutError 信息应包含 task_id"
    assert "last status=processing" in str(exc_info.value), (
        "TimeoutError 信息应包含 last status"
    )
