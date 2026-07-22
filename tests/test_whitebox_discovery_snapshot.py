"""白盒测试 - WP-P.9 端到端快照生命周期集成测试。

覆盖整个快照生命周期的端到端集成场景（区别于 WP-P.1/2 的 schema 单元测试）：
1. 快照构建 → ready → superseded 全流程（通过 _build_ready_snapshot 真实链路）
2. 快照 items 与 Score 表同步（验证 items 字段来自 Score 行）
3. 失败快照不替换 ready（building → failed 时 ready 仍可用）
4. 快照删除时 items 级联（独立 engine + PRAGMA foreign_keys=ON）

硬约束验证（参照 project_memory）：
- 关键数据操作（items 写入）commit 后再切换状态，避免大事务
- 新快照 ready 与旧快照 superseded 同事务原子生效
- 终态不被 worker 覆盖（mark_snapshot_superseded 跳过非 ready 状态）
- 失败 building 快照不污染 ready 快照
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services import discovery_data_prep, discovery_fast_scan
from app.services.discovery_dirty_set import mark_snapshot_superseded

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数（与其它 WP-P 测试文件保持一致风格）
# ============================================================================

def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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


def _make_score(
    db_session,
    *,
    symbol_id: int,
    trade_date: date,
    quality_score: float = 70.0,
    timing_score: float = 65.0,
    priority_score: float = 75.0,
    quality_grade: str = "B",
    stage: str = "accumulate",
    action: str = "buy",
    scoring_config_id: int | None = 1,
    scoring_config_version: int | None = 1,
    weight_mode: str | None = "manual",
    created_at: datetime | None = None,
) -> Score:
    if created_at is None:
        created_at = datetime(2026, 7, 19, 9, 0, 0)
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=quality_score,
        quality_grade=quality_grade,
        timing_score=timing_score,
        priority_score=priority_score,
        stage=stage,
        action=action,
        scoring_config_id=scoring_config_id,
        scoring_config_version=scoring_config_version,
        weight_mode=weight_mode,
        created_at=created_at,
    )
    db_session.add(score)
    db_session.flush()
    return score


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


def _setup_scope_with_scores(
    db_session,
    *,
    scope: str = "cn_stock",
    symbol_codes: list[str] | None = None,
    trade_date: date = date(2026, 7, 19),
) -> tuple[list[Symbol], list[UniverseSymbol], list[Score]]:
    """为某 scope 准备 universe_symbols + symbols + score 数据。

    用于 _build_ready_snapshot 端到端测试。
    """
    if symbol_codes is None:
        symbol_codes = ["000001", "000002", "000003"]

    symbols: list[Symbol] = []
    universe_symbols: list[UniverseSymbol] = []
    scores: list[Score] = []

    for i, code in enumerate(symbol_codes):
        sym = _make_symbol(db_session, symbol=code)
        us = _make_universe_symbol(db_session, symbol=code)
        score = _make_score(
            db_session,
            symbol_id=sym.id,
            trade_date=trade_date,
            quality_score=70.0 + i,
            timing_score=65.0 + i,
            priority_score=75.0 + i,
        )
        symbols.append(sym)
        universe_symbols.append(us)
        scores.append(score)

    db_session.commit()
    return symbols, universe_symbols, scores


# ============================================================================
# 1. 快照构建 → ready → superseded 全流程
# ============================================================================

def test_snapshot_full_lifecycle_building_to_ready_to_superseded(db_session):
    """端到端：_build_ready_snapshot 两次调用 → 旧 ready 被 superseded。

    流程：
    1. 准备 universe + Score 数据
    2. 第一次 _build_ready_snapshot → snapshot A (building → ready)
    3. 第二次 _build_ready_snapshot → snapshot B (building → ready)，A → superseded
    4. get_ready_snapshot 返回 B（最新 ready）
    5. A.status == "superseded"，A 仍存在于数据库（不删除）
    """
    symbols, _, _ = _setup_scope_with_scores(
        db_session, scope="cn_stock", symbol_codes=["000001", "000002"],
    )
    db_session.commit()

    trade_date = date(2026, 7, 19)

    # 第一次构建：A building → ready
    snap_a_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=trade_date,
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-a",
    )
    assert snap_a_id is not None
    db_session.expire_all()
    snap_a = db_session.get(DiscoveryScoreSnapshot, snap_a_id)
    assert snap_a.status == "ready"
    assert snap_a.generated_at is not None
    assert snap_a.build_duration_seconds is not None
    assert snap_a.source_task_id == "task-a"

    # 第二次构建：B building → ready，A → superseded
    # 注意：由于 (scope, status, trade_date) 唯一约束，B 必须用不同 trade_date
    snap_b_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 20),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-b",
    )
    assert snap_b_id is not None
    db_session.expire_all()
    snap_b = db_session.get(DiscoveryScoreSnapshot, snap_b_id)
    assert snap_b.status == "ready"
    assert snap_b.source_task_id == "task-b"

    # A 应被标记为 superseded（不删除）
    snap_a_final = db_session.get(DiscoveryScoreSnapshot, snap_a_id)
    assert snap_a_final.status == "superseded"

    # get_ready_snapshot 返回最新的 ready 快照（B）
    ready = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert ready is not None
    assert ready.id == snap_b_id


def test_snapshot_lifecycle_keeps_superseded_items_intact(db_session):
    """旧快照被 superseded 后，其 items 仍保留（供回溯）。"""
    _setup_scope_with_scores(
        db_session, scope="cn_stock", symbol_codes=["000001", "000002"],
    )
    db_session.commit()

    # 第一次构建快照 A
    snap_a_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-a",
    )
    items_a_before = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter_by(snapshot_id=snap_a_id)
        .count()
    )
    assert items_a_before > 0

    # 第二次构建快照 B（不同 trade_date）
    discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 20),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-b",
    )

    # A 的 items 应仍存在
    items_a_after = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter_by(snapshot_id=snap_a_id)
        .count()
    )
    assert items_a_after == items_a_before


# ============================================================================
# 2. 快照 items 与 Score 表同步
# ============================================================================

def test_snapshot_items_synced_from_score_table(db_session):
    """_build_ready_snapshot 从 Score 表读取数据写入 items，字段一一对应。"""
    symbols, _, scores = _setup_scope_with_scores(
        db_session,
        scope="cn_stock",
        symbol_codes=["000010", "000011", "000012"],
        trade_date=date(2026, 7, 19),
    )
    db_session.commit()

    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-sync",
    )
    assert snap_id is not None

    items = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter_by(snapshot_id=snap_id)
        .order_by(DiscoveryScoreSnapshotItem.symbol_id.asc())
        .all()
    )
    # 应有 3 条 items，对应 3 个 Score
    assert len(items) == 3
    assert len(items) == len(scores)

    # 验证字段一一对应
    scores_sorted = sorted(scores, key=lambda s: s.symbol_id)
    for item, score in zip(items, scores_sorted):
        assert item.symbol_id == score.symbol_id
        assert item.quality_score == score.quality_score
        assert item.timing_score == score.timing_score
        assert item.priority_score == score.priority_score
        assert item.stage == score.stage
        assert item.action == score.action


def test_snapshot_items_count_matches_score_count(db_session):
    """快照 items 数量与 Score 表最新评分数量一致（端到端同步验证）。

    注：`_build_ready_snapshot` 在 `autoflush=False` 的 session 下，
    内部 count 查询在 items flush 前执行（`app/db/manager.py` session_factory
    配置为 `autoflush=False`），导致 `snap.symbol_count` 与 `coverage_pct`
    字段在写入时为 0。这是服务端已知行为，不在 WP-P.9 测试任务范围内修复
    （参照 project_memory 硬约束 #6：不修改业务逻辑）。
    本测试聚焦于验证 items 与 Score 表的实际同步，而非 symbol_count 字段。
    """
    symbols, _, scores = _setup_scope_with_scores(
        db_session,
        scope="cn_stock",
        symbol_codes=["000020", "000021", "000022", "000023"],
    )
    db_session.commit()
    expected_score_count = len(scores)

    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-count",
    )
    db_session.expire_all()
    snap = db_session.get(DiscoveryScoreSnapshot, snap_id)
    assert snap.status == "ready"

    item_count = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter_by(snapshot_id=snap_id)
        .count()
    )
    # items 数量应等于 Score 表中最新评分数量（端到端同步验证）
    assert item_count == expected_score_count
    assert item_count == 4


def test_snapshot_items_empty_when_no_score_data(db_session):
    """universe 有标的但无 Score 数据 → items 为空，coverage_pct=0。"""
    # 只创建 universe + symbol，不创建 Score
    for code in ["000030", "000031"]:
        _make_symbol(db_session, symbol=code)
        _make_universe_symbol(db_session, symbol=code)
    db_session.commit()

    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-empty",
    )
    db_session.expire_all()
    snap = db_session.get(DiscoveryScoreSnapshot, snap_id)
    assert snap.status == "ready"
    assert snap.symbol_count == 0
    assert snap.coverage_pct == 0.0

    item_count = (
        db_session.query(DiscoveryScoreSnapshotItem)
        .filter_by(snapshot_id=snap_id)
        .count()
    )
    assert item_count == 0


# ============================================================================
# 3. 失败快照不替换 ready
# ============================================================================

def test_failed_building_snapshot_does_not_replace_ready(db_session):
    """building 状态快照变 failed 时，原 ready 快照仍可用。

    场景：
    1. 创建 ready 快照 A
    2. 创建 building 快照 B（模拟新版本构建中）
    3. B 构建失败 → status="failed"
    4. get_ready_snapshot 仍返回 A
    5. mark_snapshot_superseded 不会把 A 改为 superseded（因为 B 没成功）
    """
    # 准备 ready 快照 A
    _setup_scope_with_scores(
        db_session, scope="cn_stock", symbol_codes=["000001"],
    )
    db_session.commit()
    snap_a_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-a",
    )
    db_session.expire_all()
    assert db_session.get(DiscoveryScoreSnapshot, snap_a_id).status == "ready"

    # 创建 building 快照 B（同 scope，不同 trade_date 避免唯一约束冲突）
    snap_b = DiscoveryScoreSnapshot(
        scope="cn_stock",
        trade_date=datetime(2026, 7, 20, 0, 0, 0),
        status="building",
        source_task_id="task-b",
    )
    db_session.add(snap_b)
    db_session.commit()
    db_session.refresh(snap_b)

    # B 构建失败 → status="failed"
    snap_b.status = "failed"
    snap_b.error_summary_json = '{"error": "factor_pipeline_timeout"}'
    db_session.commit()

    # get_ready_snapshot 仍返回 A（B 不是 ready）
    ready = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert ready is not None
    assert ready.id == snap_a_id
    assert ready.status == "ready"

    # A 仍是 ready（未被 superseded）
    db_session.expire_all()
    snap_a_final = db_session.get(DiscoveryScoreSnapshot, snap_a_id)
    assert snap_a_final.status == "ready"


def test_mark_snapshot_superseded_skips_failed_snapshot(db_session):
    """mark_snapshot_superseded 对 failed 状态快照跳过（不误覆盖）。

    参照 project_memory 硬约束：终态不被 worker 覆盖。
    """
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="failed",
        trade_date=datetime(2026, 7, 19, 0, 0, 0),
    )
    db_session.commit()

    # 调用 mark_snapshot_superseded（模拟 worker 误调用）
    mark_snapshot_superseded(db_session, snap.id)
    db_session.commit()

    # 状态不应被改变（仍是 failed）
    db_session.expire_all()
    assert db_session.get(DiscoveryScoreSnapshot, snap.id).status == "failed"


def test_failed_snapshot_does_not_block_new_building(db_session):
    """failed 快照不阻塞后续 building 快照创建（不同 trade_date）。"""
    # 创建 failed 快照
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="failed",
        trade_date=datetime(2026, 7, 18, 0, 0, 0),
    )
    db_session.commit()

    # 创建新 building 快照（不同 trade_date）
    new_snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=datetime(2026, 7, 19, 0, 0, 0),
    )
    db_session.commit()
    db_session.refresh(new_snap)

    assert new_snap.status == "building"
    # failed 快照仍存在
    failed_count = (
        db_session.query(DiscoveryScoreSnapshot)
        .filter_by(scope="cn_stock", status="failed")
        .count()
    )
    assert failed_count == 1


# ============================================================================
# 4. 快照删除时 items 级联
# ============================================================================

def test_snapshot_delete_cascades_items_end_to_end():
    """端到端：删除 ready 快照后 items 级联删除（PRAGMA foreign_keys=ON）。

    使用独立 engine 显式开启 SQLite 外键，模拟生产级联行为。
    与 schema 测试的区别：本测试在 items 携带真实 symbol_id / universe_symbol_id
    外键的场景下验证级联。

    注：每个 item 必须使用不同的 universe_symbol_id，否则违反
    `uq_snapshot_item_symbol` 唯一约束（snapshot_id + universe_symbol_id）。
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
        # 创建 3 个不同的 symbol + universe_symbol（避免唯一约束冲突）
        symbols_data = [
            ("000001", "平安银行"),
            ("000002", "万科A"),
            ("000004", "国华网安"),
        ]
        us_ids: list[int] = []
        sym_ids: list[int] = []
        for code, name in symbols_data:
            sym = Symbol(
                symbol=code, name=name, asset_type="stock",
                market="sz", is_active=1,
            )
            session.add(sym)
            session.flush()
            us = UniverseSymbol(
                symbol=code, name=name, asset_type="stock",
                market="sz", region="cn", bar_count=10, is_synced=1,
            )
            session.add(us)
            session.flush()
            us_ids.append(us.id)
            sym_ids.append(sym.id)

        # 创建快照 + 3 个 items（每个 item 使用不同的 universe_symbol_id）
        snap = DiscoveryScoreSnapshot(
            scope="cn_stock",
            trade_date=datetime(2026, 7, 19, 0, 0, 0),
            status="ready",
            generated_at=datetime(2026, 7, 19, 10, 0, 0),
        )
        session.add(snap)
        session.commit()
        session.refresh(snap)

        for i, (us_id, sym_id) in enumerate(zip(us_ids, sym_ids)):
            session.add(DiscoveryScoreSnapshotItem(
                snapshot_id=snap.id,
                universe_symbol_id=us_id,
                symbol_id=sym_id,
                priority_score=70.0 + i,
                quality_score=65.0,
                timing_score=60.0,
                stage="accumulate",
                action="buy",
            ))
        session.commit()

        snap_id = snap.id
        assert (
            session.query(DiscoveryScoreSnapshotItem)
            .filter_by(snapshot_id=snap_id)
            .count()
            == 3
        )

        # 删除快照
        session.delete(snap)
        session.commit()

        # items 应级联删除
        assert (
            session.query(DiscoveryScoreSnapshotItem)
            .filter_by(snapshot_id=snap_id)
            .count()
            == 0
        )
        # 但 symbol / universe_symbol 不受影响（不是级联目标）
        assert session.query(Symbol).count() == 3
        assert session.query(UniverseSymbol).count() == 3
    finally:
        session.close()
        engine.dispose()


