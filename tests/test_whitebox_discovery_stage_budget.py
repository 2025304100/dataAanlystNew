"""白盒测试 - WP-P.8 5 分钟阶段预算与可观测性。

覆盖 10 个场景：
1. ScanTimings 阶段计时基本功能（add_stage / finish_stage / to_dict / duration_ms）
2. 阶段超时检测（duration_ms > budget_ms 时 exceeded=True）
3. 总预算超时检测（check_total_exceeded 更新 total_duration_ms / total_exceeded）
4. get_exceeded_stages 返回所有超时阶段
5. CancelToken 检查（None / 未取消 / 已取消抛 ScanCancelledError）
6. run_fast_scan 正常完成：返回 timings / status="ok" / exceeded_stages=None
7. run_fast_scan 超时：monkeypatch 减小 budget_ms，enforce_budget=True
   → ScanBudgetExceededError → status="timeout"
8. run_fast_scan 取消：CancelToken.cancel() → ScanCancelledError → status="cancelled"
9. _persist_timings_to_task：将 timings 持久化到 AsyncTaskRecord.result_json
10. API 路由集成：GET /discovery/snapshot/status 返回 last_fast_scan_timings
    与 last_fast_scan_status

硬约束验证（参照 project_memory）：
- 不实际等待 30s+ 阶段预算：通过 monkeypatch 缩小 STAGE_BUDGETS 与 TOTAL_BUDGET_SECONDS
- 取消令牌不阻塞主流程，仅在每个阶段开始前检查
- 超时降级 best-effort，不抛异常给用户
- 终态不被 worker 覆盖（_set_task 已有终态保护）
- 进度更新避免大跳（本任务不直接测试 progress，但 timings 阶段切换平滑）
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.async_task import AsyncTaskRecord
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services import discovery_fast_scan
from app.services.discovery_stage_budget import (
    NO_FILTERS_TARGET_SECONDS,
    STAGE_BUDGETS,
    TOTAL_BUDGET_SECONDS,
    CancelToken,
    ScanBudgetExceededError,
    ScanCancelledError,
    ScanTimings,
    StageTiming,
    check_cancel_token,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数（与 test_whitebox_discovery_data_prep.py 一致，便于跨文件复用）
# ============================================================================

def _make_universe_symbol(
    db_session,
    *,
    symbol: str,
    asset_type: str = "stock",
    region: str = "cn",
    market: str = "sz",
    bar_count: int = 10,
) -> UniverseSymbol:
    us = UniverseSymbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        region=region,
        bar_count=bar_count,
        is_synced=1,
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    db_session.add(us)
    db_session.flush()
    return us


def _make_symbol(
    db_session,
    *,
    symbol: str,
    asset_type: str = "stock",
    market: str = "sz",
) -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        is_active=1,
    )
    db_session.add(sym)
    db_session.flush()
    return sym


def _make_snapshot(
    db_session,
    *,
    scope: str = "cn_stock",
    status: str = "ready",
    trade_date: datetime | None = None,
    generated_at: datetime | None = None,
    symbol_count: int | None = None,
) -> DiscoveryScoreSnapshot:
    if trade_date is None:
        trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = DiscoveryScoreSnapshot(
        scope=scope,
        trade_date=trade_date,
        status=status,
        generated_at=generated_at,
        symbol_count=symbol_count,
    )
    db_session.add(snap)
    db_session.flush()
    return snap


def _make_snapshot_item(
    db_session,
    *,
    snapshot_id: int,
    universe_symbol_id: int,
    symbol_id: int,
    priority_score: float = 75.0,
    stage: str = "accumulate",
    action: str = "buy",
) -> DiscoveryScoreSnapshotItem:
    item = DiscoveryScoreSnapshotItem(
        snapshot_id=snapshot_id,
        universe_symbol_id=universe_symbol_id,
        symbol_id=symbol_id,
        quality_score=70.0,
        timing_score=65.0,
        priority_score=priority_score,
        stage=stage,
        action=action,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _setup_ready_snapshot_with_items(db_session, *, item_count: int = 3):
    """创建 ready 快照 + 多个 item 用于 run_fast_scan 测试。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_count=item_count,
    )
    for i in range(item_count):
        sym = _make_symbol(db_session, symbol=f"STG8_{i:04d}")
        us = _make_universe_symbol(db_session, symbol=f"STG8_{i:04d}")
        _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=80.0 - i,
            stage="accumulate",
            action="buy",
        )
    db_session.commit()
    return snap


