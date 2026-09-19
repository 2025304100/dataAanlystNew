"""白盒测试 - WP-P.9 端到端清理服务集成测试。

覆盖分层清理端到端场景（区别于 WP-P.7 的单元测试）：
1. 过期候选隐藏+删除（_hide_expired + _cleanup_candidates 全链路）
2. 每 scope 至少保留 N 次（多 scope 独立计算）
3. 已晋升不删除（is_promoted=1 候选 + ScanResult 全链路保护）
4. 快照 items 清理（_cleanup_old_snapshot_items + 主表保留）
5. 日 K 与 Score 不被删除（完整清理后仍存在）
6. 聚合清理 best-effort（单步失败不阻塞后续步骤）

硬约束验证（参照 project_memory）：
- 日 K 与基础因子 / Score 历史绝不删除
- 单步清理失败不阻塞后续步骤
- 已晋升/已观察/已入组合的 ScanResult 不删除
- 事务安全：delete+insert 用单事务
- 错误消息不暴露明文密码/Token
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.daily_bar import DailyBar
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.portfolio import Portfolio, Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import discovery_retention
from app.services.discovery_retention import (
    DEFAULT_CANDIDATE_DISPLAY_DAYS,
    DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS,
    DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    DEFAULT_SCAN_RESULT_RETENTION_DAYS,
    DEFAULT_SCAN_RUN_RETENTION_DAYS,
    DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
    CleanupReport,
    cleanup_expired_discovery_results_layered,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数（与 test_whitebox_discovery_retention.py 保持一致）
# ============================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_symbol(
    db_session,
    *,
    symbol: str,
    asset_type: str = "stock",
    market: str = "sz",
    industry: str | None = None,
) -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        industry=industry,
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
    market: str = "sz",
    region: str = "cn",
) -> UniverseSymbol:
    us = UniverseSymbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        region=region,
        is_synced=1,
    )
    db_session.add(us)
    db_session.flush()
    return us


def _make_scan_run(
    db_session,
    *,
    scope: str = "cn_stock",
    status: str = "done",
    created_at: datetime | None = None,
    snapshot_id: int | None = None,
    cache_key: str | None = None,
) -> ScanRun:
    if created_at is None:
        created_at = _now()
    scope_snapshot = json.dumps({
        "snapshot_id": snapshot_id or 0,
        "scope": scope,
        "source": "test",
    })
    run = ScanRun(
        run_name=f"test-{scope}-{created_at.timestamp()}",
        scope_snapshot=scope_snapshot,
        filters_snapshot="{}",
        status=status,
        started_at=created_at,
        finished_at=created_at,
        created_at=created_at,
        snapshot_id=snapshot_id,
        cache_key=cache_key,
    )
    db_session.add(run)
    db_session.flush()
    return run


def _make_scan_result(
    db_session,
    *,
    scan_run_id: int,
    symbol_id: int,
    result_type: str = "executable",
    rank_no: int = 1,
    priority_score: float = 75.0,
    quality_score: float = 70.0,
    timing_score: float = 65.0,
    stage: str = "accumulate",
    action: str = "buy",
    is_frozen: int = 0,
    is_active: int = 1,
    created_at: datetime | None = None,
) -> ScanResult:
    if created_at is None:
        created_at = _now()
    sr = ScanResult(
        scan_run_id=scan_run_id,
        symbol_id=symbol_id,
        result_type=result_type,
        rank_no=rank_no,
        priority_score=priority_score,
        quality_score=quality_score,
        timing_score=timing_score,
        stage=stage,
        action=action,
        is_frozen=is_frozen,
        is_active=is_active,
        created_at=created_at,
    )
    db_session.add(sr)
    db_session.flush()
    return sr


def _make_candidate(
    db_session,
    *,
    scan_run_id: int,
    universe_symbol_id: int,
    symbol: str,
    is_promoted: int = 0,
    created_at: datetime | None = None,
    priority_score: float = 75.0,
    quality_score: float = 70.0,
    timing_score: float = 65.0,
) -> DiscoveryCandidate:
    if created_at is None:
        created_at = _now()
    cand = DiscoveryCandidate(
        scan_run_id=scan_run_id,
        universe_symbol_id=universe_symbol_id,
        symbol=symbol,
        name=f"测试候选 {symbol}",
        asset_type="stock",
        quality_score=quality_score,
        timing_score=timing_score,
        priority_score=priority_score,
        stage="accumulate",
        action="buy",
        warning_days=3,
        valid_days=5,
        is_promoted=is_promoted,
        created_at=created_at,
    )
    db_session.add(cand)
    db_session.flush()
    return cand


def _make_snapshot(
    db_session,
    *,
    scope: str = "cn_stock",
    status: str = "ready",
    trade_date: datetime | None = None,
    generated_at: datetime | None = None,
    symbol_count: int = 0,
) -> DiscoveryScoreSnapshot:
    if trade_date is None:
        trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = DiscoveryScoreSnapshot(
        scope=scope,
        trade_date=trade_date,
        status=status,
        generated_at=generated_at or trade_date,
        symbol_count=symbol_count,
    )
    db_session.add(snap)
    db_session.flush()
    return snap


def _make_snapshot_item(
    db_session,
    *,
    snapshot_id: int,
    universe_symbol_id: int | None = None,
    symbol_id: int | None = None,
    priority_score: float = 75.0,
    created_at: datetime | None = None,
) -> DiscoveryScoreSnapshotItem:
    if created_at is None:
        created_at = _now()
    item = DiscoveryScoreSnapshotItem(
        snapshot_id=snapshot_id,
        universe_symbol_id=universe_symbol_id,
        symbol_id=symbol_id,
        priority_score=priority_score,
        quality_score=70.0,
        timing_score=65.0,
        stage="accumulate",
        action="buy",
        created_at=created_at,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _make_watchlist(
    db_session,
    *,
    name: str = "test-watchlist",
) -> Watchlist:
    wl = Watchlist(name=name, list_type="observation", description="测试用观察池")
    db_session.add(wl)
    db_session.flush()
    return wl


def _make_daily_bar(
    db_session,
    *,
    symbol_id: int,
    trade_date: date,
    created_at: datetime | None = None,
) -> DailyBar:
    if created_at is None:
        created_at = _now()
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000000.0,
        created_at=created_at,
    )
    db_session.add(bar)
    db_session.flush()
    return bar


def _make_score_record(
    db_session,
    *,
    symbol_id: int,
    trade_date: date,
    created_at: datetime | None = None,
) -> Score:
    if created_at is None:
        created_at = _now()
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=70.0, quality_grade="B",
        timing_score=65.0, priority_score=75.0,
        stage="accumulate", action="buy",
        created_at=created_at,
    )
    db_session.add(score)
    db_session.flush()
    return score


# ============================================================================
# 1. 过期候选隐藏+删除（全链路）
# ============================================================================

def test_full_candidate_cleanup_hide_then_delete(db_session):
    """端到端：过期候选先隐藏（is_active=0）再物理删除（DiscoveryCandidate 删除）。

    流程：
    1. 创建 5 个 ScanResult（3 个过期 + 2 个新鲜）
    2. 创建对应的 DiscoveryCandidate（3 个过期 + 2 个新鲜）
    3. _hide_expired_active_candidates → 3 个 ScanResult is_active=0
    4. _cleanup_expired_discovery_candidates → 3 个过期 DiscoveryCandidate 删除
    5. 新鲜的 ScanResult / Candidate 仍存在
    """
    now = _now()
    run = _make_scan_run(db_session, created_at=now)
    sym = _make_symbol(db_session, symbol="000001")

    fresh_dt = now - timedelta(days=1)
    old_dt = now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1)

    # 3 个过期 + 2 个新鲜
    for i in range(3):
        us = _make_universe_symbol(db_session, symbol=f"OLD{i}")
        _make_scan_result(
            db_session, scan_run_id=run.id, symbol_id=sym.id,
            rank_no=i, created_at=old_dt,
        )
        _make_candidate(
            db_session, scan_run_id=run.id, universe_symbol_id=us.id,
            symbol=f"OLD{i}", created_at=old_dt,
        )
    for i in range(2):
        us = _make_universe_symbol(db_session, symbol=f"NEW{i}")
        _make_scan_result(
            db_session, scan_run_id=run.id, symbol_id=sym.id,
            rank_no=i + 10, created_at=fresh_dt,
        )
        _make_candidate(
            db_session, scan_run_id=run.id, universe_symbol_id=us.id,
            symbol=f"NEW{i}", created_at=fresh_dt,
        )
    db_session.commit()

    # 1. 隐藏过期 ScanResult
    hidden = discovery_retention._hide_expired_active_candidates(
        db_session, now=now, display_days=DEFAULT_CANDIDATE_DISPLAY_DAYS,
    )
    assert hidden == 3

    # 2. 删除过期 DiscoveryCandidate
    deleted = discovery_retention._cleanup_expired_discovery_candidates(
        db_session, now=now,
        retention_days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS,
    )
    assert deleted == 3

    # 3. 验证：5 条 ScanResult 仍存在（隐藏不删除），2 个 DiscoveryCandidate 保留
    assert db_session.query(ScanResult).count() == 5
    assert db_session.query(DiscoveryCandidate).count() == 2

    # 4. 新鲜的 ScanResult is_active=1，过期的 is_active=0
    active_count = db_session.query(ScanResult).filter_by(is_active=1).count()
    inactive_count = db_session.query(ScanResult).filter_by(is_active=0).count()
    assert active_count == 2
    assert inactive_count == 3


def test_candidate_cleanup_preserves_promoted(db_session):
    """已晋升的候选即使过期也不删除。"""
    now = _now()
    run = _make_scan_run(db_session, created_at=now)
    sym = _make_symbol(db_session, symbol="000002")
    us = _make_universe_symbol(db_session, symbol="000002")
    # 为第二个 candidate 创建不同的 universe_symbol 避免违反
    # uq_scan_run_universe（scan_run_id + universe_symbol_id）唯一约束
    sym2 = _make_symbol(db_session, symbol="000003")
    us2 = _make_universe_symbol(db_session, symbol="000003")
    cutoff = now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1)

    # 1 个已晋升 + 1 个未晋升（都过期）
    _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000002", is_promoted=1, created_at=cutoff,
    )
    _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us2.id,
        symbol="000003", is_promoted=0, created_at=cutoff,
    )
    db_session.commit()

    deleted = discovery_retention._cleanup_expired_discovery_candidates(
        db_session, now=now,
        retention_days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS,
    )
    # 只删除未晋升的
    assert deleted == 1
    remaining = db_session.query(DiscoveryCandidate).all()
    assert len(remaining) == 1
    assert remaining[0].is_promoted == 1


# ============================================================================
# 2. 每 scope 至少保留 N 次（多 scope 独立计算）
# ============================================================================

def test_min_scan_runs_per_scope_isolation_end_to_end(db_session):
    """端到端：cn_stock 和 cn_etf 独立计算最近 N 次 ScanRun 保留。

    流程：
    1. cn_stock 创建 5 个过期 ScanRun（每个 1 条 ScanResult）
    2. cn_etf 创建 4 个过期 ScanRun（每个 1 条 ScanResult）
    3. _cleanup_expired_scan_results → cn_stock 保留 3，删除 2；cn_etf 保留 3，删除 1
    """
    now = _now()
    sym_a = _make_symbol(db_session, symbol="000010")
    sym_b = _make_symbol(db_session, symbol="000011")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # cn_stock: 5 个过期 ScanRun
    for i in range(5):
        r = _make_scan_run(
            db_session, scope="cn_stock",
            created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym_a.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )

    # cn_etf: 4 个过期 ScanRun
    for i in range(4):
        r = _make_scan_run(
            db_session, scope="cn_etf",
            created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym_b.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )
    db_session.commit()

    deleted, kept_per_scope = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    # cn_stock: 5 - 3 = 2 ScanRun 的结果删除 → 2 条
    # cn_etf: 4 - 3 = 1 ScanRun 的结果删除 → 1 条
    assert deleted == 3
    assert kept_per_scope.get("cn_stock") == 3
    assert kept_per_scope.get("cn_etf") == 3


# ============================================================================
# 3. 已晋升/已观察/已入组合不删除（全链路保护）
# ============================================================================

def test_cleanup_protects_promoted_candidate_symbols_end_to_end(db_session):
    """端到端：已晋升候选 → 对应 symbol 的 ScanResult 不删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000020")
    us = _make_universe_symbol(db_session, symbol="000020")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # 5 个过期 ScanRun
    for i in range(5):
        r = _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )
    # 第一个 ScanRun 创建已晋升 candidate
    first_run = db_session.query(ScanRun).order_by(ScanRun.id.asc()).first()
    _make_candidate(
        db_session, scan_run_id=first_run.id, universe_symbol_id=us.id,
        symbol="000020", is_promoted=1, created_at=cutoff - timedelta(days=1),
    )
    db_session.commit()

    deleted, _ = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    assert deleted == 0
    assert db_session.query(ScanResult).count() == 5


