"""WP0-04: 流水线状态机回归测试。

覆盖任务取消、重跑和异常终态场景，确保：
- 取消的任务不会被 worker 覆盖回 running
- 终态（done/failed/cancelled）任务不被恢复逻辑误改
- 失败/取消后可创建新任务（单飞不阻塞已终态任务）
- 僵尸任务（heartbeat 过期）被恢复逻辑正确标记
- 失败任务的 error_code 可提取且 message 无乱码

对齐 docs/专业因子库开发计划.md §WP0-04 任务取消、重跑和异常终态。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.models.factor_runtime import FactorSystemConfig
from app.schemas.async_task import FactorPipelineCreate
from app.services.async_tasks import (
    _set_task,
    _task_to_dict,
    cancel_async_task,
    interrupt_orphaned_async_tasks,
)
from app.services.factors.pipeline_task import (
    _run_factor_pipeline,
    create_factor_pipeline_task,
    recover_stale_pipeline_tasks,
)
from app.services.task_state_machine import cancel_task_with_cleanup

pytestmark = pytest.mark.whitebox


# ---------- 辅助 ----------


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_task(
    db_session,
    *,
    task_id: str,
    status: str = "queued",
    stage: str = "queued",
    percent: float = 0.0,
    payload: FactorPipelineCreate | None = None,
) -> AsyncTaskRecord:
    task = AsyncTaskRecord(
        id=task_id,
        task_type="factor_pipeline",
        status=status,
        stage=stage,
        percent=percent,
        payload_json=(payload or FactorPipelineCreate()).model_dump_json(),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _setup_config(db_session, warehouse_path) -> None:
    db_session.add(
        FactorSystemConfig(
            id=1,
            feature_enabled=1,
            warehouse_path=str(warehouse_path),
            updated_by="test",
        )
    )
    db_session.commit()


def _data_prep_payload() -> FactorPipelineCreate:
    """State-machine tests do not request model training or a FactorSet binding."""
    return FactorPipelineCreate(train_model=False, materialize_scores=False)


def _count_pipeline_tasks(db_session) -> int:
    rows = (
        db_session.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.task_type == "factor_pipeline"
            )
        )
        .scalars()
        .all()
    )
    return len(rows)


# ============================================================================
# 1. 任务创建与单飞（single-flight）
# ============================================================================


def test_create_pipeline_task_creates_queued_task(db_session, tmp_path):
    """创建任务后应处于 queued 状态：status=queued, stage=queued, percent=0。"""
    _setup_config(db_session, tmp_path / "create_queued.duckdb")

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_factor_pipeline_task(_data_prep_payload())

    assert result["task_type"] == "factor_pipeline"
    assert result["status"] == "queued"
    assert result["stage"] == "queued"
    assert result["percent"] == 0

    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, result["id"])
    assert task is not None
    assert task.status == "queued"
    assert task.stage == "queued"
    assert task.percent == 0
    assert task.task_type == "factor_pipeline"


def test_single_flight_rejects_duplicate_running_task(db_session, tmp_path):
    """当存在 running/queued 任务时，create_factor_pipeline_task 返回既有任务而非新建。"""
    _setup_config(db_session, tmp_path / "single_flight.duckdb")
    existing = _create_task(
        db_session, task_id="single-flight-running", status="running", stage="mirror"
    )
    before = _count_pipeline_tasks(db_session)

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_factor_pipeline_task(_data_prep_payload())

    after = _count_pipeline_tasks(db_session)
    # 单飞：返回既有任务，任务总数不增加
    assert result["id"] == existing.id
    assert after == before


def test_single_flight_allows_new_task_after_previous_done(db_session, tmp_path):
    """前序任务到达 done 终态后，可创建新任务（单飞不阻塞已终态任务）。"""
    _setup_config(db_session, tmp_path / "after_done.duckdb")
    done_task = _create_task(
        db_session,
        task_id="single-flight-done",
        status="done",
        stage="done",
        percent=100.0,
    )

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_factor_pipeline_task(_data_prep_payload())

    assert result["id"] != done_task.id
    db_session.expire_all()
    new_task = db_session.get(AsyncTaskRecord, result["id"])
    assert new_task is not None
    assert new_task.status == "queued"
    assert new_task.task_type == "factor_pipeline"


# ============================================================================
# 2. 取消与终态保护
# ============================================================================


def test_cancel_marks_task_cancelled(db_session):
    """取消 queued 任务后 status=cancelled 且 finished_at 已设置。"""
    task = _create_task(
        db_session, task_id="cancel-queued", status="queued", stage="queued"
    )

    cancel_async_task(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "cancelled"
    assert refreshed.stage == "cancelled"
    assert refreshed.finished_at is not None


def test_cancelled_terminal_state_not_overwritten_by_worker(db_session):
    """终态（cancelled/done/failed）不被 _set_task 覆盖回 running。

    与 test_whitebox_factor_pipeline_startup.py 中通过 _run_factor_pipeline + mock
    验证 cancelled 保护的方式互补：本测试直接验证 _set_task 的终态守卫本身，
    并覆盖 done/failed 两种终态。
    """
    for terminal in ("cancelled", "done", "failed"):
        task = _create_task(
            db_session,
            task_id=f"terminal-protect-{terminal}",
            status=terminal,
            stage=terminal,
        )

        # 模拟 worker 试图将终态任务改回 running
        _set_task(
            db_session,
            task.id,
            status="running",
            stage="running",
            percent=50.0,
            message="worker tried to resume",
        )

        db_session.expire_all()
        refreshed = db_session.get(AsyncTaskRecord, task.id)
        assert refreshed.status == terminal, (
            f"终态 {terminal} 不应被 worker 覆盖为 running"
        )
        assert refreshed.stage == terminal
        # 非状态字段仍可更新（终态保护只拦截 status/stage）
        assert refreshed.percent == 50.0


def test_cancel_running_task_sets_cancelled_not_failed(db_session):
    """取消 running 任务后应进入 cancelled（而非 failed）。"""
    task = _create_task(
        db_session, task_id="cancel-running", status="running", stage="mirror"
    )

    cancel_async_task(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "cancelled"
    assert refreshed.status != "failed"
    assert refreshed.stage == "cancelled"
    assert refreshed.finished_at is not None


def test_cancel_request_flag_is_set(db_session):
    """取消请求应设置 cancel_requested=True（让 worker 自检时主动退出）。"""
    task = _create_task(
        db_session, task_id="cancel-requested", status="running", stage="mirror"
    )

    cancel_task_with_cleanup(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "cancelled"
    assert bool(refreshed.cancel_requested) is True


# ============================================================================
# 3. 重跑场景
# ============================================================================


def test_rerun_after_failure_creates_new_task(db_session, tmp_path):
    """任务失败后可创建新任务（单飞不阻塞 failed 终态）。"""
    _setup_config(db_session, tmp_path / "rerun_fail.duckdb")
    failed_task = _create_task(
        db_session, task_id="rerun-after-fail", status="failed", stage="failed"
    )

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_factor_pipeline_task(_data_prep_payload())

    assert result["id"] != failed_task.id
    db_session.expire_all()
    new_task = db_session.get(AsyncTaskRecord, result["id"])
    assert new_task is not None
    assert new_task.status == "queued"


def test_rerun_after_cancel_creates_new_task(db_session, tmp_path):
    """任务取消后可创建新任务（单飞不阻塞 cancelled 终态）。"""
    _setup_config(db_session, tmp_path / "rerun_cancel.duckdb")
    cancelled_task = _create_task(
        db_session,
        task_id="rerun-after-cancel",
        status="cancelled",
        stage="cancelled",
    )

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_factor_pipeline_task(_data_prep_payload())

    assert result["id"] != cancelled_task.id
    db_session.expire_all()
    new_task = db_session.get(AsyncTaskRecord, result["id"])
    assert new_task is not None
    assert new_task.status == "queued"


def test_rerun_produces_different_task_id(db_session, tmp_path):
    """两次顺序创建（前序已完成）应产生不同的 task_id。"""
    _setup_config(db_session, tmp_path / "rerun_diff_id.duckdb")

    with patch("app.services.factors.pipeline_task._start_worker"):
        result1 = create_factor_pipeline_task(_data_prep_payload())

    # 将首个任务标记为 done，使其不阻塞单飞
    _set_task(
        db_session, result1["id"], status="done", stage="done", percent=100.0
    )

    with patch("app.services.factors.pipeline_task._start_worker"):
        result2 = create_factor_pipeline_task(_data_prep_payload())

    assert result1["id"] != result2["id"]


# ============================================================================
# 4. 异常终态恢复
# ============================================================================


def test_stale_running_task_marked_failed_by_recovery(db_session, tmp_path):
    """heartbeat 过期的 running 任务被 recover_stale_pipeline_tasks 标记为 failed。"""
    _setup_config(db_session, tmp_path / "stale_recovery.duckdb")
    task = _create_task(
        db_session, task_id="stale-running", status="running", stage="mirror"
    )
    # 将 heartbeat/updated_at 置为 2 小时前（远超 90s 阈值）
    old = _now_naive() - timedelta(hours=2)
    db_session.execute(
        AsyncTaskRecord.__table__.update()
        .where(AsyncTaskRecord.id == task.id)
        .values(heartbeat_at=old, updated_at=old, started_at=old)
    )
    db_session.commit()

    recovered = recover_stale_pipeline_tasks(db_session)

    assert any(r["task_id"] == task.id for r in recovered)
    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "failed"
    assert refreshed.stage == "failed"
    assert refreshed.finished_at is not None


def test_orphaned_task_marked_interrupted_on_startup(db_session):
    """启动时 interrupt_orphaned_async_tasks 将遗留 running 任务标记为 interrupted。"""
    task = _create_task(
        db_session, task_id="orphaned-running", status="running", stage="mirror"
    )

    interrupted_ids = interrupt_orphaned_async_tasks(db_session)

    assert task.id in interrupted_ids
    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "failed"
    assert refreshed.stage == "interrupted"
    assert refreshed.finished_at is not None


def test_done_task_not_affected_by_recovery(db_session, tmp_path):
    """done 终态任务不被恢复逻辑（recover_stale / interrupt_orphaned）误改。"""
    _setup_config(db_session, tmp_path / "done_safe.duckdb")
    task = _create_task(
        db_session,
        task_id="done-safe",
        status="done",
        stage="done",
        percent=100.0,
    )

    # 两次恢复扫描都不应触碰 done 任务
    recover_stale_pipeline_tasks(db_session)
    interrupt_orphaned_async_tasks(db_session)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "done"
    assert refreshed.stage == "done"
    assert refreshed.percent == 100.0


# ============================================================================
# 5. 错误处理与 error_code
# ============================================================================


def test_failed_task_has_error_code(db_session, tmp_path):
    """失败任务的 errors_json[0].error_code 可提取，且与 _task_to_dict 顶层一致。

    与 test_whitebox_factor_pipeline_startup.py::test_pipeline_failure_uses_unified_error_protocol
    互补：前者验证统一错误协议字段完整性，本测试聚焦 error_code 的端到端可提取性
    （errors_json[0] → _task_to_dict 顶层 error_code）。
    """
    _setup_config(db_session, tmp_path / "error_code.duckdb")
    task = _create_task(
        db_session, task_id="error-code", status="running", stage="mirror"
    )

    with patch(
        "app.services.factors.pipeline_task.mirror_daily_bars",
        side_effect=RuntimeError("duckdb concurrency lock conflict"),
    ):
        _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "failed"

    errors = json.loads(refreshed.errors_json or "[]")
    assert len(errors) == 1
    assert errors[0]["error_code"] == "DB_LOCK_TIMEOUT"

    # 互补断言：_task_to_dict 从 errors[0] 提取的顶层 error_code 一致
    d = _task_to_dict(refreshed)
    assert d["error_code"] == "DB_LOCK_TIMEOUT"
    assert d["error_code"] == d["errors"][0]["error_code"]


def test_failed_task_message_is_utf8_not_mojibake(db_session, tmp_path):
    """失败任务的 message/errors_json 为合法 UTF-8，无 U+FFFD 替换字符（对齐 WPD-05）。"""
    _setup_config(db_session, tmp_path / "utf8_msg.duckdb")
    task = _create_task(
        db_session, task_id="utf8-msg", status="running", stage="mirror"
    )

    with patch(
        "app.services.factors.pipeline_task.mirror_daily_bars",
        side_effect=RuntimeError("duckdb concurrency lock conflict"),
    ):
        _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "failed"

    # message 无 U+FFFD 替换字符（乱码标志）
    assert refreshed.message is not None
    assert "\ufffd" not in refreshed.message

    # errors_json 无 U+FFFD，且使用 ensure_ascii=False（中文原样，无 \uXXXX 转义）
    assert refreshed.errors_json is not None
    assert "\ufffd" not in refreshed.errors_json
    assert "\\u" not in refreshed.errors_json
    assert "因子仓库" in refreshed.message

    # errors_json 可作为合法 UTF-8 JSON 往返解析
    parsed = json.loads(refreshed.errors_json)
    assert parsed[0]["error_code"] == "DB_LOCK_TIMEOUT"
