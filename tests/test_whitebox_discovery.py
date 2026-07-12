"""白盒测试 - 机会挖掘任务 (app.services.discovery_tasks)。

覆盖 P2 改动：
- _task_to_dict 序列化完整性（updated_at / can_retry / cleanup_count）
- retry_discovery_task 终态校验、并发保护、断点续扫、errors 清空
- DiscoveryResultState 僵尸表已移除（import 不应可用）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.discovery import DiscoveryTaskRecord
from app.services import discovery_tasks

pytestmark = pytest.mark.whitebox


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_task(
    db_session,
    *,
    status="failed",
    stage="failed",
    percent=100.0,
    processed_ids=None,
    errors=None,
    payload_json=None,
) -> DiscoveryTaskRecord:
    """创建一个 discovery 任务记录用于测试。"""
    task = DiscoveryTaskRecord(
        id=f"qa-disc-{id(db_session)}-{status}-{_now().timestamp()}",
        status=status,
        stage=stage,
        percent=percent,
        message="test",
        scope="cn-stock",
        min_score=55,
        include_news=1,
        total=10,
        processed=len(processed_ids or []),
        ok_count=5,
        failed_count=2,
        empty_count=1,
        scored_count=4,
        batch_size=20,
        delay_seconds=0.25,
        adaptive_delay_seconds=0.25,
        payload_json=payload_json or json.dumps({
            "scope": "cn-stock",
            "min_score": 55,
            "include_news": True,
            "batch_size": 20,
            "delay_seconds": 0.25,
            "symbol_limit": None,
            "warning_days": 3,
            "valid_days": 5,
        }),
        processed_symbol_ids_json=json.dumps(sorted(processed_ids or [])),
        synced_symbol_ids_json="[]",
        errors_json=json.dumps(errors or []),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


# ---------- _task_to_dict 序列化 ----------

def test_task_to_dict_includes_updated_at(db_session):
    """_task_to_dict 应返回 updated_at 字段（P2-2 修复）。"""
    task = _create_task(db_session, status="done")
    d = discovery_tasks._task_to_dict(task)
    assert "updated_at" in d, "_task_to_dict 必须返回 updated_at"


def test_task_to_dict_includes_can_retry(db_session):
    """_task_to_dict 应返回 can_retry 字段。"""
    task = _create_task(db_session, status="failed")
    d = discovery_tasks._task_to_dict(task)
    assert "can_retry" in d
    assert d["can_retry"] is True, "failed 状态的 can_retry 应为 True"


def test_task_to_dict_can_retry_false_for_done(db_session):
    """done 状态的 can_retry 应为 False。"""
    task = _create_task(db_session, status="done", stage="done")
    d = discovery_tasks._task_to_dict(task)
    assert d["can_retry"] is False


def test_task_to_dict_can_retry_true_for_cancelled(db_session):
    """cancelled 状态的 can_retry 应为 True。"""
    task = _create_task(db_session, status="cancelled", stage="cancelled")
    d = discovery_tasks._task_to_dict(task)
    assert d["can_retry"] is True


def test_task_to_dict_can_retry_true_for_expired(db_session):
    """expired 状态的 can_retry 应为 True。"""
    task = _create_task(db_session, status="expired", stage="failed")
    d = discovery_tasks._task_to_dict(task)
    assert d["can_retry"] is True


def test_task_to_dict_includes_cleanup_count():
    """_task_to_dict 应返回 cleanup_count 字段。"""
    task = DiscoveryTaskRecord(
        id="qa-cleanup-count",
        status="done",
        stage="done",
        percent=100.0,
        message="test",
        scope="cn-stock",
        min_score=55,
        include_news=1,
        total=0,
        processed=0,
        ok_count=0,
        failed_count=0,
        empty_count=0,
        scored_count=0,
        batch_size=20,
        delay_seconds=0.25,
        adaptive_delay_seconds=0.25,
        payload_json="{}",
        processed_symbol_ids_json="[]",
        synced_symbol_ids_json="[]",
        errors_json="[]",
    )
    d = discovery_tasks._task_to_dict(task)
    assert "cleanup_count" in d


# ---------- DiscoveryResultState 僵尸表移除 ----------

def test_discovery_result_state_removed_from_models():
    """P2-1: DiscoveryResultState 应已从 app.models.discovery 移除。"""
    from app.models import discovery as discovery_mod
    assert not hasattr(discovery_mod, "DiscoveryResultState"), \
        "DiscoveryResultState 僵尸表应已删除"


def test_discovery_cleanup_no_longer_imports_state():
    """P2-1: discovery_cleanup 不应再引用 DiscoveryResultState。"""
    import app.services.discovery_cleanup as mod
    source = open(mod.__file__, encoding="utf-8").read()
    assert "DiscoveryResultState" not in source, \
        "discovery_cleanup.py 不应再引用 DiscoveryResultState"


# ---------- retry_discovery_task ----------

def test_retry_failed_task_resets_to_queued(db_session, monkeypatch):
    """retry 已失败任务应重置为 queued 并保留 processed_ids。"""
    # 模拟 _start_worker 避免真实启动线程
    started = []
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: started.append(tid))

    processed = [101, 102, 103]
    task = _create_task(db_session, status="failed", processed_ids=processed,
                        errors=[{"scope": "task", "error": "boom"}])

    result = discovery_tasks.retry_discovery_task(task.id)

    assert result["status"] == "queued"
    assert result["stage"] == "queued"
    assert result["percent"] == 0
    assert result["message"] == "任务已重试，等待后台继续（保留已处理进度）"
    assert started == [task.id], "应启动 worker"
    # 断点续扫：processed_ids 保留；errors 清空
    # expire_all 丢弃 db_session 缓存，强制从数据库重新加载（retry 通过独立 session commit）
    db_session.expire_all()
    db_task = db_session.get(DiscoveryTaskRecord, task.id)
    assert json.loads(db_task.processed_symbol_ids_json) == sorted(processed)
    assert json.loads(db_task.errors_json) == []


def test_retry_cancelled_task(db_session, monkeypatch):
    """retry 已取消任务应成功。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    task = _create_task(db_session, status="cancelled", stage="cancelled")
    result = discovery_tasks.retry_discovery_task(task.id)
    assert result["status"] == "queued"


