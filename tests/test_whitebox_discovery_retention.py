"""白盒测试 - WP-P.7 扩展分层保留与清理。

覆盖：
1. 隐藏过期活动候选（_hide_expired_active_candidates）
2. 删除过期未晋升 ScanResult（_cleanup_expired_scan_results）
3. 每 scope 至少保留最近 N 次 ScanRun 的结果
4. 已晋升/已观察/已入组合的 ScanResult 不删除
5. 删除过期未晋升 DiscoveryCandidate（_cleanup_expired_discovery_candidates）
6. 删除过期 ScanRun 摘要（_cleanup_old_scan_runs）
7. 删除过期评分快照 items（_cleanup_old_snapshot_items）
8. 删除 superseded 状态快照主表（_cleanup_superseded_snapshots）
9. 分层清理聚合 cleanup_expired_discovery_results_layered
10. 现有 cleanup_expired_discovery_results 向后兼容
11. 单步清理失败不阻塞后续步骤（best-effort）
12. 日 K 与 Score 历史不受分层清理影响
13. 候选晋升为观察项时复制评分快照（promote_candidate_to_observation）

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
from sqlalchemy import select

from app.db.base import Base
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
from app.services.candidate_promote import (
    promote_candidate_to_observation,
)
from app.services.discovery_cleanup import cleanup_expired_discovery_results
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
# 辅助函数
# ============================================================================

def _now() -> datetime:
    """统一的 now（与 services 内部 _now_utc 对齐）。"""
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
    symbol_count: int = 0,
) -> DiscoveryScoreSnapshot:
    if trade_date is None:
        trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = DiscoveryScoreSnapshot(
        scope=scope,
        trade_date=trade_date,
        status=status,
        generated_at=trade_date,
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
) -> DiscoveryScoreSnapshotItem:
    item = DiscoveryScoreSnapshotItem(
        snapshot_id=snapshot_id,
        universe_symbol_id=universe_symbol_id,
        symbol_id=symbol_id,
        priority_score=priority_score,
        quality_score=70.0,
        timing_score=65.0,
        stage="accumulate",
        action="buy",
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


# ============================================================================
# 1. 隐藏过期活动候选
# ============================================================================

def test_hide_expired_active_candidates(db_session):
    """超过 display_days 的活动候选 → is_active=0，记录仍存在。"""
    now = _now()
    run = _make_scan_run(db_session, created_at=now)
    sym = _make_symbol(db_session, symbol="000001")

    # 5 个 ScanResult：2 个新（< display_days）、3 个旧（> display_days）
    fresh_dt = now - timedelta(days=1)
    old_dt = now - timedelta(days=DEFAULT_CANDIDATE_DISPLAY_DAYS + 1)
    for i in range(2):
        _make_scan_result(
            db_session, scan_run_id=run.id, symbol_id=sym.id,
            rank_no=i, created_at=fresh_dt,
        )
    for i in range(3):
        _make_scan_result(
            db_session, scan_run_id=run.id, symbol_id=sym.id,
            rank_no=i + 10, created_at=old_dt,
        )
    db_session.commit()

    hidden = discovery_retention._hide_expired_active_candidates(
        db_session, now=now, display_days=DEFAULT_CANDIDATE_DISPLAY_DAYS,
    )
    assert hidden == 3

    # 验证：3 条 is_active=0，2 条 is_active=1，全部 5 条记录仍存在
    all_results = db_session.query(ScanResult).all()
    assert len(all_results) == 5
    inactive_count = sum(1 for r in all_results if r.is_active == 0)
    active_count = sum(1 for r in all_results if r.is_active == 1)
    assert inactive_count == 3
    assert active_count == 2


def test_hide_expired_does_not_touch_frozen_results(db_session):
    """is_frozen=1 的 ScanResult 即使过期也不隐藏。"""
    now = _now()
    run = _make_scan_run(db_session, created_at=now)
    sym = _make_symbol(db_session, symbol="000002")
    old_dt = now - timedelta(days=DEFAULT_CANDIDATE_DISPLAY_DAYS + 10)
    _make_scan_result(
        db_session, scan_run_id=run.id, symbol_id=sym.id,
        created_at=old_dt, is_frozen=1,
    )
    db_session.commit()

    hidden = discovery_retention._hide_expired_active_candidates(
        db_session, now=now, display_days=DEFAULT_CANDIDATE_DISPLAY_DAYS,
    )
    assert hidden == 0
    sr = db_session.query(ScanResult).one()
    assert sr.is_active == 1


def test_hide_expired_protects_promoted_observed_position(db_session):
    """已晋升/已观察/已入组合的 ScanResult 即使过期也不隐藏。"""
    now = _now()
    run = _make_scan_run(db_session, created_at=now)
    sym = _make_symbol(db_session, symbol="000003")
    old_dt = now - timedelta(days=DEFAULT_CANDIDATE_DISPLAY_DAYS + 5)
    _make_scan_result(
        db_session, scan_run_id=run.id, symbol_id=sym.id, created_at=old_dt,
    )

    # 创建 WatchlistItem 使 sym 进入"已观察"
    wl = _make_watchlist(db_session)
    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id))
    db_session.commit()

    hidden = discovery_retention._hide_expired_active_candidates(
        db_session, now=now, display_days=DEFAULT_CANDIDATE_DISPLAY_DAYS,
    )
    assert hidden == 0
    sr = db_session.query(ScanResult).one()
    assert sr.is_active == 1


# ============================================================================
# 2. 删除过期未晋升 ScanResult
# ============================================================================

def test_cleanup_expired_scan_results_basic(db_session):
    """超过 retention_days 的 ScanResult 删除；最近 N 次 ScanRun 的结果保留。"""
    now = _now()
    # 使用足够多的 ScanRun，确保删除的 ScanResult 来自过期 ScanRun
    # 而最近 3 次 ScanRun（即使过期）的结果保留
    fresh_run = _make_scan_run(db_session, created_at=now)
    sym = _make_symbol(db_session, symbol="000010")

    # 7 个过期 ScanResult（来自同一过期 ScanRun，但该 ScanRun 不在最近 3 次内）
    old_run = _make_scan_run(
        db_session, created_at=now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 5),
    )
    old_dt = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)
    for i in range(7):
        _make_scan_result(
            db_session, scan_run_id=old_run.id, symbol_id=sym.id,
            rank_no=i, created_at=old_dt,
        )
    # 3 个新鲜 ScanResult（来自 fresh_run，未过期）
    for i in range(3):
        _make_scan_result(
            db_session, scan_run_id=fresh_run.id, symbol_id=sym.id,
            rank_no=i + 100, created_at=now - timedelta(days=1),
        )
    db_session.commit()

    deleted, kept_per_scope = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    # old_run 在 scope cn_stock 下，且只有 2 个 run（fresh + old），所以最近 3 次包含两者
    # old_run 的 ScanResult 因属于"最近 N 次 ScanRun"而保留 → 删除 0
    # 这验证了"每 scope 至少保留最近 N 次"的语义
    assert deleted == 0
    # 数据库中仍有 10 条 ScanResult（7+3），未被删除
    assert db_session.query(ScanResult).count() == 10


def test_cleanup_expired_scan_results_deletes_when_more_runs(db_session):
    """当 scope 下 ScanRun 数量 > min_per_scope 时，过期 ScanRun 的 ScanResult 删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000011")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # 创建 5 个 ScanRun，全部过期（created_at 都早于 cutoff）
    old_runs = []
    for i in range(5):
        r = _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
        old_runs.append(r)
    # 给每个 ScanRun 写 2 条 ScanResult
    for r in old_runs:
        for j in range(2):
            _make_scan_result(
                db_session, scan_run_id=r.id, symbol_id=sym.id,
                rank_no=j, created_at=cutoff - timedelta(days=1),
            )
    db_session.commit()

    deleted, kept_per_scope = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    # cn_stock 下 5 个 ScanRun，保留最近 3 个，删除其余 2 个 ScanRun 的 ScanResult
    # 2 ScanRun × 2 ScanResult = 4 条删除
    assert deleted == 4
    assert kept_per_scope.get("cn_stock") == 3
    # 数据库剩余 3 × 2 = 6 条
    assert db_session.query(ScanResult).count() == 6