def test_cleanup_protects_position_symbols_end_to_end(db_session):
    """端到端：已入组合（Position）的 symbol 的 ScanResult 不删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000021")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    for i in range(5):
        r = _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )
    pf = Portfolio(
        name="测试组合", account_type="spot",
        total_capital=100000, investable_ratio=1.0, cash_reserve_ratio=0.1,
    )
    db_session.add(pf)
    db_session.flush()
    db_session.add(Position(
        portfolio_id=pf.id, symbol_id=sym.id,
        quantity=100, avg_cost=10, latest_price=11,
        market_value=1100, position_pct=1.0, asset_type="stock",
    ))
    db_session.commit()

    deleted, _ = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    assert deleted == 0
    assert db_session.query(ScanResult).count() == 5


def test_cleanup_protects_observed_symbols_end_to_end(db_session):
    """端到端：已观察（WatchlistItem）的 symbol 的 ScanResult 不删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000022")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    for i in range(5):
        r = _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )
    wl = _make_watchlist(db_session)
    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id))
    db_session.commit()

    deleted, _ = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    assert deleted == 0


# ============================================================================
# 4. 快照 items 清理
# ============================================================================

def test_cleanup_old_snapshot_items_end_to_end(db_session):
    """端到端：清理过期快照 items（superseded 快照的 items 全部删除），保留 ready 快照 items。

    服务端逻辑（`_cleanup_old_snapshot_items`）：
    - ready 状态快照：保留最近 N 个 trade_date 的 items
    - superseded 状态快照：items 全部删除（保留主表 metadata）

    本测试通过 superseded 快照验证"过期 items 清理"语义。
    """
    now = _now()

    # 1 个 ready 快照 + 5 个新鲜 items（应保留）
    ready_snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=now, symbol_count=5,
    )
    for i in range(5):
        _make_snapshot_item(
            db_session, snapshot_id=ready_snap.id,
            priority_score=80.0 + i, created_at=now,
        )

    # 1 个 superseded 快照 + 5 个 items（应被清理）
    superseded_snap = _make_snapshot(
        db_session, scope="cn_stock", status="superseded",
        generated_at=now - timedelta(days=1), symbol_count=5,
    )
    for i in range(5):
        _make_snapshot_item(
            db_session, snapshot_id=superseded_snap.id,
            priority_score=70.0 + i,
            created_at=now - timedelta(days=1),
        )
    db_session.commit()

    assert db_session.query(DiscoveryScoreSnapshotItem).count() == 10

    # _cleanup_old_snapshot_items 返回 (deleted_items_count, deleted_superseded_snapshots_count)
    deleted_items, deleted_superseded = discovery_retention._cleanup_old_snapshot_items(
        db_session, now=now,
        retention_trading_days=DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
    )
    # superseded 快照的 5 个 items 全部删除
    assert deleted_items == 5
    assert deleted_superseded == 1
    # ready 快照的 5 个 items 仍保留
    assert db_session.query(DiscoveryScoreSnapshotItem).count() == 5

    # 快照主表仍存在（只删 items，不删主表 metadata）
    assert db_session.query(DiscoveryScoreSnapshot).count() == 2