def test_retry_expired_task(db_session, monkeypatch):
    """retry 已过期任务应成功。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    task = _create_task(db_session, status="expired", stage="failed")
    result = discovery_tasks.retry_discovery_task(task.id)
    assert result["status"] == "queued"


def test_retry_running_task_returns_original(db_session, monkeypatch):
    """retry 运行中任务应返回原状态（不报错，不重启 worker）。"""
    started = []
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: started.append(tid))
    task = _create_task(db_session, status="running", stage="sync", percent=50.0)
    result = discovery_tasks.retry_discovery_task(task.id)
    assert result["status"] == "running", "非终态任务 retry 应返回原状态"
    assert started == [], "不应启动 worker"


def test_retry_paused_task_returns_original(db_session, monkeypatch):
    """retry 暂停任务应返回原状态。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    task = _create_task(db_session, status="paused", stage="paused", percent=60.0)
    result = discovery_tasks.retry_discovery_task(task.id)
    assert result["status"] == "paused"


def test_retry_done_task_returns_original(db_session, monkeypatch):
    """retry 已完成任务应返回原状态。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    task = _create_task(db_session, status="done", stage="done", percent=100.0)
    result = discovery_tasks.retry_discovery_task(task.id)
    assert result["status"] == "done"


def test_retry_nonexistent_raises(db_session):
    """retry 不存在的任务应抛 ValueError。"""
    with pytest.raises(ValueError, match="not found"):
        discovery_tasks.retry_discovery_task("nonexistent-task-id")


def test_retry_concurrent_protection(db_session, monkeypatch):
    """retry 时已有 queued/running 任务应拒绝。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    # 先创建一个 running 任务
    _create_task(db_session, status="running", stage="sync")
    # 再创建一个 failed 任务尝试 retry
    failed_task = _create_task(db_session, status="failed")
    with pytest.raises(ValueError, match="已有正在运行"):
        discovery_tasks.retry_discovery_task(failed_task.id)


