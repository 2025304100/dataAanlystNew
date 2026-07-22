"""白盒测试 - WP-P.1 / WP-P.2 评分快照与挖掘性能 schema 层。

覆盖：
1. DiscoveryTaskRecord 新增性能监控字段可读写、默认 null 兼容性
2. DiscoveryScoreSnapshot 状态机基础（building → ready、唯一约束）
3. DiscoveryScoreSnapshotItem 基础（Top-K 查询、唯一约束、级联删除）
4. schema 补丁幂等（_ensure_sqlite_discovery_task_perf_columns 重复调用不报错、与旧补丁共存）
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.init_db import (
    _ensure_sqlite_discovery_columns,
    _ensure_sqlite_discovery_task_perf_columns,
)
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.discovery import DiscoveryTaskRecord
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# WP-P.1: DiscoveryTaskRecord 新增性能监控字段
# ============================================================================

def test_discovery_task_record_new_fields_roundtrip(db_session):
    """WP-P.1: 新增性能监控字段可读可写。"""
    task = DiscoveryTaskRecord(
        id="qa-perf-1",
        status="done",
        stage="done",
        scope="cn_stock",
        # WP-P.1 新增字段
        snapshot_id=42,
        snapshot_hit=1,
        stage_durations_json='[{"stage":"coarse","duration_ms":120}]',
        dirty_symbol_count=5,
        reused_score_count=100,
        rescored_count=20,
        coarse_match_count=300,
        advanced_match_count=50,
        result_rows_written=10,
        cache_key="snap42-cn_stock-min55-abc",
        cache_hit=0,
        degraded_reason="snapshot_stale",
    )
    db_session.add(task)
    db_session.commit()
    db_session.expire_all()

    loaded = db_session.get(DiscoveryTaskRecord, "qa-perf-1")
    assert loaded is not None
    assert loaded.snapshot_id == 42
    assert loaded.snapshot_hit == 1
    assert loaded.stage_durations_json == '[{"stage":"coarse","duration_ms":120}]'
    assert loaded.dirty_symbol_count == 5
    assert loaded.reused_score_count == 100
    assert loaded.rescored_count == 20
    assert loaded.coarse_match_count == 300
    assert loaded.advanced_match_count == 50
    assert loaded.result_rows_written == 10
    assert loaded.cache_key == "snap42-cn_stock-min55-abc"
    assert loaded.cache_hit == 0
    assert loaded.degraded_reason == "snapshot_stale"


def test_discovery_task_record_new_fields_default_null(db_session):
    """WP-P.1: 新字段默认为 null，向后兼容旧数据。"""
    task = DiscoveryTaskRecord(
        id="qa-perf-default",
        status="queued",
        stage="prepare",
        scope="cn_stock",
    )
    db_session.add(task)
    db_session.commit()
    db_session.expire_all()

    loaded = db_session.get(DiscoveryTaskRecord, "qa-perf-default")
    assert loaded is not None
    # 全部新增字段默认应为 null
    assert loaded.snapshot_id is None
    assert loaded.snapshot_hit is None
    assert loaded.stage_durations_json is None
    assert loaded.dirty_symbol_count is None
    assert loaded.reused_score_count is None
    assert loaded.rescored_count is None
    assert loaded.coarse_match_count is None
    assert loaded.advanced_match_count is None
    assert loaded.result_rows_written is None
    assert loaded.cache_key is None
    assert loaded.cache_hit is None
    assert loaded.degraded_reason is None


# ============================================================================
# WP-P.2: DiscoveryScoreSnapshot 状态机基础
# ============================================================================

def _make_snapshot(
    db_session,
    scope: str = "cn_stock",
    status: str = "building",
    trade_date: datetime | None = None,
) -> DiscoveryScoreSnapshot:
    if trade_date is None:
        trade_date = datetime(2026, 7, 19)
    snap = DiscoveryScoreSnapshot(scope=scope, trade_date=trade_date, status=status)
    db_session.add(snap)
    db_session.commit()
    db_session.refresh(snap)
    return snap


def test_snapshot_default_status_is_building(db_session):
    """WP-P.2: 新建快照默认状态为 building。"""
    snap = DiscoveryScoreSnapshot(
        scope="cn_stock",
        trade_date=datetime(2026, 7, 19),
    )
    db_session.add(snap)
    db_session.commit()
    db_session.refresh(snap)
    assert snap.status == "building"
    assert snap.symbol_count == 0
    assert snap.coverage_pct == 0.0
    assert snap.dirty_symbol_count == 0


def test_snapshot_building_to_ready_transition(db_session):
    """WP-P.2: 快照状态 building → ready 流转，generated_at 写入。"""
    snap = _make_snapshot(db_session, status="building")
    assert snap.status == "building"
    assert snap.generated_at is None

    snap.status = "ready"
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    snap.generated_at = now_naive
    snap.build_duration_seconds = 12.5
    db_session.commit()
    db_session.refresh(snap)

    assert snap.status == "ready"
    assert snap.generated_at == now_naive
    assert snap.build_duration_seconds == 12.5


def test_snapshot_unique_scope_status_trade_date(db_session):
    """WP-P.2: (scope, status, trade_date) 唯一约束生效，同 scope+status+date 不允许重复。"""
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=datetime(2026, 7, 19),
    )
    # 重复插入应抛 IntegrityError
    with pytest.raises(IntegrityError):
        _make_snapshot(
            db_session,
            scope="cn_stock",
            status="building",
            trade_date=datetime(2026, 7, 19),
        )
    db_session.rollback()


def test_snapshot_allows_different_status_same_scope_date(db_session):
    """WP-P.2: 同 scope + trade_date 不同 status 允许共存（building 与 ready 各一条）。

    这是状态机设计：新版本 building 时旧版本仍可保持 ready。
    """
    _make_snapshot(
        db_session,
        scope="cn_etf",
        status="building",
        trade_date=datetime(2026, 7, 19),
    )
    _make_snapshot(
        db_session,
        scope="cn_etf",
        status="ready",
        trade_date=datetime(2026, 7, 19),
    )
    # 不抛异常即通过
    rows = (
        db_session.query(DiscoveryScoreSnapshot)
        .filter_by(scope="cn_etf", trade_date=datetime(2026, 7, 19))
        .all()
    )
    assert len(rows) == 2


def test_snapshot_allows_different_trade_date_same_scope_status(db_session):
    """WP-P.2: 同 scope + status 不同 trade_date 允许共存。"""
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=datetime(2026, 7, 18),
    )
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=datetime(2026, 7, 19),
    )
    # 不抛异常即通过


# ============================================================================
# WP-P.2: DiscoveryScoreSnapshotItem 基础
# ============================================================================

def _make_snapshot_with_items(
    db_session,
    items_spec: list[tuple[float, int]],
    scope: str = "cn_stock",
    trade_date: datetime | None = None,
) -> DiscoveryScoreSnapshot:
    """创建一个 ready 快照并写入若干 items。

    items_spec: [(priority_score, universe_symbol_id), ...]
    """
    snap = _make_snapshot(db_session, scope=scope, status="ready", trade_date=trade_date)
    for priority, uni_id in items_spec:
        item = DiscoveryScoreSnapshotItem(
            snapshot_id=snap.id,
            universe_symbol_id=uni_id,
            priority_score=priority,
        )
        db_session.add(item)
    db_session.commit()
    return snap


def test_snapshot_item_topk_by_priority_desc(db_session):
    """WP-P.2: 按 priority_score 倒序取 Top-K（利用 ix_snapshot_item_priority 索引）。"""
    snap = _make_snapshot_with_items(
        db_session,
        [
            (50.0, 1),
            (80.0, 2),
            (65.0, 3),
            (90.0, 4),
            (75.0, 5),
        ],
    )
    items = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter(DiscoveryScoreSnapshotItem.snapshot_id == snap.id)
        .order_by(DiscoveryScoreSnapshotItem.priority_score.desc())
        .limit(3)
        .all()
    )
    assert [item.priority_score for item in items] == [90.0, 80.0, 75.0]


def test_snapshot_item_unique_symbol_per_snapshot(db_session):
    """WP-P.2: 同一 snapshot + universe_symbol 不允许重复（uq_snapshot_item_symbol）。"""
    snap = _make_snapshot_with_items(db_session, [(50.0, 100)])
    with pytest.raises(IntegrityError):
        dup = DiscoveryScoreSnapshotItem(
            snapshot_id=snap.id,
            universe_symbol_id=100,
            priority_score=60.0,
        )
        db_session.add(dup)
        db_session.commit()
    db_session.rollback()


def test_snapshot_item_allows_same_symbol_different_snapshot(db_session):
    """WP-P.2: 不同 snapshot 下相同 universe_symbol 允许共存。"""
    snap1 = _make_snapshot_with_items(
        db_session,
        [(50.0, 200)],
        scope="cn_stock",
        trade_date=datetime(2026, 7, 19),
    )
    # 第二个快照同 scope 但不同 trade_date，避免触发 (scope, status, trade_date) 唯一约束
    snap2 = _make_snapshot_with_items(
        db_session,
        [(70.0, 200)],
        scope="cn_stock",
        trade_date=datetime(2026, 7, 20),
    )
    # 不抛异常即通过
    count = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter(DiscoveryScoreSnapshotItem.universe_symbol_id == 200)
        .count()
    )
    assert count == 2
    assert snap1.id != snap2.id


def test_snapshot_item_cascade_delete():
    """WP-P.2: 删除 snapshot 后 items 级联删除（ondelete=CASCADE）。

    使用独立 engine 显式开启 SQLite PRAGMA foreign_keys=ON，
    确保数据库层级联生效（SQLite 默认不启用外键）。
    """
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _connection_record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        snap = DiscoveryScoreSnapshot(
            scope="cn_stock",
            trade_date=datetime(2026, 7, 19),
            status="ready",
        )
        session.add(snap)
        session.commit()
        session.refresh(snap)

        for i, score in enumerate([50.0, 80.0, 65.0]):
            session.add(
                DiscoveryScoreSnapshotItem(
                    snapshot_id=snap.id,
                    universe_symbol_id=i + 1,
                    priority_score=score,
                )
            )
        session.commit()

        snap_id = snap.id
        assert (
            session.query(DiscoveryScoreSnapshotItem)
            .filter_by(snapshot_id=snap_id)
            .count()
            == 3
        )

        session.delete(snap)
        session.commit()

        # 级联删除后 items 应为 0
        assert (
            session.query(DiscoveryScoreSnapshotItem)
            .filter_by(snapshot_id=snap_id)
            .count()
            == 0
        )
    finally:
        session.close()
        engine.dispose()


# ============================================================================
# WP-P.1: schema 补丁幂等
# ============================================================================

def test_schema_patch_idempotent_sqlite():
    """WP-P.1: _ensure_sqlite_discovery_task_perf_columns 重复调用幂等。"""
    engine = create_engine("sqlite:///:memory:")
    # 模拟旧库：手动建一个最小 discovery_tasks 表（只含旧字段）
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE discovery_tasks ("
                "id TEXT PRIMARY KEY, status TEXT, stage TEXT, scope TEXT"
                ")"
            )
        )

    # 第一次调用应补齐所有新字段
    _ensure_sqlite_discovery_task_perf_columns(engine)
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("discovery_tasks")}
    expected = {
        "snapshot_id",
        "snapshot_hit",
        "stage_durations_json",
        "dirty_symbol_count",
        "reused_score_count",
        "rescored_count",
        "coarse_match_count",
        "advanced_match_count",
        "result_rows_written",
        "cache_key",
        "cache_hit",
        "degraded_reason",
    }
    assert expected.issubset(cols), f"缺失字段: {expected - cols}"

    # 第二次调用不应报错（幂等）
    _ensure_sqlite_discovery_task_perf_columns(engine)
    cols_after_second = {
        c["name"] for c in inspect(engine).get_columns("discovery_tasks")
    }
    assert expected.issubset(cols_after_second)
    engine.dispose()


def test_schema_patch_coexists_with_legacy_discovery_columns():
    """WP-P.1: 新补丁与 _ensure_sqlite_discovery_columns 顺序无关、共存。

    场景 1：新库先调用新补丁，再调用旧补丁；
    场景 2：旧库已有 cleanup_count，再补新字段。
    """
    # 场景 1：新库（discovery_tasks 表只有基础字段）
    engine1 = create_engine("sqlite:///:memory:")
    with engine1.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE discovery_tasks ("
                "id TEXT PRIMARY KEY, status TEXT, stage TEXT, scope TEXT"
                ")"
            )
        )
    _ensure_sqlite_discovery_task_perf_columns(engine1)
    _ensure_sqlite_discovery_columns(engine1)
    cols1 = {
        c["name"] for c in inspect(engine1).get_columns("discovery_tasks")
    }
    assert "cleanup_count" in cols1  # 旧补丁字段
    assert "snapshot_id" in cols1  # 新补丁字段
    engine1.dispose()

    # 场景 2：旧库已有 cleanup_count（旧补丁已应用过），再补新字段
    engine2 = create_engine("sqlite:///:memory:")
    with engine2.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE discovery_tasks ("
                "id TEXT PRIMARY KEY, status TEXT, stage TEXT, scope TEXT, "
                "cleanup_count INTEGER DEFAULT 0"
                ")"
            )
        )
    _ensure_sqlite_discovery_columns(engine2)
    _ensure_sqlite_discovery_task_perf_columns(engine2)
    cols2 = {
        c["name"] for c in inspect(engine2).get_columns("discovery_tasks")
    }
    assert "cleanup_count" in cols2
    assert "snapshot_id" in cols2
    engine2.dispose()


def test_schema_patch_skips_when_table_missing():
    """WP-P.1: 表不存在时 _ensure_sqlite_discovery_task_perf_columns 安全跳过。"""
    engine = create_engine("sqlite:///:memory:")
    # 不创建 discovery_tasks 表
    _ensure_sqlite_discovery_task_perf_columns(engine)
    # 不抛异常即通过
    inspector = inspect(engine)
    assert "discovery_tasks" not in inspector.get_table_names()
    engine.dispose()