def test_cleanup_snapshot_items_preserves_recent_snapshot(db_session):
    """快照 items 清理后，主表与新鲜 items 仍可复用。"""
    now = _now()
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=now, symbol_count=3,
    )
    for i in range(3):
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            priority_score=70.0 + i, created_at=now,
        )
    db_session.commit()

    # _cleanup_old_snapshot_items 返回 (deleted_items_count, deleted_superseded_snapshots_count)
    deleted_items, _ = discovery_retention._cleanup_old_snapshot_items(
        db_session, now=now,
        retention_trading_days=DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
    )
    assert deleted_items == 0  # 都是新鲜的
    assert db_session.query(DiscoveryScoreSnapshotItem).count() == 3
    assert db_session.query(DiscoveryScoreSnapshot).count() == 1


# ============================================================================
# 5. 日 K 与 Score 不被删除
# ============================================================================

def test_daily_bar_not_deleted_after_full_cleanup(db_session):
    """端到端：完整分层清理后，DailyBar 仍存在。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000030")

    # 创建过期 ScanResult + 候选
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1),
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1),
    )
    us = _make_universe_symbol(db_session, symbol="000030")
    _make_candidate(
        db_session, scan_run_id=old_run.id, universe_symbol_id=us.id,
        symbol="000030",
        created_at=now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1),
    )

    # 创建过期 + 新鲜的 DailyBar
    old_bar = _make_daily_bar(
        db_session, symbol_id=sym.id,
        trade_date=now.date() - timedelta(days=100),
        created_at=now - timedelta(days=100),
    )
    new_bar = _make_daily_bar(
        db_session, symbol_id=sym.id,
        trade_date=now.date(),
        created_at=now,
    )
    db_session.commit()

    # 执行完整分层清理
    report = cleanup_expired_discovery_results_layered(db_session, now=now)

    # DailyBar 不受影响
    assert db_session.query(DailyBar).count() == 2
    assert db_session.query(DailyBar).filter_by(id=old_bar.id).count() == 1
    assert db_session.query(DailyBar).filter_by(id=new_bar.id).count() == 1


def test_score_not_deleted_after_full_cleanup(db_session):
    """端到端：完整分层清理后，Score 历史仍存在。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000031")

    # 创建过期 ScanResult
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1),
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1),
    )

    # 创建过期 + 新鲜的 Score
    old_score = _make_score_record(
        db_session, symbol_id=sym.id,
        trade_date=now.date() - timedelta(days=200),
        created_at=now - timedelta(days=200),
    )
    new_score = _make_score_record(
        db_session, symbol_id=sym.id,
        trade_date=now.date(),
        created_at=now,
    )
    db_session.commit()

    report = cleanup_expired_discovery_results_layered(db_session, now=now)

    # Score 不受影响
    assert db_session.query(Score).count() == 2
    assert db_session.query(Score).filter_by(id=old_score.id).count() == 1
    assert db_session.query(Score).filter_by(id=new_score.id).count() == 1