# ============================================================================
# 1. ScanTimings 阶段计时基本功能
# ============================================================================

def test_scan_timings_add_finish_to_dict_basic():
    """add_stage / finish_stage / to_dict 基本功能。

    - add_stage 返回 StageTiming，started_at 非空
    - finish_stage 设置 finished_at / duration_ms
    - to_dict 包含 stages / total_duration_ms / total_budget_ms
    """
    timings = ScanTimings()
    assert timings.stages == []
    assert timings.total_exceeded is False

    t = timings.add_stage("snapshot_health_check")
    assert isinstance(t, StageTiming)
    assert t.stage == "snapshot_health_check"
    assert t.started_at is not None
    assert t.finished_at is None
    assert t.duration_ms is None
    # budget_ms 应来自 STAGE_BUDGETS
    assert t.budget_ms == STAGE_BUDGETS["snapshot_health_check"] * 1000

    # 短暂 sleep 以保证 duration_ms > 0
    time.sleep(0.005)
    finished = timings.finish_stage("snapshot_health_check", item_count=10)
    assert finished is not None
    assert finished.finished_at is not None
    assert finished.duration_ms is not None
    assert finished.duration_ms > 0
    assert finished.item_count == 10
    assert finished.exceeded is False  # 5ms < 10000ms

    d = timings.to_dict()
    assert "stages" in d
    assert len(d["stages"]) == 1
    assert d["stages"][0]["stage"] == "snapshot_health_check"
    assert d["stages"][0]["duration_ms"] is not None
    assert d["total_budget_ms"] == TOTAL_BUDGET_SECONDS * 1000
    # total_duration_ms 在 check_total_exceeded 之前为 None
    assert d["total_duration_ms"] is None


def test_scan_timings_finish_unknown_stage_returns_none():
    """finish_stage 找不到匹配阶段时返回 None。"""
    timings = ScanTimings()
    timings.add_stage("snapshot_health_check")
    result = timings.finish_stage("non_existent_stage")
    assert result is None


def test_scan_timings_supports_same_stage_multiple_times():
    """同一阶段名可多次 add/finish（取最后一个未结束的）。"""
    timings = ScanTimings()
    timings.add_stage("finalize_audit")
    timings.finish_stage("finalize_audit")
    # 再次添加同名阶段
    timings.add_stage("finalize_audit")
    assert len(timings.stages) == 2
    finished = timings.finish_stage("finalize_audit")
    assert finished is not None
    assert finished.finished_at is not None
    # 第一个 finalize_audit 已 finish，第二个也 finish
    assert timings.stages[0].finished_at is not None
    assert timings.stages[1].finished_at is not None


# ============================================================================
# 2. 阶段超时检测
# ============================================================================

def test_stage_exceeded_when_duration_exceeds_budget():
    """duration_ms > budget_ms 时 exceeded=True。

    通过显式传入极小的 budget_ms 触发超时，避免实际等待 30s。
    """
    timings = ScanTimings()
    # 显式传入 budget_ms=1（1ms）以保证超时
    t = timings.add_stage("sql_coarse_filter", budget_ms=1.0)
    assert t.budget_ms == 1.0
    time.sleep(0.005)  # 5ms > 1ms
    finished = timings.finish_stage("sql_coarse_filter")
    assert finished is not None
    assert finished.exceeded is True
    assert finished.duration_ms > finished.budget_ms


def test_stage_not_exceeded_when_duration_under_budget():
    """duration_ms < budget_ms 时 exceeded=False。"""
    timings = ScanTimings()
    # budget_ms 给一个足够大的值（10s）
    t = timings.add_stage("snapshot_health_check", budget_ms=10_000.0)
    time.sleep(0.002)
    finished = timings.finish_stage("snapshot_health_check")
    assert finished is not None
    assert finished.exceeded is False


# ============================================================================
# 3. 总预算超时检测
# ============================================================================