# ============================================================================
# 3. 每 scope 至少保留最近 N 次（独立计算）
# ============================================================================

def test_min_scan_runs_per_scope_isolation(db_session):
    """不同 scope 独立计算最近 N 次 ScanRun 保留。"""
    now = _now()
    sym_a = _make_symbol(db_session, symbol="000020")
    sym_b = _make_symbol(db_session, symbol="000021")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # scope=cn_stock：5 个过期 ScanRun
    cn_runs = []
    for i in range(5):
        r = _make_scan_run(
            db_session, scope="cn_stock",
            created_at=cutoff - timedelta(days=i + 1),
        )
        cn_runs.append(r)
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym_a.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )

    # scope=cn_etf：4 个过期 ScanRun
    etf_runs = []
    for i in range(4):
        r = _make_scan_run(
            db_session, scope="cn_etf",
            created_at=cutoff - timedelta(days=i + 1),
        )
        etf_runs.append(r)
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
    # cn_stock: 5 - 3 = 2 ScanRun 的结果被删除 → 2 条
    # cn_etf: 4 - 3 = 1 ScanRun 的结果被删除 → 1 条
    assert deleted == 3
    assert kept_per_scope.get("cn_stock") == 3
    assert kept_per_scope.get("cn_etf") == 3


# ============================================================================
# 4. 已晋升/已观察/已入组合的 ScanResult 不删除
# ============================================================================