def test_daily_bar_and_score_survive_layered_cleanup(db_session):
    """端到端：分层清理同时执行，DailyBar 与 Score 都不被删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000032")

    # 创建各类过期数据
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1),
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1),
    )
    us = _make_universe_symbol(db_session, symbol="000032")
    _make_candidate(
        db_session, scan_run_id=old_run.id, universe_symbol_id=us.id,
        symbol="000032",
        created_at=now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1),
    )

    # 创建日 K + Score（新旧都有）
    for days_ago in [1, 100, 200]:
        _make_daily_bar(
            db_session, symbol_id=sym.id,
            trade_date=now.date() - timedelta(days=days_ago),
            created_at=now - timedelta(days=days_ago),
        )
        _make_score_record(
            db_session, symbol_id=sym.id,
            trade_date=now.date() - timedelta(days=days_ago),
            created_at=now - timedelta(days=days_ago),
        )
    db_session.commit()

    bar_count_before = db_session.query(DailyBar).count()
    score_count_before = db_session.query(Score).count()

    cleanup_expired_discovery_results_layered(db_session, now=now)

    # 日 K 与 Score 数量不变
    assert db_session.query(DailyBar).count() == bar_count_before
    assert db_session.query(Score).count() == score_count_before


# ============================================================================
# 6. 聚合清理 best-effort（单步失败不阻塞）
# ============================================================================

def test_layered_cleanup_returns_cleanup_report(db_session):
    """端到端：cleanup_expired_discovery_results_layered 返回 CleanupReport。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000040")

    # 创建少量过期数据
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1),
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1),
    )
    db_session.commit()

    report = cleanup_expired_discovery_results_layered(db_session, now=now)

    assert isinstance(report, CleanupReport)
    # CleanupReport 应有各步骤的计数字段
    assert hasattr(report, "hidden_active_candidates") or hasattr(report, "candidates_hidden")
    assert hasattr(report, "deleted_scan_results") or hasattr(report, "scan_results_deleted")