def test_check_total_exceeded_updates_state():
    """check_total_exceeded 更新 total_duration_ms / total_exceeded。"""
    timings = ScanTimings()
    timings.add_stage("snapshot_health_check", budget_ms=10_000.0)
    time.sleep(0.003)  # 保证 duration_ms > 0
    timings.finish_stage("snapshot_health_check")
    timings.add_stage("sql_coarse_filter", budget_ms=10_000.0)
    time.sleep(0.003)
    timings.finish_stage("sql_coarse_filter")

    # 默认总预算 300s，远未超时
    result = timings.check_total_exceeded()
    assert result is False
    assert timings.total_duration_ms is not None
    assert timings.total_duration_ms > 0
    assert timings.total_exceeded is False


def test_check_total_exceeded_triggers_when_overrun():
    """总预算超时时 total_exceeded=True。

    通过显式设置 timings.total_budget_ms 为极小值触发。
    """
    timings = ScanTimings()
    timings.total_budget_ms = 0.001  # 0.001ms 极小预算
    timings.add_stage("snapshot_health_check")
    time.sleep(0.002)
    timings.finish_stage("snapshot_health_check")

    result = timings.check_total_exceeded()
    assert result is True
    assert timings.total_exceeded is True


def test_check_total_exceeded_empty_stages():
    """无阶段时 check_total_exceeded 返回 False。"""
    timings = ScanTimings()
    assert timings.check_total_exceeded() is False
    assert timings.total_duration_ms is None


# ============================================================================
# 4. get_exceeded_stages
# ============================================================================

def test_get_exceeded_stages_returns_only_exceeded():
    """get_exceeded_stages 只返回 exceeded=True 的阶段。"""
    timings = ScanTimings()
    # 第一个阶段不超时
    timings.add_stage("snapshot_health_check", budget_ms=10_000.0)
    timings.finish_stage("snapshot_health_check")
    # 第二个阶段超时
    timings.add_stage("sql_coarse_filter", budget_ms=1.0)
    time.sleep(0.005)
    timings.finish_stage("sql_coarse_filter")

    exceeded = timings.get_exceeded_stages()
    assert len(exceeded) == 1
    assert exceeded[0].stage == "sql_coarse_filter"
    assert exceeded[0].exceeded is True


def test_get_exceeded_stages_empty_when_none_exceeded():
    """无超时阶段时返回空列表。"""
    timings = ScanTimings()
    timings.add_stage("snapshot_health_check", budget_ms=10_000.0)
    timings.finish_stage("snapshot_health_check")
    assert timings.get_exceeded_stages() == []


# ============================================================================
# 5. CancelToken 检查
# ============================================================================

def test_check_cancel_token_none_is_noop():
    """token=None 时不抛异常（无取消能力）。"""
    # 不应抛异常
    check_cancel_token(None)


def test_check_cancel_token_not_cancelled_is_noop():
    """token 存在但未取消时不抛异常。"""
    token = CancelToken(task_id="test-task")
    assert token.cancelled is False
    check_cancel_token(token)


def test_cancel_token_cancel_raises_scan_cancelled_error():
    """token.cancel() 后 check() 抛 ScanCancelledError。"""
    token = CancelToken(task_id="test-task")
    token.cancel()
    assert token.cancelled is True
    with pytest.raises(ScanCancelledError):
        token.check()
    # check_cancel_token 也应抛
    with pytest.raises(ScanCancelledError):
        check_cancel_token(token)


def test_cancel_token_task_id_field():
    """CancelToken 保留 task_id 用于关联 AsyncTaskRecord。"""
    token = CancelToken(task_id="abc123")
    assert token.task_id == "abc123"


# ============================================================================
# 6. run_fast_scan 正常完成：返回 timings / status="ok"
# ============================================================================