def test_cleanup_protects_promoted_candidate_symbols(db_session):
    """已晋升的 DiscoveryCandidate 对应 symbol 的 ScanResult 不删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000030")
    us = _make_universe_symbol(db_session, symbol="000030")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # 创建 5 个过期 ScanRun（确保超过 min_per_scope）
    for i in range(5):
        r = _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )
    # 第一个 ScanRun 创建已晋升 candidate（保护 sym）
    first_run = db_session.query(ScanRun).order_by(ScanRun.id.asc()).first()
    _make_candidate(
        db_session, scan_run_id=first_run.id, universe_symbol_id=us.id,
        symbol="000030", is_promoted=1, created_at=cutoff - timedelta(days=1),
    )
    db_session.commit()

    deleted, _ = discovery_retention._cleanup_expired_scan_results(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RESULT_RETENTION_DAYS,
        min_per_scope=DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    )
    # 全部 5 个 ScanResult 因 sym 已晋升而保留
    assert deleted == 0
    assert db_session.query(ScanResult).count() == 5


def test_cleanup_protects_position_symbols(db_session):
    """已入组合（Position）的 symbol 的 ScanResult 不删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000031")
    cutoff = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # 5 个过期 ScanRun，每个一个 ScanResult
    for i in range(5):
        r = _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=r.id, symbol_id=sym.id,
            rank_no=i, created_at=cutoff - timedelta(days=1),
        )
    # 创建 Portfolio + Position 保护 sym
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


# ============================================================================
# 5. 删除过期 DiscoveryCandidate
# ============================================================================

def test_cleanup_expired_discovery_candidates(db_session):
    """超过 retention_days 的未晋升 DiscoveryCandidate 物理删除；已晋升不删。"""
    now = _now()
    run = _make_scan_run(db_session, created_at=now)
    cutoff = now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1)

    # 5 个候选：2 个新、3 个旧；其中 1 个旧的已晋升
    for i in range(2):
        us = _make_universe_symbol(db_session, symbol=f"NEW{i}")
        _make_candidate(
            db_session, scan_run_id=run.id, universe_symbol_id=us.id,
            symbol=f"NEW{i}", created_at=now - timedelta(days=1),
        )
    # 3 个旧的（其中 1 个已晋升）
    us_old_promoted = _make_universe_symbol(db_session, symbol="OLD_PROM")
    _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us_old_promoted.id,
        symbol="OLD_PROM", is_promoted=1, created_at=cutoff,
    )
    for i in range(2):
        us = _make_universe_symbol(db_session, symbol=f"OLD{i}")
        _make_candidate(
            db_session, scan_run_id=run.id, universe_symbol_id=us.id,
            symbol=f"OLD{i}", created_at=cutoff,
        )
    db_session.commit()

    deleted = discovery_retention._cleanup_expired_discovery_candidates(
        db_session, now=now,
        retention_days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS,
    )
    # 删除 2 个未晋升的过期候选；已晋升的不删
    assert deleted == 2
    remaining = db_session.query(DiscoveryCandidate).all()
    assert len(remaining) == 3  # 2 新 + 1 已晋升
    assert all(c.is_promoted == 1 or c.created_at > cutoff for c in remaining)