def test_retry_clears_errors(db_session, monkeypatch):
    """retry 应清空 errors_json。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    task = _create_task(db_session, status="failed",
                        errors=[{"scope": "task", "error": "err1"},
                                {"scope": "sync", "error": "err2"}])
    discovery_tasks.retry_discovery_task(task.id)
    db_session.expire_all()
    db_task = db_session.get(DiscoveryTaskRecord, task.id)
    assert json.loads(db_task.errors_json) == [], "retry 后 errors 应清空"


def test_retry_preserves_processed_ids(db_session, monkeypatch):
    """retry 应保留 processed_symbol_ids_json 实现断点续扫。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    processed = [1, 2, 3, 4, 5]
    task = _create_task(db_session, status="failed", processed_ids=processed)
    discovery_tasks.retry_discovery_task(task.id)
    db_session.expire_all()
    db_task = db_session.get(DiscoveryTaskRecord, task.id)
    assert json.loads(db_task.processed_symbol_ids_json) == sorted(processed), \
        "retry 应保留 processed_ids 实现断点续扫"


def test_retry_resets_timestamps(db_session, monkeypatch):
    """retry 应重置 started_at/paused_at/cancelled_at/finished_at。"""
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: None)
    now = _now()
    task = _create_task(db_session, status="failed")
    task.started_at = now - timedelta(hours=1)
    task.finished_at = now
    db_session.commit()

    discovery_tasks.retry_discovery_task(task.id)
    db_session.expire_all()
    db_task = db_session.get(DiscoveryTaskRecord, task.id)
    assert db_task.started_at is None
    assert db_task.paused_at is None
    assert db_task.cancelled_at is None
    assert db_task.finished_at is None


def test_create_task_resumes_matching_paused_task(db_session, monkeypatch):
    """同 scope/portfolio 的 paused 任务在 1 天内点开始应续跑原进度。"""
    from app.schemas.discovery import DiscoveryTaskCreate

    started = []
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: started.append(tid))

    payload = DiscoveryTaskCreate(scope="cn-stock", portfolio_id=7, refresh_universe=False)
    processed = [101, 102, 103]
    task = _create_task(
        db_session,
        status="paused",
        stage="paused",
        percent=60.0,
        processed_ids=processed,
        payload_json=payload.model_dump_json(),
    )
    task.paused_at = _now() - timedelta(hours=2)
    db_session.commit()

    result = discovery_tasks.create_discovery_task(payload)

    assert result["id"] == task.id
    assert result["status"] == "queued"
    assert result["processed"] == len(processed)
    assert result["percent"] >= 60.0
    assert started == [task.id]

    db_session.expire_all()
    db_task = db_session.get(DiscoveryTaskRecord, task.id)
    assert db_task.paused_at is None
    assert json.loads(db_task.processed_symbol_ids_json) == sorted(processed)



def test_create_task_restarts_when_paused_task_expired(db_session, monkeypatch):
    """paused 超过 1 天后再次点开始，应将旧任务标记 expired 并新建任务。"""
    from app.schemas.discovery import DiscoveryTaskCreate

    started = []
    monkeypatch.setattr(discovery_tasks, "_start_worker", lambda tid: started.append(tid))

    payload = DiscoveryTaskCreate(scope="cn-stock", portfolio_id=9, refresh_universe=False)
    task = _create_task(
        db_session,
        status="paused",
        stage="paused",
        percent=72.0,
        processed_ids=[201, 202],
        payload_json=payload.model_dump_json(),
    )
    task.paused_at = _now() - discovery_tasks.RESUME_DEADLINE - timedelta(minutes=1)
    db_session.commit()

    result = discovery_tasks.create_discovery_task(payload)

    assert result["id"] != task.id
    assert result["status"] == "queued"
    assert result["processed"] == 0
    assert started == [result["id"]]

    db_session.expire_all()
    old_task = db_session.get(DiscoveryTaskRecord, task.id)
    assert old_task.status == "expired"
    assert old_task.finished_at is not None



def test_discovery_cached_mode_skips_stale_bar_refresh():
    """cached 模式应直接复用基础数据评分，不先补过期 K 线。"""
    from app.schemas.discovery import DiscoveryTaskCreate

    payload = DiscoveryTaskCreate(scope="cn-stock", refresh_universe=False)
    assert discovery_tasks._should_refresh_discovery_stale_bars(payload) is False


def test_discovery_sync_mode_refreshes_stale_bars():
    """sync 模式应先补过期 K 线，再评分与扫描。"""
    from app.schemas.discovery import DiscoveryTaskCreate

    payload = DiscoveryTaskCreate(scope="cn-stock", refresh_universe=True)
    assert discovery_tasks._should_refresh_discovery_stale_bars(payload) is True