def test_run_fast_scan_normal_returns_timings_ok(db_session):
    """正常完成时返回 timings / status="ok" / exceeded_stages=None。"""
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )

    # 基本字段
    assert result["degraded_reason"] is None
    assert result["coarse_match_count"] == 3
    assert result["result_rows_written"] == 3

    # WP-P.8 字段
    assert result["status"] == "ok"
    assert result["timings"] is not None
    assert "stages" in result["timings"]
    # 正常完成应至少有 snapshot_health_check / sql_coarse_filter / result_persistence / finalize_audit
    stage_names = [s["stage"] for s in result["timings"]["stages"]]
    assert "snapshot_health_check" in stage_names
    assert "sql_coarse_filter" in stage_names
    assert "result_persistence" in stage_names
    assert "finalize_audit" in stage_names
    # 无超时阶段
    assert result["exceeded_stages"] is None
    # 总预算字段存在
    assert result["timings"]["total_budget_ms"] == TOTAL_BUDGET_SECONDS * 1000
    # cancel_token_checked 标记
    assert result["timings"]["cancel_token_checked"] is True


def test_run_fast_scan_no_snapshot_returns_timings_ok(db_session):
    """无快照时也返回 timings / status="ok"。"""
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )

    assert result["degraded_reason"] == "no_ready_snapshot"
    assert result["status"] == "ok"
    assert result["timings"] is not None
    # 无快照时只有 snapshot_health_check 阶段
    stage_names = [s["stage"] for s in result["timings"]["stages"]]
    assert "snapshot_health_check" in stage_names


# ============================================================================
# 7. run_fast_scan 超时：enforce_budget=True → status="timeout"
# ============================================================================

def test_run_fast_scan_timeout_returns_timeout_status(db_session, monkeypatch):
    """总预算超时时 status="timeout"。

    通过 monkeypatch 缩小 TOTAL_BUDGET_SECONDS 触发超时，避免实际等待 300s。
    """
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    # 缩小总预算到 0.001s，保证任何执行都会超时
    import app.services.discovery_stage_budget as budget_mod
    monkeypatch.setattr(budget_mod, "TOTAL_BUDGET_SECONDS", 0.001)

    # ScanTimings.total_budget_ms 在构造时读取 TOTAL_BUDGET_SECONDS，
    # 需要让 __init__ 使用新的值。最直接的方式是 monkeypatch 默认值
    # 但 dataclass field default 在类定义时已绑定，需要更细粒度处理：
    # 直接在 ScanTimings 构造后覆盖 total_budget_ms。
    # 这里采用更简单的策略：monkeypatch STAGE_BUDGETS 让每个阶段都超时，
    # 同时让 total_budget_ms 在 ScanTimings 初始化时为极小值。
    original_init = ScanTimings.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.total_budget_ms = 0.001  # 0.001ms 极小预算

    monkeypatch.setattr(ScanTimings, "__init__", patched_init)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
        enforce_budget=True,
    )

    # 超时降级：status="timeout"，不抛异常给用户
    assert result["status"] == "timeout"
    assert result["degraded_reason"] == "budget_exceeded"
    assert result["timings"] is not None
    assert result["timings"]["total_exceeded"] is True
    # 超时阶段列表存在（至少 finalize_audit 应超时，因为总预算只有 0.001ms）
    assert result["exceeded_stages"] is not None
    assert isinstance(result["exceeded_stages"], list)


def test_run_fast_scan_timeout_no_enforce_returns_ok_with_degraded(db_session, monkeypatch):
    """enforce_budget=False 时总预算超时不抛异常，但 degraded_reason="budget_exceeded"。

    同时缩小 STAGE_BUDGETS 与 total_budget_ms，使个别阶段也超时；
    这样 exceeded_stages 非空，degraded_reason 被标记为 "budget_exceeded"。
    """
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    # 缩小 STAGE_BUDGETS，使每个阶段预算都极小（0.001s = 1ms）
    import app.services.discovery_stage_budget as budget_mod
    monkeypatch.setattr(
        budget_mod,
        "STAGE_BUDGETS",
        {k: 0.001 for k in budget_mod.STAGE_BUDGETS},
    )

    original_init = ScanTimings.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.total_budget_ms = 0.001  # 0.001ms 极小总预算

    monkeypatch.setattr(ScanTimings, "__init__", patched_init)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
        enforce_budget=False,  # 不强制执行预算
    )

    # 不强制执行时，status 仍为 "ok"，但标记超时降级
    assert result["status"] == "ok"
    assert result["timings"]["total_exceeded"] is True
    # 阶段也超时（每个阶段 budget_ms=1ms，执行时间会超过）
    assert result["exceeded_stages"] is not None
    assert isinstance(result["exceeded_stages"], list)
    assert len(result["exceeded_stages"]) >= 1
    # degraded_reason 应为 "budget_exceeded"
    assert result["degraded_reason"] == "budget_exceeded"