# ============================================================================
# 6. 删除过期 ScanRun 摘要
# ============================================================================

def test_cleanup_old_scan_runs(db_session):
    """超过 retention_days 的 ScanRun 物理删除。"""
    now = _now()
    cutoff = now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1)

    # 5 个 ScanRun：2 新、3 旧
    for i in range(2):
        _make_scan_run(
            db_session, created_at=now - timedelta(days=i + 1),
        )
    for i in range(3):
        _make_scan_run(
            db_session, created_at=cutoff - timedelta(days=i + 1),
        )
    db_session.commit()

    deleted = discovery_retention._cleanup_old_scan_runs(
        db_session, now=now,
        retention_days=DEFAULT_SCAN_RUN_RETENTION_DAYS,
    )
    assert deleted == 3
    assert db_session.query(ScanRun).count() == 2


# ============================================================================
# 7. 删除过期评分快照 items
# ============================================================================

def test_cleanup_old_snapshot_items(db_session):
    """保留每 scope 最近 N 个交易日的 items，删除旧的；superseded items 全部删除。"""
    now = _now()
    # 25 个不同交易日的 ready 快照
    base_date = datetime(2026, 6, 1, 0, 0, 0)
    for i in range(25):
        trade_date = base_date + timedelta(days=i)
        snap = _make_snapshot(
            db_session, scope="cn_stock", status="ready",
            trade_date=trade_date,
        )
        # 每个快照 1 个 item
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            priority_score=70.0 + i,
        )
    # 1 个 superseded 快照（items 应全部删除）
    sup_snap = _make_snapshot(
        db_session, scope="cn_stock", status="superseded",
        trade_date=base_date + timedelta(days=30),
    )
    for i in range(3):
        _make_snapshot_item(
            db_session, snapshot_id=sup_snap.id,
            priority_score=80.0 + i,
        )
    db_session.commit()

    deleted_items, deleted_sup_count = discovery_retention._cleanup_old_snapshot_items(
        db_session, now=now,
        retention_trading_days=DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
    )
    # 最近 20 个交易日保留 → 删除 25 - 20 = 5 个 ready 快照的 items
    # superseded 3 个 items 全部删除
    # 共删除 5 + 3 = 8
    assert deleted_items == 8
    assert deleted_sup_count == 1  # 1 个 superseded 快照被清理 items


def test_cleanup_old_snapshot_items_keeps_recent(db_session):
    """最近 N 个交易日的 items 保留。"""
    now = _now()
    base_date = datetime(2026, 7, 1, 0, 0, 0)
    # 创建 10 个交易日的 ready 快照（< N=20，全部应保留）
    for i in range(10):
        snap = _make_snapshot(
            db_session, scope="cn_stock", status="ready",
            trade_date=base_date + timedelta(days=i),
        )
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            priority_score=70.0 + i,
        )
    db_session.commit()

    deleted_items, _ = discovery_retention._cleanup_old_snapshot_items(
        db_session, now=now,
        retention_trading_days=DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
    )
    # 10 < 20，全部保留
    assert deleted_items == 0
    assert db_session.query(DiscoveryScoreSnapshotItem).count() == 10


# ============================================================================
# 8. 删除 superseded 快照主表
# ============================================================================