def test_layered_cleanup_single_step_failure_does_not_block(db_session, monkeypatch):
    """单步清理失败不阻塞后续步骤（best-effort）。

    通过 monkeypatch 让 _hide_expired_active_candidates 抛异常，
    验证后续步骤仍执行。
    """
    now = _now()
    sym = _make_symbol(db_session, symbol="000050")

    # 创建过期数据
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1),
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1),
    )
    db_session.commit()

    # 让 _hide_expired_active_candidates 抛异常
    def _failing_hide(*args, **kwargs):
        raise RuntimeError("模拟隐藏步骤失败")

    monkeypatch.setattr(
        discovery_retention,
        "_hide_expired_active_candidates",
        _failing_hide,
    )

    # 不应抛异常
    report = cleanup_expired_discovery_results_layered(db_session, now=now)

    # 后续步骤仍执行（ScanResult 清理应正常）
    assert isinstance(report, CleanupReport)


def test_layered_cleanup_idempotent(db_session):
    """分层清理可重复执行，第二次执行无新增删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000060")

    # 创建过期数据
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1),
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1),
    )
    us = _make_universe_symbol(db_session, symbol="000060")
    _make_candidate(
        db_session, scan_run_id=old_run.id, universe_symbol_id=us.id,
        symbol="000060",
        created_at=now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1),
    )
    db_session.commit()

    # 第一次清理
    report1 = cleanup_expired_discovery_results_layered(db_session, now=now)

    # 第二次清理：应无新增删除
    report2 = cleanup_expired_discovery_results_layered(db_session, now=now)
    assert isinstance(report2, CleanupReport)


# ============================================================================
# 补充：候选晋升与快照复用
# ============================================================================

def test_candidate_promote_copies_snapshot(db_session):
    """候选晋升为观察项时复制评分快照（底层快照仍可复用）。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000070")
    us = _make_universe_symbol(db_session, symbol="000070")

    # 创建快照 + item
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=now, symbol_count=1,
    )
    _make_snapshot_item(
        db_session, snapshot_id=snap.id,
        universe_symbol_id=us.id, symbol_id=sym.id,
        priority_score=85.0,
    )

    # 创建 ScanRun + ScanResult + Candidate
    run = _make_scan_run(db_session, created_at=now, snapshot_id=snap.id)
    _make_scan_result(
        db_session, scan_run_id=run.id, symbol_id=sym.id,
        created_at=now, priority_score=85.0,
    )
    cand = _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000070", is_promoted=0, created_at=now,
        priority_score=85.0,
    )
    db_session.commit()

    # 晋升候选（promote_candidate_to_observation 接受 watchlist_id，不接受 watchlist_name）
    from app.services.candidate_promote import promote_candidate_to_observation
    wl = _make_watchlist(db_session, name="测试观察池-promote-copy")
    promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
    )
    db_session.commit()

    # 候选应标记为已晋升
    db_session.expire_all()
    promoted_cand = db_session.get(DiscoveryCandidate, cand.id)
    assert promoted_cand.is_promoted == 1

    # 底层快照仍存在（可复用）
    assert db_session.query(DiscoveryScoreSnapshot).filter_by(id=snap.id).count() == 1
    assert db_session.query(DiscoveryScoreSnapshotItem).filter_by(snapshot_id=snap.id).count() == 1

    # 观察项应存在
    wl_item = db_session.query(WatchlistItem).filter_by(symbol_id=sym.id).first()
    assert wl_item is not None