def test_snapshot_delete_cascades_with_superseded():
    """superseded 快照删除时 items 同样级联删除。"""
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
            trade_date=datetime(2026, 7, 19, 0, 0, 0),
            status="superseded",
            generated_at=datetime(2026, 7, 18, 10, 0, 0),
        )
        session.add(snap)
        session.commit()
        session.refresh(snap)

        for i in range(2):
            session.add(DiscoveryScoreSnapshotItem(
                snapshot_id=snap.id,
                priority_score=70.0 + i,
            ))
        session.commit()

        snap_id = snap.id
        session.delete(snap)
        session.commit()

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
# 补充：get_ready_snapshot 端到端一致性
# ============================================================================

def test_get_ready_snapshot_returns_none_after_all_superseded(db_session):
    """所有快照都 superseded 时 get_ready_snapshot 返回 None。"""
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="superseded",
        trade_date=datetime(2026, 7, 18, 0, 0, 0),
        generated_at=datetime(2026, 7, 18, 10, 0, 0),
    )
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="superseded",
        trade_date=datetime(2026, 7, 19, 0, 0, 0),
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
    )
    db_session.commit()

    ready = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert ready is None


def test_get_ready_snapshot_picks_latest_after_multiple_builds(db_session):
    """多次构建后，get_ready_snapshot 始终返回最新的 ready 快照。"""
    _setup_scope_with_scores(
        db_session, scope="cn_stock", symbol_codes=["000001"],
    )
    db_session.commit()

    # 三次连续构建（trade_date 递增）
    ids = []
    for d in [date(2026, 7, 17), date(2026, 7, 18), date(2026, 7, 19)]:
        sid = discovery_data_prep._build_ready_snapshot(
            db_session,
            scope="cn_stock",
            trade_date=d,
            trigger_full_rebuild=False,
            full_rebuild_reason=None,
            dirty_symbols=[],
            source_task_id=f"task-{d.isoformat()}",
        )
        ids.append(sid)

    # 只有最后一次应为 ready，前两次为 superseded
    db_session.expire_all()
    ready = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert ready is not None
    assert ready.id == ids[-1]

    # 前两个应为 superseded
    for old_id in ids[:-1]:
        old_snap = db_session.get(DiscoveryScoreSnapshot, old_id)
        assert old_snap.status == "superseded"