def test_cleanup_superseded_snapshots(db_session):
    """superseded 且无引用的快照主表删除；ready / building / failed 保留。"""
    # 3 个 superseded 快照
    sup_ids = []
    for i in range(3):
        s = _make_snapshot(
            db_session, scope="cn_stock", status="superseded",
            trade_date=datetime(2026, 6, 1) + timedelta(days=i),
        )
        sup_ids.append(s.id)
    # 2 个 ready 快照
    for i in range(2):
        _make_snapshot(
            db_session, scope="cn_stock", status="ready",
            trade_date=datetime(2026, 7, 1) + timedelta(days=i),
        )
    db_session.commit()

    deleted = discovery_retention._cleanup_superseded_snapshots(db_session)
    assert deleted == 3

    remaining = db_session.query(DiscoveryScoreSnapshot).all()
    assert len(remaining) == 2
    assert all(s.status == "ready" for s in remaining)


def test_cleanup_superseded_snapshots_keeps_referenced(db_session):
    """被 ScanRun.snapshot_id 引用的 superseded 快照不删除。"""
    now = _now()
    sup_snap = _make_snapshot(
        db_session, scope="cn_stock", status="superseded",
        trade_date=now,
    )
    # 创建一个引用此快照的 ScanRun
    _make_scan_run(
        db_session, created_at=now, snapshot_id=sup_snap.id,
    )
    db_session.commit()

    deleted = discovery_retention._cleanup_superseded_snapshots(db_session)
    assert deleted == 0
    # 快照主表保留
    assert db_session.query(DiscoveryScoreSnapshot).count() == 1


# ============================================================================
# 9. 分层清理聚合 cleanup_expired_discovery_results_layered
# ============================================================================