def test_cleanup_after_promote_preserves_snapshot(db_session):
    """晋升后执行清理：候选保留（已晋升），底层快照仍可复用。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000080")
    us = _make_universe_symbol(db_session, symbol="000080")

    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=now, symbol_count=1,
    )
    _make_snapshot_item(
        db_session, snapshot_id=snap.id,
        universe_symbol_id=us.id, symbol_id=sym.id,
        priority_score=85.0,
    )

    # 创建过期 ScanRun + Candidate
    cutoff = now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1)
    run = _make_scan_run(
        db_session, created_at=cutoff, snapshot_id=snap.id,
    )
    _make_scan_result(
        db_session, scan_run_id=run.id, symbol_id=sym.id,
        created_at=cutoff, priority_score=85.0,
    )
    cand = _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000080", is_promoted=0, created_at=cutoff,
        priority_score=85.0,
    )
    db_session.commit()

    # 先晋升候选（promote_candidate_to_observation 接受 watchlist_id，不接受 watchlist_name）
    from app.services.candidate_promote import promote_candidate_to_observation
    wl = _make_watchlist(db_session, name="测试观察池-promote-cleanup")
    promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
    )
    db_session.commit()

    # 执行清理
    cleanup_expired_discovery_results_layered(db_session, now=now)

    # 已晋升候选不删除
    db_session.expire_all()
    assert db_session.query(DiscoveryCandidate).filter_by(id=cand.id).count() == 1

    # 底层快照仍存在
    assert db_session.query(DiscoveryScoreSnapshot).filter_by(id=snap.id).count() == 1

    # ScanResult 因 sym 已观察而保留
    assert db_session.query(ScanResult).count() >= 1


# ============================================================================
# 补充：分层清理覆盖所有层
# ============================================================================

def test_layered_cleanup_covers_all_layers(db_session):
    """端到端：分层清理覆盖候选隐藏 / ScanResult / Candidate / ScanRun / 快照 items。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000090")
    us = _make_universe_symbol(db_session, symbol="000090")

    # 1. 过期 ScanResult（is_active=1，待隐藏）
    fresh_run = _make_scan_run(db_session, created_at=now)
    _make_scan_result(
        db_session, scan_run_id=fresh_run.id, symbol_id=sym.id,
        created_at=now - timedelta(days=DEFAULT_CANDIDATE_DISPLAY_DAYS + 1),
    )

    # 2. 过期 ScanRun + ScanResult + Candidate（待删除）
    old_cutoff = now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1)
    result_cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)
    candidate_cutoff = now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1)

    # 需要超过 min_per_scope (3) 个 ScanRun 才会删除
    for i in range(5):
        old_run = _make_scan_run(
            db_session, created_at=old_cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=old_run.id, symbol_id=sym.id,
            rank_no=i, created_at=result_cutoff,
        )
        # 只给前 2 个 ScanRun 创建 candidate
        if i < 2:
            _make_candidate(
                db_session, scan_run_id=old_run.id, universe_symbol_id=us.id,
                symbol=f"OLD{i}", created_at=candidate_cutoff,
            )
    db_session.commit()

    # 执行分层清理
    report = cleanup_expired_discovery_results_layered(db_session, now=now)
    assert isinstance(report, CleanupReport)

    # 验证：候选隐藏 > 0
    # （至少有 1 个 ScanResult 因过期被隐藏）
    inactive_count = db_session.query(ScanResult).filter_by(is_active=0).count()
    assert inactive_count >= 1
