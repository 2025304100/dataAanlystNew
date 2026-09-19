"""白盒测试 - 因子流水线启动契约与异常降级 (UAT-P0.3)。

回归根因：`_start_task_heartbeat` 缺少 `return` 语句，调用方
`heartbeat_stop, heartbeat_thread = _start_task_heartbeat(task_id)` 解包 None，
抛出 `cannot unpack non-iterable NoneType object`，任务直接失败。

覆盖：
1. 启动契约：`_start_task_heartbeat` 返回 `(Event, Thread)` 元组，不再返回 None
2. 心跳更新：running 状态心跳持续刷新 updated_at；非 running 状态停止
3. 取消早退：已 cancelled 的任务 → worker 立即返回，终态不被覆盖
4. cancelled 终态保护：异常发生在取消之后 → 状态保持 cancelled，不被 worker 覆盖为 failed
5. 异常降级：异常分支使用统一错误协议（中文文案 + error_code），不裸露 NoneType
6. DuckDB 锁释放：warehouse 写连接随上下文退出释放，取消后无锁残留
7. 真实成功链路：小规模 universe 跑通 计算→训练→快照，产生可追溯的
   因子日期/覆盖率/模型版本/评分快照
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.factor_runtime import FactorSystemConfig
from app.schemas.async_task import FactorPipelineCreate
from app.services.factors.bar_mirror import BarMirrorResult
from app.services.factors.data_sync import FactorInputMirrorResult
from app.services.factors.factor_engine import FactorCalculationResult
from app.services.factors.pipeline_task import (
    _classify_pipeline_error,
    _run_factor_pipeline,
    _start_task_heartbeat,
    _touch_task_heartbeat,
)
from app.services.factors.ridge_model import RidgeTrainingResult
from app.services.factors.runtime import FactorRuntimeSnapshot
from app.services.factors.scoring_bridge import ScoringBridgeResult
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import TargetCalculationResult

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
    payload: FactorPipelineCreate | None = None,
) -> AsyncTaskRecord:
    task = AsyncTaskRecord(
        id=task_id,
        task_type="factor_pipeline",
        status=status,
        stage=stage,
        percent=0.0,
        payload_json=(payload or FactorPipelineCreate()).model_dump_json(),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _setup_config(db_session, warehouse_path: str) -> None:
    db_session.add(
        FactorSystemConfig(
            id=1,
            feature_enabled=1,
            warehouse_path=str(warehouse_path),
            updated_by="test",
        )
    )
    db_session.commit()


def _set_status_via_fresh_session(task_id: str, status: str) -> None:
    """用独立 session 修改任务状态（模拟 API 并发取消）。"""
    SessionLocal = get_session_local()
    sess = SessionLocal()
    try:
        task = sess.get(AsyncTaskRecord, task_id)
        if task is not None:
            task.status = status
            task.stage = status
            sess.commit()
    finally:
        sess.close()


# ============================================================================
# 1. 启动契约：_start_task_heartbeat 返回 (Event, Thread) 元组
# ============================================================================


def test_start_task_heartbeat_returns_event_thread_tuple(db_session):
    """_start_task_heartbeat 必须返回 (Event, Thread)，不能返回 None。

    回归根因：缺少 return 导致调用方解包 None 抛
    `cannot unpack non-iterable NoneType object`。
    """
    task = _create_task(
        db_session, task_id="heartbeat-contract", status="running", stage="mirror"
    )
    try:
        result = _start_task_heartbeat(task.id)
        # 核心断言：返回值是二元组，不是 None
        assert result is not None
        stop_event, thread = result
        assert isinstance(stop_event, threading.Event)
        assert isinstance(thread, threading.Thread)
        assert thread.is_alive()
    finally:
        stop_event.set()
        thread.join(timeout=2.0)


def test_start_task_heartbeat_tuple_unpacking_does_not_raise(db_session):
    """直接验证元组解包契约：`a, b = _start_task_heartbeat(...)` 不再抛异常。"""
    task = _create_task(
        db_session, task_id="heartbeat-unpack", status="running", stage="factors"
    )
    try:
        # 这正是 _run_factor_pipeline 中的调用方式；修复前会抛 TypeError
        heartbeat_stop, heartbeat_thread = _start_task_heartbeat(task.id)
        assert heartbeat_stop is not None
        assert heartbeat_thread is not None
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2.0)


# ============================================================================
# 2. 心跳更新：running 状态心跳持续；非 running 停止
# ============================================================================


def test_touch_task_heartbeat_refreshes_running_task(db_session):
    """running 任务的 updated_at 应被心跳刷新。"""
    base = _now_naive() - timedelta(seconds=30)
    task = _create_task(
        db_session, task_id="heartbeat-refresh", status="running", stage="mirror"
    )
    # 手动设一个旧的 updated_at
    db_session.execute(
        AsyncTaskRecord.__table__.update()
        .where(AsyncTaskRecord.id == task.id)
        .values(updated_at=base)
    )
    db_session.commit()

    assert _touch_task_heartbeat(task.id) is True
    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.updated_at > base


def test_touch_task_heartbeat_stops_for_non_running(db_session):
    """failed/cancelled/done 状态的任务心跳应返回 False，不更新 updated_at。"""
    task = _create_task(
        db_session, task_id="heartbeat-stop", status="failed", stage="failed"
    )
    frozen = _now_naive()
    db_session.execute(
        AsyncTaskRecord.__table__.update()
        .where(AsyncTaskRecord.id == task.id)
        .values(updated_at=frozen)
    )
    db_session.commit()

    assert _touch_task_heartbeat(task.id) is False
    db_session.expire_all()
    assert db_session.get(AsyncTaskRecord, task.id).updated_at == frozen


def test_heartbeat_thread_stops_when_task_no_longer_running(db_session):
    """心跳线程在任务离开 running 状态后应自行退出。"""
    task = _create_task(
        db_session, task_id="heartbeat-auto-stop", status="running", stage="train"
    )
    stop_event, thread = _start_task_heartbeat(task.id)
    try:
        assert thread.is_alive()
        # 将任务标记为 failed（用独立 session 模拟外部状态变更）
        _set_status_via_fresh_session(task.id, "failed")
        # 等待心跳线程在下个周期检测到非 running 并退出
        # TASK_HEARTBEAT_SECONDS=15，但 _touch 返回 False 后线程立即 return
        deadline = time.monotonic() + 20.0
        while thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not thread.is_alive()
    finally:
        stop_event.set()
        thread.join(timeout=2.0)


# ============================================================================
# 3. 取消早退：已 cancelled 的任务 → worker 立即返回，终态不被覆盖
# ============================================================================


def test_run_pipeline_returns_immediately_for_cancelled_task(db_session, tmp_path):
    """已 cancelled 的任务，worker 应立即返回，不启动心跳、不改状态。"""
    _setup_config(db_session, tmp_path / "cancel_early.duckdb")
    task = _create_task(
        db_session, task_id="cancel-early", status="cancelled", stage="cancelled"
    )

    # worker 应直接返回，不抛异常
    _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "cancelled"
    assert refreshed.stage == "cancelled"


# ============================================================================
# 4. cancelled 终态保护：异常发生在取消之后 → 状态保持 cancelled
# ============================================================================


def test_cancelled_terminal_state_not_overwritten_by_worker(db_session, tmp_path):
    """worker 执行中抛异常，但任务已被并发取消 → 状态应保持 cancelled，不被覆盖为 failed。

    硬约束：异步任务终端状态（cancelled）必须不被 worker 线程覆盖。
    """
    _setup_config(db_session, tmp_path / "cancel_protect.duckdb")
    task = _create_task(
        db_session, task_id="cancel-protect", status="running", stage="mirror"
    )

    def _fail_after_cancel(*args, **kwargs):
        # 模拟：mirror 执行期间用户并发取消，然后 mirror 失败
        _set_status_via_fresh_session(task.id, "cancelled")
        raise RuntimeError("mirror failed after user cancelled")

    with patch(
        "app.services.factors.pipeline_task.mirror_daily_bars",
        side_effect=_fail_after_cancel,
    ):
        _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    # 核心断言：cancelled 终态不被 worker 的 except 块覆盖为 failed
    assert refreshed.status == "cancelled"
    assert refreshed.stage == "cancelled"


# ============================================================================
# 5. 异常降级：统一错误协议（中文文案 + error_code），不裸露 NoneType
# ============================================================================


def test_pipeline_failure_uses_unified_error_protocol(db_session, tmp_path):
    """异常分支应使用统一错误协议：中文 user_message + error_code + technical_details。

    不得裸露 `NoneType` / 原始异常文本到用户可见的 message。
    """
    _setup_config(db_session, tmp_path / "error_protocol.duckdb")
    task = _create_task(
        db_session, task_id="error-protocol", status="running", stage="mirror"
    )

    with patch(
        "app.services.factors.pipeline_task.mirror_daily_bars",
        side_effect=RuntimeError("cannot unpack non-iterable NoneType object"),
    ):
        _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "failed"
    assert refreshed.stage == "failed"

    # message 必须是中文可操作文案，不得裸露 NoneType
    assert refreshed.message is not None
    assert "NoneType" not in refreshed.message
    assert "因子流水线" in refreshed.message or "因子仓库" in refreshed.message

    # errors_json 必须包含统一错误协议字段
    errors = json.loads(refreshed.errors_json or "[]")
    assert len(errors) == 1
    err = errors[0]
    assert "error_code" in err
    assert "user_message" in err
    assert "impact" in err
    assert "retryable" in err
    assert "correlation_id" in err
    # technical_details 折叠原始异常
    assert err["technical_details"] is not None
    assert "NoneType" in err["technical_details"]["error_message"]
    assert err["technical_details"]["exception_type"] == "RuntimeError"


def test_classify_pipeline_error_maps_value_error_to_validation(db_session):
    """ValueError(disabled) → FACTOR_PIPELINE_DISABLED；ValueError(参数) → VALIDATION_ERROR。"""
    code_disabled, msg_disabled = _classify_pipeline_error(
        ValueError("Factor pipeline is disabled; enable it"), "initializing"
    )
    assert code_disabled == "FACTOR_PIPELINE_DISABLED"
    assert "启用" in msg_disabled

    code_param, msg_param = _classify_pipeline_error(
        ValueError("validation_days must be less than window_days"), "initializing"
    )
    assert code_param == "VALIDATION_ERROR"
    assert "参数" in msg_param or "校验" in msg_param


def test_classify_pipeline_error_maps_duckdb_lock(db_session):
    """DuckDB 锁/连接类异常 → DB_LOCK_TIMEOUT（可重试）。"""
    code, msg = _classify_pipeline_error(
        RuntimeError("duckdb concurrency lock conflict"), "mirror"
    )
    assert code == "DB_LOCK_TIMEOUT"
    assert "重试" in msg or "占用" in msg


def test_classify_pipeline_error_fallback_unknown(db_session):
    """未知异常 → UNKNOWN_ERROR（可重试），含阶段信息。"""
    code, msg = _classify_pipeline_error(
        RuntimeError("unexpected boom"), "train"
    )
    assert code == "UNKNOWN_ERROR"
    assert "train" in msg
    assert "重试" in msg


# ============================================================================
# 6. DuckDB 锁释放：warehouse 写连接随上下文退出释放，取消后无锁残留
# ============================================================================


def test_warehouse_write_lock_released_after_context_exit(tmp_path):
    """warehouse.connection() 上下文退出后，DuckDB 写连接释放，可再次写入。

    验证取消后无锁残留：worker 协作取消后 context manager 正常退出，
    DuckDB 文件锁随之释放，后续可立即重新写入。
    """
    pytest.importorskip("duckdb")
    warehouse = FactorWarehouse(tmp_path / "lock_release.duckdb")
    warehouse.initialize()

    # 第一次写入（持有写连接）
    with warehouse.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t1 (x INTEGER)")
        conn.execute("INSERT INTO t1 VALUES (1)")

    # 上下文退出后应能立即再次打开写连接（无锁残留）
    with warehouse.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM t1").fetchone()
        assert row[0] == 1
        conn.execute("INSERT INTO t1 VALUES (2)")

    with warehouse.connection(read_only=True) as conn:
        row = conn.execute("SELECT COUNT(*) FROM t1").fetchone()
        assert row[0] == 2


def test_cancelled_pipeline_leaves_warehouse_writable(db_session, tmp_path):
    """取消的流水线退出后，warehouse 可正常写入（无 DuckDB 锁残留）。"""
    pytest.importorskip("duckdb")
    wh_path = tmp_path / "cancel_lock.duckdb"
    _setup_config(db_session, wh_path)
    task = _create_task(
        db_session, task_id="cancel-lock", status="cancelled", stage="cancelled"
    )

    # worker 早退（cancelled），不持有任何 warehouse 连接
    _run_factor_pipeline(task.id)

    # 取消后 warehouse 可正常初始化并写入
    warehouse = FactorWarehouse(wh_path)
    warehouse.initialize()
    with warehouse.connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS post_cancel (v INTEGER)")
        conn.execute("INSERT INTO post_cancel VALUES (42)")
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM post_cancel").fetchone()[0] == 1


# ============================================================================
# 7. 真实成功链路：计算→训练→快照，产生可追溯的因子日期/覆盖率/模型版本/评分快照
# ============================================================================


def test_pipeline_success_path_produces_traceable_results(db_session, tmp_path):
    """小规模 universe 跑通 计算→训练→快照，产生可追溯的：
    因子日期 / 覆盖率 / 模型版本 / 评分快照。

    使用 mock 替换重 IO/重计算函数（mirror/calculate/train/score），
    验证流水线编排契约：状态机 running→done、心跳启停、结果结构可追溯。
    """
    wh_path = tmp_path / "success.duckdb"
    _setup_config(db_session, wh_path)
    payload = FactorPipelineCreate(
        train_model=True,
        materialize_scores=True,
        window_days=250,
        validation_days=50,
    )
    task = _create_task(
        db_session, task_id="success-path", status="queued", payload=payload
    )

    # 可追溯的固定 ID，便于断言
    factor_batch = "factor-batch-001"
    target_batch = "target-batch-001"
    model_run_id = "ridge-run-001"
    factor_date = date(2026, 7, 15)

    bars_result = BarMirrorResult(
        batch_id="bar-mirror-001",
        business_rows=10,
        universe_rows=20,
        metadata_rows=4,
    )
    inputs_result = FactorInputMirrorResult(batch_id="input-mirror-001")
    factors_result = FactorCalculationResult(
        calc_batch_id=factor_batch,
        rows_written=80,
        eligible_rows=75,
        trade_date_count=20,
        symbol_count=4,
        coverage_by_factor={"ep_ttm": 1.0, "momentum_20d": 0.95},
    )
    targets_result = TargetCalculationResult(
        calc_batch_id=target_batch,
        rows_written=60,
        tradable_rows=58,
        invalid_rows=2,
        signal_date_count=19,
        symbol_count=4,
    )
    model_result = RidgeTrainingResult(
        model_run_id=model_run_id,
        status="validated",
        selected_alpha=1.0,
        sample_count=800,
        symbol_count=4,
        trade_date_count=20,
        validation_ic=0.05,
        coefficients={"ep_ttm": 0.3},
        normalized_weights={"ep_ttm": 1.0},
    )
    runtime_snapshot = FactorRuntimeSnapshot(
        weight_mode="shadow",
        active_model_run_id=model_run_id,
        updated_by="test",
        fallback_reason=None,
        version=1,
        updated_at=_now_naive(),
    )
    scoring_result = ScoringBridgeResult(
        mode="shadow",
        trade_date=factor_date,
        model_run_id=model_run_id,
        calc_batch_id=factor_batch,
        manual_score_count=0,
        materialized_count=4,
        skipped_symbol_count=0,
    )

    with patch(
        "app.services.factors.pipeline_task.mirror_daily_bars",
        return_value=bars_result,
    ), patch(
        "app.services.factors.pipeline_task.mirror_factor_inputs",
        return_value=inputs_result,
    ), patch(
        "app.services.factors.pipeline_task.calculate_stock_factors",
        return_value=factors_result,
    ), patch(
        "app.services.factors.pipeline_task.calculate_targets",
        return_value=targets_result,
    ), patch(
        "app.services.factors.pipeline_task.train_rolling_ridge",
        return_value=model_result,
    ), patch(
        "app.services.factors.pipeline_task.get_factor_runtime_snapshot",
        return_value=runtime_snapshot,
    ), patch(
        "app.services.factors.pipeline_task._latest_factor_date",
        return_value=factor_date,
    ), patch(
        "app.services.factors.pipeline_task.materialize_factor_scores",
        return_value=scoring_result,
    ):
        _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)

    # 状态机：queued → running → done（WP-S.5 验证）
    assert refreshed.status == "done"
    assert refreshed.stage == "done"
    assert refreshed.percent == 100.0
    assert refreshed.finished_at is not None
    assert refreshed.started_at is not None

    results = json.loads(refreshed.result_json)
    # 可追溯：因子批次 + 覆盖率
    assert results["factors"]["calc_batch_id"] == factor_batch
    assert results["factors"]["coverage_by_factor"]["ep_ttm"] == 1.0
    assert results["factors"]["symbol_count"] == 4
    # 可追溯：模型版本
    assert results["model"]["model_run_id"] == model_run_id
    assert results["model"]["status"] == "validated"
    # 可追溯：评分快照（因子日期 + 模型版本 + 物化数量）
    assert results["scoring_bridge"]["trade_date"] == factor_date.isoformat()
    assert results["scoring_bridge"]["model_run_id"] == model_run_id
    assert results["scoring_bridge"]["materialized_count"] == 4
    # 可追溯：生效日期范围
    assert "effective_range" in results
    assert results["effective_range"]["end_date"] is not None
    # bar_mirror / input_mirror 结构完整
    assert results["bar_mirror"]["rows_written"] == 30
    assert "rows_written" in results["input_mirror"]


def test_pipeline_success_path_daily_mode_without_training(db_session, tmp_path):
    """train_model=False 的日常模式也能跑通（跳过训练/评分）。"""
    wh_path = tmp_path / "daily.duckdb"
    _setup_config(db_session, wh_path)
    payload = FactorPipelineCreate(
        train_model=False,
        materialize_scores=False,
    )
    task = _create_task(
        db_session, task_id="daily-path", status="queued", payload=payload
    )

    bars_result = BarMirrorResult(batch_id="bar-daily-001")
    inputs_result = FactorInputMirrorResult(batch_id="input-daily-001")
    factors_result = FactorCalculationResult(
        calc_batch_id="factor-daily-001",
        rows_written=40,
        eligible_rows=38,
        trade_date_count=10,
        symbol_count=4,
        coverage_by_factor={"ep_ttm": 1.0},
    )
    targets_result = TargetCalculationResult(
        calc_batch_id="target-daily-001",
        rows_written=30,
        tradable_rows=28,
        invalid_rows=2,
        signal_date_count=9,
        symbol_count=4,
    )

    with patch(
        "app.services.factors.pipeline_task.mirror_daily_bars",
        return_value=bars_result,
    ), patch(
        "app.services.factors.pipeline_task.mirror_factor_inputs",
        return_value=inputs_result,
    ), patch(
        "app.services.factors.pipeline_task.calculate_stock_factors",
        return_value=factors_result,
    ), patch(
        "app.services.factors.pipeline_task.calculate_targets",
        return_value=targets_result,
    ):
        _run_factor_pipeline(task.id)

    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.status == "done"
    assert refreshed.stage == "done"
    results = json.loads(refreshed.result_json)
    # 日常模式无训练/评分
    assert "model" not in results
    assert "scoring_bridge" not in results
    assert results["factors"]["calc_batch_id"] == "factor-daily-001"