def test_layered_cleanup_aggregation(db_session):
    """聚合清理：CleanupReport 各字段正确。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000050")
    us = _make_universe_symbol(db_session, symbol="000050")
    cutoff_7d = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)
    cutoff_180d = now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1)
    cutoff_5d = now - timedelta(days=DEFAULT_CANDIDATE_DISPLAY_DAYS + 1)

    # 准备数据：
    # - 5 个过期 ScanRun（180+ 天前）+ 5 个新 ScanRun
    # - 给每个过期 ScanRun 写 1 个 ScanResult（共 5 个过期 ScanResult）
    #   但最近 3 个 ScanRun 保留 → 删除 2 个 ScanRun 的 ScanResult
    #   注：过期 ScanRun 在 step 4 也会被删除
    # - 3 个过期未晋升 DiscoveryCandidate
    # - 3 个过期 ready 快照 items（25 个交易日，保留 20）
    # - 2 个 superseded 快照
    for i in range(5):
        old_run = _make_scan_run(
            db_session, created_at=cutoff_180d - timedelta(days=i + 1),
        )
        _make_scan_result(
            db_session, scan_run_id=old_run.id, symbol_id=sym.id,
            rank_no=i, created_at=cutoff_7d,
        )
    for i in range(5):
        _make_scan_run(db_session, created_at=now - timedelta(days=i + 1))

    # DiscoveryCandidate
    run_for_cand = db_session.query(ScanRun).first()
    for i in range(3):
        us_c = _make_universe_symbol(db_session, symbol=f"CAND{i}")
        _make_candidate(
            db_session, scan_run_id=run_for_cand.id, universe_symbol_id=us_c.id,
            symbol=f"CAND{i}", created_at=cutoff_7d,
        )

    # Snapshot items: 25 个交易日的 ready 快照
    base_date = datetime(2026, 1, 1, 0, 0, 0)
    for i in range(25):
        snap = _make_snapshot(
            db_session, scope="cn_stock", status="ready",
            trade_date=base_date + timedelta(days=i),
        )
        _make_snapshot_item(db_session, snapshot_id=snap.id)
    # 2 个 superseded 快照
    for i in range(2):
        _make_snapshot(
            db_session, scope="cn_stock", status="superseded",
            trade_date=base_date + timedelta(days=30 + i),
        )
    db_session.commit()

    report = cleanup_expired_discovery_results_layered(db_session, now=now)

    # 验证：所有字段都有合理值
    assert isinstance(report, CleanupReport)
    assert report.deleted_scan_results >= 0
    assert report.deleted_scan_runs == 5  # 5 个 180+ 天前的 ScanRun
    assert report.deleted_discovery_candidates == 3
    assert report.deleted_snapshot_items >= 5  # 至少 5 个旧 ready items
    assert report.deleted_superseded_snapshots == 2
    assert report.hidden_active_candidates >= 0
    assert isinstance(report.kept_min_scan_runs_per_scope, dict)
    assert isinstance(report.errors, list)
    # 不应有错误
    assert len(report.errors) == 0, f"unexpected errors: {report.errors}"


def test_layered_cleanup_to_dict(db_session):
    """CleanupReport.to_dict 返回完整字段。"""
    report = CleanupReport(
        deleted_scan_results=1,
        deleted_scan_runs=2,
        deleted_discovery_candidates=3,
        deleted_snapshot_items=4,
        deleted_superseded_snapshots=5,
        hidden_active_candidates=6,
        kept_min_scan_runs_per_scope={"cn_stock": 3},
        errors=[{"step": "test", "error_type": "ValueError", "error_message": "x"}],
    )
    d = report.to_dict()
    assert d["deleted_scan_results"] == 1
    assert d["deleted_scan_runs"] == 2
    assert d["deleted_discovery_candidates"] == 3
    assert d["deleted_snapshot_items"] == 4
    assert d["deleted_superseded_snapshots"] == 5
    assert d["hidden_active_candidates"] == 6
    assert d["kept_min_scan_runs_per_scope"] == {"cn_stock": 3}
    assert d["errors"] == [{"step": "test", "error_type": "ValueError", "error_message": "x"}]


# ============================================================================
# 10. 现有 cleanup_expired_discovery_results 向后兼容
# ============================================================================

def test_cleanup_expired_discovery_results_backward_compatible(db_session):
    """旧接口返回 {"deleted": int, "layered_report": dict} 兼容格式。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000060")
    cutoff_180d = now - timedelta(days=DEFAULT_SCAN_RUN_RETENTION_DAYS + 1)
    cutoff_7d = now - timedelta(days=DEFAULT_SCAN_RESULT_RETENTION_DAYS + 1)

    # 创建过期 ScanRun + ScanResult
    old_run = _make_scan_run(
        db_session, created_at=cutoff_180d,
    )
    _make_scan_result(
        db_session, scan_run_id=old_run.id, symbol_id=sym.id,
        created_at=cutoff_7d,
    )
    db_session.commit()

    result = cleanup_expired_discovery_results(db_session)

    # 兼容字段
    assert "deleted" in result
    assert "skipped_frozen" in result
    assert "layered_report" in result
    assert isinstance(result["deleted"], int)
    assert isinstance(result["layered_report"], dict)
    # layered_report 包含所有字段
    layered = result["layered_report"]
    for key in (
        "deleted_scan_results", "deleted_scan_runs", "deleted_discovery_candidates",
        "deleted_snapshot_items", "deleted_superseded_snapshots",
        "hidden_active_candidates", "kept_min_scan_runs_per_scope", "errors",
    ):
        assert key in layered, f"missing key in layered_report: {key}"


def test_cleanup_expired_discovery_results_old_caller_unchanged(db_session):
    """旧调用方读取 result['deleted'] 仍能正常工作。"""
    # 空数据库调用，不应报错
    result = cleanup_expired_discovery_results(db_session)
    assert result["deleted"] == 0
    assert result["layered_report"]["deleted_scan_results"] == 0


# ============================================================================
# 11. 单步失败不阻塞
# ============================================================================