# ============================================================================
# 8. run_fast_scan 取消：CancelToken.cancel() → status="cancelled"
# ============================================================================

def test_run_fast_scan_cancel_before_sql_coarse_filter(db_session):
    """在 sql_coarse_filter 阶段开始前取消 → status="cancelled"。

    cancel_token 在每个阶段开始前检查；snapshot_health_check 完成后、
    sql_coarse_filter 开始前触发取消。
    """
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    token = CancelToken(task_id="test-cancel-task")

    # 包装 _coarse_filter 以在它被调用前取消
    original_coarse_filter = discovery_fast_scan._coarse_filter

    def spy_coarse_filter(*args, **kwargs):
        token.cancel()  # 在 sql_coarse_filter 实际执行前取消
        return original_coarse_filter(*args, **kwargs)

    import app.services.discovery_fast_scan as fs_mod
    original_attr = fs_mod._coarse_filter
    fs_mod._coarse_filter = spy_coarse_filter
    try:
        result = discovery_fast_scan.run_fast_scan(
            scope="cn_stock",
            min_score=55,
            db=db_session,
            cancel_token=token,
        )
    finally:
        fs_mod._coarse_filter = original_attr

    # 取消降级：status="cancelled"
    assert result["status"] == "cancelled"
    assert result["degraded_reason"] == "cancelled_by_user"
    assert result["timings"] is not None
    # 应保留已完成的 snapshot_health_check 阶段
    stage_names = [s["stage"] for s in result["timings"]["stages"]]
    assert "snapshot_health_check" in stage_names


def test_run_fast_scan_cancel_token_none_works(db_session):
    """cancel_token=None 时正常执行，不抛异常。"""
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
        cancel_token=None,
    )

    assert result["status"] == "ok"
    assert result["coarse_match_count"] == 2


# ============================================================================
# 9. _persist_timings_to_task：持久化到 AsyncTaskRecord
# ============================================================================

def test_persist_timings_to_task_writes_result_json(db_session):
    """timings 被写入 AsyncTaskRecord.result_json.timings。"""
    import uuid

    task_id = uuid.uuid4().hex
    task = AsyncTaskRecord(
        id=task_id,
        task_type=discovery_fast_scan.TASK_TYPE_FAST_SCAN,
        status="running",
        stage="scan",
        percent=0,
        payload_json=json.dumps({"scope": "cn_stock"}),
    )
    db_session.add(task)
    db_session.commit()

    # 构造一组 timings
    timings = ScanTimings()
    timings.add_stage("snapshot_health_check")
    timings.finish_stage("snapshot_health_check", item_count=10)
    timings.check_total_exceeded()

    # 调用 _persist_timings_to_task
    # 注意：该函数内部使用 get_session_local()，需确保 db_session 是同一引擎
    # 通过 monkeypatch 替换 get_session_local 返回 db_session 的工厂
    import app.services.discovery_fast_scan as fs_mod
    import app.db.session as session_mod

    class _FactoryStub:
        def __call__(self):
            return _SessionStub(db_session)

    class _SessionStub:
        def __init__(self, real_session):
            self._real = real_session

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):
            # 不关闭外部 db_session（由 fixture 管理）
            pass

    original_get_session_local = session_mod.get_session_local
    session_mod.get_session_local = lambda: _FactoryStub()
    try:
        discovery_fast_scan._persist_timings_to_task(
            task_id, timings, fast_scan_status="ok"
        )
    finally:
        session_mod.get_session_local = original_get_session_local

    # 验证 result_json 已写入 timings 与 fast_scan_status
    db_session.refresh(task)
    result = json.loads(task.result_json or "{}")
    assert "timings" in result
    assert result["fast_scan_status"] == "ok"
    assert result["timings"]["stages"][0]["stage"] == "snapshot_health_check"
    assert result["timings"]["stages"][0]["item_count"] == 10