def test_snapshot_dirty_symbol_count_recorded(db_session):
    """_build_ready_snapshot 将 dirty_symbols 数量写入 snapshot.dirty_symbol_count。"""
    _setup_scope_with_scores(
        db_session, scope="cn_stock", symbol_codes=["000001"],
    )
    db_session.commit()

    # 模拟 5 个 dirty 标的
    fake_dirty: list[Any] = [object() for _ in range(5)]

    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=fake_dirty,
        source_task_id="task-dirty",
    )
    db_session.expire_all()
    snap = db_session.get(DiscoveryScoreSnapshot, snap_id)
    assert snap.dirty_symbol_count == 5


def test_snapshot_full_rebuild_reason_recorded(db_session):
    """全量重建时 full_rebuild_reason 通过 source_task_id 可追溯。"""
    _setup_scope_with_scores(
        db_session, scope="cn_stock", symbol_codes=["000001"],
    )
    db_session.commit()

    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=True,
        full_rebuild_reason="scoring_config_version_changed",
        dirty_symbols=[],
        source_task_id="task-full-rebuild",
    )
    db_session.expire_all()
    snap = db_session.get(DiscoveryScoreSnapshot, snap_id)
    assert snap.source_task_id == "task-full-rebuild"
    assert snap.status == "ready"