def test_layered_cleanup_step_failure_does_not_block(db_session, monkeypatch):
    """某步骤抛异常时，后续步骤仍执行，errors 记录失败步骤。"""
    now = _now()

    # monkeypatch _cleanup_expired_scan_results 抛异常
    original = discovery_retention._cleanup_expired_scan_results

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated failure for test")

    monkeypatch.setattr(
        discovery_retention, "_cleanup_expired_scan_results", _raise,
    )

    # 准备一些 DiscoveryCandidate 让后续步骤有事可做
    run = _make_scan_run(db_session, created_at=now)
    us = _make_universe_symbol(db_session, symbol="FAIL1")
    cutoff = now - timedelta(days=DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS + 1)
    _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="FAIL1", created_at=cutoff,
    )
    db_session.commit()

    report = cleanup_expired_discovery_results_layered(db_session, now=now)

    # errors 应记录失败步骤
    assert len(report.errors) >= 1
    step_names = [e["step"] for e in report.errors]
    assert "cleanup_expired_scan_results" in step_names
    # 后续步骤仍执行（deleted_discovery_candidates 应为 1）
    assert report.deleted_discovery_candidates == 1
    # 错误消息不含敏感信息（密码/Token 等）
    for err in report.errors:
        msg = err.get("error_message", "")
        assert "password" not in msg.lower()
        assert "token" not in msg.lower()
        assert "secret" not in msg.lower()


# ============================================================================
# 12. 日 K 与 Score 历史不受影响
# ============================================================================

def test_daily_bar_not_affected_by_layered_cleanup(db_session):
    """日 K（DailyBar）不被分层清理删除。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000070")
    # 1000 天前的 DailyBar
    old_date = (now - timedelta(days=1000)).date()
    db_session.add(DailyBar(
        symbol_id=sym.id, trade_date=old_date,
        open=10, high=11, low=9, close=10.5,
        volume=1000, amount=10000,
    ))
    db_session.commit()

    cleanup_expired_discovery_results_layered(db_session, now=now)

    # DailyBar 仍在数据库
    assert db_session.query(DailyBar).count() == 1
    bar = db_session.query(DailyBar).one()
    assert bar.trade_date == old_date


def test_score_history_not_affected_by_layered_cleanup(db_session):
    """Score 历史不被分层清理删除（约束：至少 250 个交易日）。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000071")
    # 1000 天前的 Score
    old_date = (now - timedelta(days=1000)).date()
    db_session.add(Score(
        symbol_id=sym.id, trade_date=old_date,
        quality_score=70, quality_grade="A",
        timing_score=65, stage="accumulate", action="buy",
        priority_score=75, weight_mode="manual",
    ))
    db_session.commit()

    cleanup_expired_discovery_results_layered(db_session, now=now)

    # Score 仍在数据库
    assert db_session.query(Score).count() == 1
    sc = db_session.query(Score).one()
    assert sc.trade_date == old_date


# ============================================================================
# 13. 候选晋升为观察项时复制评分快照
# ============================================================================

def test_promote_candidate_to_observation_copies_score_snapshot(db_session):
    """候选晋升为观察项时，score_snapshot_json 应包含入选评分。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000080")
    us = _make_universe_symbol(db_session, symbol="000080")
    run = _make_scan_run(db_session, created_at=now)
    cand = _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000080", is_promoted=0,
        priority_score=82.5, quality_score=78.0, timing_score=70.0,
    )
    wl = _make_watchlist(db_session, name="观察池-1")
    db_session.commit()

    result = promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
    )

    assert result["ok"] is True
    assert result["candidate_id"] == cand.id
    assert result["symbol"] == "000080"
    assert result["already_in_watchlist"] is False
    assert result["watchlist_item_id"] is not None

    # 验证 WatchlistItem 已创建且 score_snapshot_json 含评分
    item = db_session.get(WatchlistItem, result["watchlist_item_id"])
    assert item is not None
    assert item.score_snapshot_json is not None
    payload = json.loads(item.score_snapshot_json)
    assert payload["quality_score"] == 78.0
    assert payload["timing_score"] == 70.0
    assert payload["priority_score"] == 82.5
    assert payload["source"] == "discovery_candidate"
    assert payload["candidate_id"] == cand.id

    # 候选 is_promoted 应标记为 1
    db_session.refresh(cand)
    assert cand.is_promoted == 1
    assert cand.promoted_at is not None


def test_promote_candidate_to_observation_with_snapshot_item(db_session):
    """提供 snapshot_item_id 时，score_snapshot_json 优先使用快照明细。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000081")
    us = _make_universe_symbol(db_session, symbol="000081")
    run = _make_scan_run(db_session, created_at=now)
    cand = _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000081", is_promoted=0,
        priority_score=70.0,  # 候选自身评分
    )
    # 创建 snapshot + item，priority_score 更高
    snap = _make_snapshot(db_session, scope="cn_stock", status="ready")
    snap_item = _make_snapshot_item(
        db_session, snapshot_id=snap.id,
        universe_symbol_id=us.id, symbol_id=sym.id,
        priority_score=95.0,  # 快照评分
    )
    wl = _make_watchlist(db_session, name="观察池-2")
    db_session.commit()

    result = promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
        snapshot_item_id=snap_item.id,
    )

    assert result["ok"] is True
    item = db_session.get(WatchlistItem, result["watchlist_item_id"])
    payload = json.loads(item.score_snapshot_json)
    # 应使用 snapshot_item 的 priority_score（95.0），而非 candidate 的 70.0
    assert payload["priority_score"] == 95.0
    assert payload["snapshot_item_id"] == snap_item.id