def test_persist_timings_to_task_none_task_id_is_noop(db_session):
    """task_id=None 时不执行任何操作。"""
    timings = ScanTimings()
    # 不应抛异常
    discovery_fast_scan._persist_timings_to_task(None, timings)


def test_persist_timings_to_task_cancelled_status(db_session):
    """fast_scan_status="cancelled" 被正确写入 result_json。"""
    import uuid

    task_id = uuid.uuid4().hex
    task = AsyncTaskRecord(
        id=task_id,
        task_type=discovery_fast_scan.TASK_TYPE_FAST_SCAN,
        status="cancelled",
        stage="cancelled",
        percent=0,
        payload_json=json.dumps({"scope": "cn_stock"}),
    )
    db_session.add(task)
    db_session.commit()

    timings = ScanTimings()
    timings.add_stage("snapshot_health_check")
    timings.finish_stage("snapshot_health_check")
    timings.degraded_reason = "cancelled_by_user"

    import app.db.session as session_mod

    class _FactoryStub:
        def __call__(self):
            return _SessionStub(db_session)

    class _SessionStub:
        def __init__(self, real_session):
            self._real = real_session

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):
            pass

    original_get_session_local = session_mod.get_session_local
    session_mod.get_session_local = lambda: _FactoryStub()
    try:
        discovery_fast_scan._persist_timings_to_task(
            task_id, timings, fast_scan_status="cancelled"
        )
    finally:
        session_mod.get_session_local = original_get_session_local

    db_session.refresh(task)
    result = json.loads(task.result_json or "{}")
    assert result["fast_scan_status"] == "cancelled"
    assert result["timings"]["degraded_reason"] == "cancelled_by_user"
    # 终态保护：task.status 不应被覆盖（仍是 cancelled）
    assert task.status == "cancelled"


# ============================================================================
# 10. API 路由集成：GET /discovery/snapshot/status 返回 timings
# ============================================================================

