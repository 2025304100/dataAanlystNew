"""白盒测试 - 任务中心 API (P1-3)。

覆盖：
1. app/api/routes/market_data.py 中的异步任务端点
   - list_sync_tasks / get_sync_task / cancel_sync_task
2. app/api/routes/system.py 中的 list_unified_tasks
   - 聚合 async_tasks + discovery_tasks 表

异步任务端点不依赖 Depends(get_db)，需要 mock 服务层
（list_async_tasks / get_async_task / cancel_async_task）。
list_unified_tasks 走 db: Session = Depends(get_db)，可直接传 db_session。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.api.routes.market_data import (
    cancel_sync_task,
    get_sync_task,
    list_sync_tasks,
)
from app.api.routes.system import list_unified_tasks
from app.models.async_task import AsyncTaskRecord
from app.models.discovery import DiscoveryTaskRecord
from app.schemas.async_task import AsyncTaskRead

pytestmark = pytest.mark.whitebox


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_async_task_read(task_id: str = "task-1") -> AsyncTaskRead:
    """构造一个 AsyncTaskRead 用于 mock 服务层返回。"""
    return AsyncTaskRead(
        id=task_id,
        task_type="market_data_sync",
        status="done",
        stage="done",
        percent=100.0,
        message="synced",
        total=10,
        processed=10,
        ok_count=10,
        failed_count=0,
        current_item=None,
        result={"synced": 10},
        errors=[],
        created_at=_now(),
        started_at=_now(),
        finished_at=_now(),
    )


# ============================================================================
# 1. GET /market-data/sync-tasks 列表（mock 服务层）
# ============================================================================

def test_list_sync_tasks_returns_async_task_read_fields():
    """【P1-3 API 测试】list_sync_tasks 返回 list[dict]，字段完整。"""
    fake_task = _make_async_task_read(task_id="qa-list-1")
    with patch(
        "app.api.routes.market_data.list_async_tasks",
        return_value=[fake_task],
    ):
        result = list_sync_tasks(limit=10)
    assert isinstance(result, list)
    assert len(result) == 1
    item = result[0]
    # 验证 model_dump() 后字段齐全（前端依赖）
    for key in (
        "id", "task_type", "status", "stage", "percent", "message",
        "total", "processed", "ok_count", "failed_count",
        "current_item", "result", "errors",
        "created_at", "started_at", "finished_at",
    ):
        assert key in item, f"AsyncTaskRead 缺少字段 {key}"
    assert item["id"] == "qa-list-1"
    assert item["task_type"] == "market_data_sync"
    assert item["status"] == "done"
    assert item["percent"] == 100.0
    assert item["result"] == {"synced": 10}


def test_list_sync_tasks_passes_limit_to_service():
    """【P1-3 API 测试】list_sync_tasks 应把 limit 透传给服务层。"""
    with patch(
        "app.api.routes.market_data.list_async_tasks",
        return_value=[],
    ) as mock_fn:
        list_sync_tasks(limit=25)
    assert mock_fn.call_args.kwargs["limit"] == 25
    assert mock_fn.call_args.kwargs["task_type"] == "market_data_sync"


def test_list_sync_tasks_empty_returns_empty_list():
    """【P1-3 API 测试】服务层返回空列表时路由也应返回空列表。"""
    with patch(
        "app.api.routes.market_data.list_async_tasks",
        return_value=[],
    ):
        result = list_sync_tasks(limit=10)
    assert result == []


# ============================================================================
# 2. GET /market-data/sync-tasks/{task_id} 单条
# ============================================================================

def test_get_sync_task_success_returns_dict():
    """【P1-3 API 测试】get_sync_task 找到任务时返回 dict。"""
    fake_task = _make_async_task_read(task_id="qa-get-1")
    with patch(
        "app.api.routes.market_data.get_async_task",
        return_value=fake_task,
    ):
        result = get_sync_task("qa-get-1")
    assert result["id"] == "qa-get-1"
    assert result["status"] == "done"


def test_get_sync_task_not_found_raises_404():
    """【P1-3 API 测试】get_sync_task 找不到时抛 HTTPException 404。"""
    with patch(
        "app.api.routes.market_data.get_async_task",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc:
            get_sync_task("not-exist")
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


# ============================================================================
# 3. POST /market-data/sync-tasks/{task_id}/cancel
# ============================================================================

def test_cancel_sync_task_not_found_raises_404():
    """【P1-3 API 测试】cancel_sync_task 服务层抛 ValueError 时路由转 404。"""
    with patch(
        "app.api.routes.market_data.cancel_async_task",
        side_effect=ValueError("Async task not found"),
    ):
        with pytest.raises(HTTPException) as exc:
            cancel_sync_task("not-exist")
    assert exc.value.status_code == 404
    assert "not found" in exc.value.detail.lower()


def test_cancel_sync_task_success_returns_dict():
    """【P1-3 API 测试】cancel_sync_task 成功时返回 task dict。"""
    fake_task = _make_async_task_read(task_id="qa-cancel-1")
    fake_task.status = "cancelled"
    fake_task.stage = "cancelled"
    with patch(
        "app.api.routes.market_data.cancel_async_task",
        return_value=fake_task,
    ):
        result = cancel_sync_task("qa-cancel-1")
    assert result["id"] == "qa-cancel-1"
    assert result["status"] == "cancelled"


# ============================================================================
# 4. GET /system/tasks 统一任务聚合
# ============================================================================

def test_list_unified_tasks_aggregates_async_and_discovery(db_session):
    """【P1-3 API 测试】list_unified_tasks 聚合 async_tasks 和 discovery_tasks。"""
    # 插入一条 AsyncTaskRecord
    db_session.add(AsyncTaskRecord(
        id="qa-async-1",
        task_type="market_data_sync",
        status="done",
        stage="done",
        percent=100.0,
        message="ok",
        total=10,
        processed=10,
        ok_count=10,
        failed_count=0,
        current_item=None,
        payload_json='{"scope": "all"}',
        result_json='{"synced": 10}',
        errors_json='[]',
        created_at=_now(),
        started_at=_now(),
        finished_at=_now(),
        updated_at=_now(),
    ))
    # 插入一条 DiscoveryTaskRecord
    db_session.add(DiscoveryTaskRecord(
        id="qa-disc-1",
        status="done",
        stage="done",
        percent=100.0,
        message="ok",
        scope="actionable",
        min_score=55,
        include_news=1,
        total=20,
        processed=20,
        ok_count=20,
        failed_count=0,
        empty_count=0,
        scored_count=15,
        batch_size=20,
        delay_seconds=0.25,
        adaptive_delay_seconds=0.25,
        executable_count=8,
        cleanup_count=0,
        news_symbols_total=0,
        errors_json='[]',
        payload_json='{"scope": "actionable"}',
        created_at=_now(),
        started_at=_now(),
        finished_at=_now(),
        updated_at=_now(),
    ))
    db_session.commit()

    result = list_unified_tasks(task_type=None, limit=30, db=db_session)
    assert "tasks" in result
    items = result["tasks"]
    assert len(items) == 2

    sources = {item["source"] for item in items}
    assert sources == {"async", "discovery"}

    # 验证 async 项字段
    async_item = next(i for i in items if i["source"] == "async")
    assert async_item["id"] == "qa-async-1"
    assert async_item["task_type"] == "market_data_sync"
    assert async_item["payload"] == {"scope": "all"}
    assert async_item["result"] == {"synced": 10}
    assert async_item["errors"] == []
    assert async_item["duration_sec"] is not None  # started_at + finished_at 都有

    # 验证 discovery 项字段
    disc_item = next(i for i in items if i["source"] == "discovery")
    assert disc_item["id"] == "qa-disc-1"
    assert disc_item["task_type"] == "discovery_mining"
    assert disc_item["payload"]["scope"] == "actionable"
    assert disc_item["result"]["executable_count"] == 8
    assert disc_item["current_item"] is None  # DiscoveryTaskRecord.current_symbol


def test_list_unified_tasks_filter_by_task_type(db_session):
    """【P1-3 API 测试】list_unified_tasks 支持 task_type 过滤。"""
    db_session.add(AsyncTaskRecord(
        id="qa-async-2",
        task_type="market_data_sync",
        status="done",
        stage="done",
        percent=100.0,
        total=0,
        created_at=_now(),
        updated_at=_now(),
    ))
    db_session.add(DiscoveryTaskRecord(
        id="qa-disc-2",
        status="done",
        stage="done",
        percent=100.0,
        scope="actionable",
        min_score=55,
        total=0,
        created_at=_now(),
        updated_at=_now(),
    ))
    db_session.commit()

    result = list_unified_tasks(task_type="discovery_mining", limit=30, db=db_session)
    items = result["tasks"]
    assert len(items) == 1
    assert items[0]["source"] == "discovery"
    assert items[0]["task_type"] == "discovery_mining"


def test_list_unified_tasks_invalid_json_falls_back_empty(db_session):
    """【P1-3 API 测试】payload_json/result_json/errors_json 解析失败时静默降级。"""
    db_session.add(AsyncTaskRecord(
        id="qa-async-bad",
        task_type="market_data_sync",
        status="failed",
        stage="failed",
        percent=50.0,
        total=10,
        processed=5,
        payload_json="{not valid json",
        result_json="not-json-either",
        errors_json="[also not json",
        created_at=_now(),
        updated_at=_now(),
    ))
    db_session.commit()

    result = list_unified_tasks(task_type=None, limit=30, db=db_session)
    items = result["tasks"]
    assert len(items) == 1
    item = items[0]
    assert item["payload"] == {}
    assert item["result"] == {}
    assert item["errors"] == []