def test_promote_candidate_to_observation_idempotent(db_session):
    """重复调用 promote_candidate_to_observation 不创建重复 WatchlistItem。"""
    now = _now()
    sym = _make_symbol(db_session, symbol="000082")
    us = _make_universe_symbol(db_session, symbol="000082")
    run = _make_scan_run(db_session, created_at=now)
    cand = _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000082", is_promoted=0,
    )
    wl = _make_watchlist(db_session, name="观察池-3")
    db_session.commit()

    r1 = promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
    )
    r2 = promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
    )

    assert r1["ok"] is True
    assert r2["ok"] is True
    # 第二次 already_in_watchlist=True，watchlist_item_id 与第一次相同
    assert r1["already_in_watchlist"] is False
    assert r2["already_in_watchlist"] is True
    assert r1["watchlist_item_id"] == r2["watchlist_item_id"]
    # 数据库只有 1 条 WatchlistItem
    assert db_session.query(WatchlistItem).count() == 1


def test_promote_candidate_to_observation_not_found(db_session):
    """候选不存在时返回 ok=False。"""
    wl = _make_watchlist(db_session, name="观察池-4")
    db_session.commit()

    result = promote_candidate_to_observation(
        db_session, candidate_id=99999, watchlist_id=wl.id,
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_promote_candidate_to_observation_score_snapshot_survives_cleanup(db_session):
    """评分快照已晋升为观察项后，候选清理不影响 WatchlistItem.score_snapshot_json。

    验证 spec："候选清理后底层快照仍可复用"——快照数据已复制为正式 JSON，
    不再依赖快照表，清理候选/快照 items 后 WatchlistItem 仍保留评分。
    """
    now = _now()
    sym = _make_symbol(db_session, symbol="000083")
    us = _make_universe_symbol(db_session, symbol="000083")
    run = _make_scan_run(db_session, created_at=now)
    cand = _make_candidate(
        db_session, scan_run_id=run.id, universe_symbol_id=us.id,
        symbol="000083", is_promoted=0,
        priority_score=88.0,
    )
    snap = _make_snapshot(db_session, scope="cn_stock", status="ready")
    snap_item = _make_snapshot_item(
        db_session, snapshot_id=snap.id,
        universe_symbol_id=us.id, symbol_id=sym.id,
        priority_score=88.0,
    )
    wl = _make_watchlist(db_session, name="观察池-5")
    db_session.commit()

    # 晋升
    result = promote_candidate_to_observation(
        db_session, candidate_id=cand.id, watchlist_id=wl.id,
        snapshot_item_id=snap_item.id,
    )
    item_id = result["watchlist_item_id"]

    # 触发分层清理（标记 snap 为 superseded，然后清理）
    snap.status = "superseded"
    db_session.commit()

    cleanup_expired_discovery_results_layered(db_session, now=now)

    # WatchlistItem 仍存在，score_snapshot_json 仍可读
    item = db_session.get(WatchlistItem, item_id)
    assert item is not None
    assert item.score_snapshot_json is not None
    payload = json.loads(item.score_snapshot_json)
    assert payload["priority_score"] == 88.0
    assert payload["candidate_id"] == cand.id