def test_snapshot_status_api_returns_last_fast_scan_timings(db_session, monkeypatch):
    """GET /discovery/snapshot/status 在有 fast_scan 任务记录时返回 timings。"""
    from app.main import app
    import uuid

    # 构造一个 AsyncTaskRecord 模拟 fast_scan 任务已完成
    task_id = uuid.uuid4().hex
    timings_dict = {
        "stages": [
            {
                "stage": "snapshot_health_check",
                "started_at": "2026-07-19T10:00:00",
                "finished_at": "2026-07-19T10:00:01",
                "duration_ms": 1000.0,
                "budget_ms": 10000.0,
                "exceeded": False,
                "item_count": 10,
                "error": None,
            }
        ],
        "total_duration_ms": 1000.0,
        "total_budget_ms": 300000.0,
        "total_exceeded": False,
        "degraded_reason": None,
        "cancel_token_checked": True,
    }
    task = AsyncTaskRecord(
        id=task_id,
        task_type=discovery_fast_scan.TASK_TYPE_FAST_SCAN,
        status="done",
        stage="done",
        percent=100,
        payload_json=json.dumps({"scope": "cn-stock"}),
        result_json=json.dumps({
            "timings": timings_dict,
            "fast_scan_status": "ok",
        }),
    )
    db_session.add(task)
    db_session.commit()

    def override_get_db():
        yield db_session

    monkeypatch.setattr(
        discovery_fast_scan,
        "_assert_no_http_request",
        lambda: None,
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "cn-stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # WP-P.8 字段
        assert body["last_fast_scan_timings"] is not None
        assert body["last_fast_scan_status"] == "ok"
        assert body["last_fast_scan_timings"]["stages"][0]["stage"] == "snapshot_health_check"
        assert body["last_fast_scan_timings"]["total_budget_ms"] == 300000.0
    finally:
        app.dependency_overrides.clear()


def test_snapshot_status_api_no_fast_scan_task_returns_none(db_session, monkeypatch):
    """GET /discovery/snapshot/status 无 fast_scan 任务记录时返回 None。"""
    from app.main import app

    def override_get_db():
        yield db_session

    monkeypatch.setattr(
        discovery_fast_scan,
        "_assert_no_http_request",
        lambda: None,
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "cn-stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # 无 fast_scan 任务记录时两个字段为 None
        assert body["last_fast_scan_timings"] is None
        assert body["last_fast_scan_status"] is None
    finally:
        app.dependency_overrides.clear()


def test_snapshot_status_api_filters_by_scope(db_session, monkeypatch):
    """GET /discovery/snapshot/status 按 scope 过滤 fast_scan 任务。"""
    from app.main import app
    import uuid

    # cn_stock scope 的 fast_scan 任务
    task_cn = AsyncTaskRecord(
        id=uuid.uuid4().hex,
        task_type=discovery_fast_scan.TASK_TYPE_FAST_SCAN,
        status="done",
        stage="done",
        percent=100,
        payload_json=json.dumps({"scope": "cn-stock"}),
        result_json=json.dumps({
            "timings": {"stages": [], "total_duration_ms": 500.0},
            "fast_scan_status": "ok",
        }),
    )
    # us_stock scope 的 fast_scan 任务（更晚创建）
    task_us = AsyncTaskRecord(
        id=uuid.uuid4().hex,
        task_type=discovery_fast_scan.TASK_TYPE_FAST_SCAN,
        status="done",
        stage="done",
        percent=100,
        payload_json=json.dumps({"scope": "us-stock"}),
        result_json=json.dumps({
            "timings": {"stages": [], "total_duration_ms": 800.0},
            "fast_scan_status": "ok",
        }),
    )
    db_session.add_all([task_cn, task_us])
    db_session.commit()

    def override_get_db():
        yield db_session

    monkeypatch.setattr(
        discovery_fast_scan,
        "_assert_no_http_request",
        lambda: None,
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        # 查询 cn-stock 应只返回 cn_stock 的 timings
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "cn-stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["last_fast_scan_timings"] is not None
        assert body["last_fast_scan_timings"]["total_duration_ms"] == 500.0

        # 查询 us-stock 应只返回 us_stock 的 timings
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "us-stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["last_fast_scan_timings"] is not None
        assert body["last_fast_scan_timings"]["total_duration_ms"] == 800.0
    finally:
        app.dependency_overrides.clear()


# ============================================================================
# 常量与配置验证
# ============================================================================

def test_stage_budgets_match_spec():
    """STAGE_BUDGETS 与 spec 定义一致（总 280s + 20s 预留 = 300s）。"""
    assert STAGE_BUDGETS == {
        "snapshot_health_check": 10,
        "sql_coarse_filter": 30,
        "advanced_indicator": 90,
        "portfolio_filter": 60,
        "result_persistence": 60,
        "finalize_audit": 30,
    }
    # 阶段预算总和 = 280s
    assert sum(STAGE_BUDGETS.values()) == 280
    # 总预算 = 300s（280s + 20s 预留）
    assert TOTAL_BUDGET_SECONDS == 300
    # 无过滤目标 60s
    assert NO_FILTERS_TARGET_SECONDS == 60


def test_task_type_fast_scan_constant():
    """TASK_TYPE_FAST_SCAN 常量值为 'discovery_fast_scan'。"""
    assert discovery_fast_scan.TASK_TYPE_FAST_SCAN == "discovery_fast_scan"


def test_scan_budget_exceeded_error_carries_timings():
    """ScanBudgetExceededError 携带 timings 与 exceeded_stages。"""
    timings = ScanTimings()
    timings.add_stage("snapshot_health_check", budget_ms=1.0)
    time.sleep(0.005)
    timings.finish_stage("snapshot_health_check")
    exceeded_stages = timings.get_exceeded_stages()

    err = ScanBudgetExceededError(
        "test budget exceeded",
        timings=timings,
        exceeded_stages=exceeded_stages,
    )
    assert err.timings is timings
    assert err.exceeded_stages == exceeded_stages
    assert len(err.exceeded_stages) >= 1


def test_scan_cancelled_error_is_exception():
    """ScanCancelledError 是 Exception 子类。"""
    assert issubclass(ScanCancelledError, Exception)
    err = ScanCancelledError("test")
    assert isinstance(err, Exception)
