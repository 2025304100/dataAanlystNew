"""白盒测试 - WP-P.6 相同参数结果复用。

覆盖：
1. cache_key 生成稳定（相同参数 → 相同 cache_key；不同参数 → 不同 cache_key；
   dict 顺序无关）
2. 首次扫描 cache_hit=False，写入新 ScanRun
3. 二次扫描相同参数 cache_hit=True，返回 cached_from_scan_run_id，
   result_rows_written=0，filter_stats=None
4. 不同 snapshot 不命中缓存（snapshot_id 不同）
5. 相同 snapshot 不同参数不命中（cache_key 不同）
6. ScanRun 摘要写入（cache_key / total_in_snapshot / coarse_match_count /
   advanced_match_count / result_rows_written / cache_hit / snapshot_id /
   degraded_reason）
7. ScanResult 只物化 Top 300（snapshot 内有 1000 个 items，limit=300 →
   只写入 300 条）
8. 缓存命中后 ScanResult 不重复写入（第一次 300 条，第二次命中缓存总数仍 300）
9. schema 补丁幂等（_ensure_sqlite_scan_run_cache_columns 调用两次不报错）

硬约束验证（参照 project_memory）：
- 缓存命中时绝不重复写入 ScanResult
- ScanResult 写入数量严格受 limit（默认 300）限制
- 不修改已稳定 _build_cache_key 逻辑
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.init_db import _ensure_sqlite_scan_run_cache_columns
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.scan import ScanResult, ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services import discovery_fast_scan

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数
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
    quality_score: float = 70.0,
    timing_score: float = 65.0,
    priority_score: float = 75.0,
    stage: str = "accumulate",
    action: str = "buy",
    data_credibility: float | None = 0.8,
) -> DiscoveryScoreSnapshotItem:
    item = DiscoveryScoreSnapshotItem(
        snapshot_id=snapshot_id,
        universe_symbol_id=universe_symbol_id,
        symbol_id=symbol_id,
        quality_score=quality_score,
        timing_score=timing_score,
        priority_score=priority_score,
        stage=stage,
        action=action,
        data_credibility=data_credibility,
    )
    db_session.add(item)
    db_session.flush()
    return item


def _setup_snapshot_with_n_items(
    db_session,
    *,
    item_count: int,
    snapshot_id: int | None = None,
    base_score: float = 80.0,
    scope: str = "cn_stock",
    generated_at: datetime | None = None,
    trade_date: datetime | None = None,
    symbol_prefix: str = "S",
) -> tuple[DiscoveryScoreSnapshot, list[Symbol]]:
    """创建快照 + N 个 item，priority_score 从 base_score 递减。

    返回 (snapshot, symbols)。

    Args:
        symbol_prefix: 标的代码前缀，用于在同一测试内创建多个快照时避免
            symbols.symbol 唯一约束冲突（不同快照使用不同前缀）。
    """
    if generated_at is None:
        generated_at = datetime(2026, 7, 19, 10, 0, 0)
    if snapshot_id is None:
        snap = _make_snapshot(
            db_session,
            scope=scope,
            status="ready",
            trade_date=trade_date or datetime(2026, 7, 19, 0, 0, 0),
            generated_at=generated_at,
            symbol_count=item_count,
        )
        snapshot_id = snap.id
    else:
        snap = db_session.get(DiscoveryScoreSnapshot, snapshot_id)

    symbols: list[Symbol] = []
    for i in range(item_count):
        # 使用 symbol_prefix 区分不同快照下的标的，避免唯一约束冲突
        sym_code = f"{symbol_prefix}{item_count:04d}_{i:04d}"
        sym = _make_symbol(
            db_session,
            symbol=sym_code,
            asset_type="stock" if i % 2 == 0 else "etf",
            industry="金融" if i % 3 == 0 else "消费",
        )
        us = _make_universe_symbol(db_session, symbol=sym_code)
        _make_snapshot_item(
            db_session,
            snapshot_id=snapshot_id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=base_score - i * 0.01,  # 严格递减避免并列
            quality_score=base_score - 5 - i * 0.01,
            timing_score=base_score - 10 - i * 0.01,
            stage="accumulate" if i % 2 == 0 else "breakout",
            action="buy" if i % 2 == 0 else "watch",
            data_credibility=0.5 + (i % 10) * 0.05,
        )
        symbols.append(sym)
    db_session.flush()
    return snap, symbols


# ============================================================================
# 1. cache_key 生成稳定
# ============================================================================

def test_cache_key_same_params_same_value(db_session):
    """相同参数 → 相同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id,
        scope="cn_stock",
        min_score=55,
        asset_types=["stock", "etf"],
        stages=["accumulate"],
        actions=["buy"],
        indicator_plan={"ind1": {"formula": "close"}},
        portfolio_id=42,
        portfolio_rule_id=7,
        limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id,
        scope="cn_stock",
        min_score=55,
        asset_types=["stock", "etf"],
        stages=["accumulate"],
        actions=["buy"],
        indicator_plan={"ind1": {"formula": "close"}},
        portfolio_id=42,
        portfolio_rule_id=7,
        limit=300,
    )
    assert k1 == k2


def test_cache_key_different_min_score_differs(db_session):
    """不同 min_score → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=70,
        asset_types=None, stages=None, actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    assert k1 != k2


def test_cache_key_different_asset_types_differs(db_session):
    """不同 asset_types → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=["stock"], stages=None, actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=["etf"], stages=None, actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    assert k1 != k2


def test_cache_key_different_stages_differs(db_session):
    """不同 stages → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=["accumulate"], actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=["breakout"], actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    assert k1 != k2


def test_cache_key_different_actions_differs(db_session):
    """不同 actions → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=["buy"],
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=["watch"],
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    assert k1 != k2


def test_cache_key_different_portfolio_id_differs(db_session):
    """不同 portfolio_id → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan=None, portfolio_id=1, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan=None, portfolio_id=2, portfolio_rule_id=None, limit=300,
    )
    assert k1 != k2


def test_cache_key_different_portfolio_rule_id_differs(db_session):
    """不同 portfolio_rule_id → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=1, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan=None, portfolio_id=None, portfolio_rule_id=2, limit=300,
    )
    assert k1 != k2


def test_cache_key_different_indicator_plan_differs(db_session):
    """不同 indicator_plan → 不同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan={"ind1": {"formula": "close"}},
        portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=None, stages=None, actions=None,
        indicator_plan={"ind1": {"formula": "open"}},  # 公式不同
        portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    assert k1 != k2


def test_cache_key_dict_order_independent(db_session):
    """dict 顺序无关：{"a":1,"b":2} 与 {"b":2,"a":1} → 相同 cache_key。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    k1 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=["stock", "etf"],  # 顺序 1
        stages=["accumulate", "breakout"],  # 顺序 1
        actions=["buy", "watch"],  # 顺序 1
        indicator_plan={"b": {"formula": "close"}, "a": {"formula": "open"}},  # 顺序 1
        portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    k2 = discovery_fast_scan._build_cache_key(
        snapshot_id=snap.id, scope="cn_stock", min_score=55,
        asset_types=["etf", "stock"],  # 顺序 2
        stages=["breakout", "accumulate"],  # 顺序 2
        actions=["watch", "buy"],  # 顺序 2
        indicator_plan={"a": {"formula": "open"}, "b": {"formula": "close"}},  # 顺序 2
        portfolio_id=None, portfolio_rule_id=None, limit=300,
    )
    assert k1 == k2


# ============================================================================
# 2. 首次扫描 cache_hit=False
# ============================================================================

def test_first_scan_no_cache_hit(db_session):
    """无历史 ScanRun → 首次扫描 cache_hit=False，写入新 ScanRun。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=3)
    db_session.commit()

    # 扫描前 ScanRun 表为空
    pre_count = db_session.query(ScanRun).count()
    assert pre_count == 0

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    assert result["cache_hit"] is False
    assert result["cached_from_scan_run_id"] is None
    assert result["result_rows_written"] > 0
    assert result["cache_key"] is not None

    # ScanRun 表新增 1 条
    post_count = db_session.query(ScanRun).count()
    assert post_count == 1


# ============================================================================
# 3. 二次扫描 cache_hit=True
# ============================================================================

def test_second_scan_same_params_cache_hit(db_session):
    """相同参数二次扫描 → cache_hit=True，返回 cached_from_scan_run_id。"""
    _setup_snapshot_with_n_items(db_session, item_count=3)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 首次未命中
    assert r1["cache_hit"] is False
    assert r1["cached_from_scan_run_id"] is None
    assert r1["result_rows_written"] > 0
    assert r1["filter_stats"] is not None  # 未命中时返回完整 filter_stats

    # 二次命中
    assert r2["cache_hit"] is True
    assert r2["cached_from_scan_run_id"] is not None
    assert r2["result_rows_written"] == 0  # 命中缓存不重复写
    assert r2["filter_stats"] is None  # 命中缓存无需重新过滤
    assert r2["cached_at"] is not None

    # 命中返回的 cached_from_scan_run_id 应为首次写入的 ScanRun id
    scan_runs = db_session.query(ScanRun).order_by(ScanRun.id.asc()).all()
    assert len(scan_runs) == 1  # 只写入 1 条 ScanRun（第二次命中不重复写）
    assert r2["cached_from_scan_run_id"] == scan_runs[0].id


def test_second_scan_duration_much_smaller(db_session):
    """二次扫描 duration_ms 应远小于首次（命中缓存无需重新过滤）。"""
    _setup_snapshot_with_n_items(db_session, item_count=5)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 缓存命中应明显更快（宽松断言，避免 CI 抖动）
    assert r2["duration_ms"] <= r1["duration_ms"]
    assert r2["cache_hit"] is True


def test_second_scan_results_match_first(db_session):
    """二次扫描返回的 results 与首次一致（symbol_id 集合相同）。"""
    _setup_snapshot_with_n_items(db_session, item_count=3)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    ids1 = {r["symbol_id"] for r in r1["results"]}
    ids2 = {r["symbol_id"] for r in r2["results"]}
    assert ids1 == ids2


# ============================================================================
# 4. 不同 snapshot 不命中缓存
# ============================================================================

def test_different_snapshot_no_cache_hit(db_session):
    """不同 snapshot_id → cache_hit=False（cache_key 内 snapshot_id 不同）。"""
    # 第一个快照 + items（使用前缀 S1 避免与 snap2 标的代码冲突）
    snap1, _ = _setup_snapshot_with_n_items(
        db_session,
        item_count=3,
        trade_date=datetime(2026, 7, 18, 0, 0, 0),
        generated_at=datetime(2026, 7, 18, 10, 0, 0),
        symbol_prefix="S1",
    )
    db_session.commit()

    # 第一次扫描 snap1
    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r1["cache_hit"] is False
    assert r1["snapshot_id"] == snap1.id

    # 创建第二个更新的 ready 快照（同 scope 不同 trade_date，前缀 S2）
    snap2, _ = _setup_snapshot_with_n_items(
        db_session,
        item_count=3,
        trade_date=datetime(2026, 7, 19, 0, 0, 0),
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_prefix="S2",
    )
    db_session.commit()

    # 第二次扫描应读到 snap2（更新），且不命中 snap1 的缓存
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r2["snapshot_id"] == snap2.id
    assert r2["cache_hit"] is False  # snapshot_id 不同 → cache_key 不同 → 不命中
    assert r2["cached_from_scan_run_id"] is None


# ============================================================================
# 5. 相同 snapshot 不同参数不命中
# ============================================================================

def test_same_snapshot_different_params_no_cache_hit(db_session):
    """相同 snapshot 但 min_score 不同 → cache_hit=False（cache_key 不同）。"""
    _setup_snapshot_with_n_items(db_session, item_count=5)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=70, db=db_session,  # 不同 min_score
    )

    assert r1["cache_hit"] is False
    assert r2["cache_hit"] is False
    assert r1["cache_key"] != r2["cache_key"]


def test_same_snapshot_different_asset_types_no_cache_hit(db_session):
    """相同 snapshot 但 asset_types 不同 → cache_hit=False。"""
    _setup_snapshot_with_n_items(db_session, item_count=5)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, asset_types=["stock"],
        db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, asset_types=["etf"],
        db=db_session,
    )

    assert r1["cache_hit"] is False
    assert r2["cache_hit"] is False
    assert r1["cache_key"] != r2["cache_key"]


# ============================================================================
# 6. ScanRun 摘要写入
# ============================================================================

def test_scan_run_summary_fields_written(db_session):
    """调用 run_fast_scan 后 ScanRun 应写入 cache_key 与摘要统计字段。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=5)
    db_session.commit()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    scan_run = db_session.query(ScanRun).one()
    # cache_key 必须写入
    assert scan_run.cache_key is not None
    assert isinstance(scan_run.cache_key, str)
    assert len(scan_run.cache_key) > 0
    # snapshot_id 必须写入
    assert scan_run.snapshot_id == snap.id
    # cache_hit 新建时为 0
    assert scan_run.cache_hit == 0
    # 摘要统计字段已写入
    assert scan_run.total_in_snapshot == 5  # snapshot 内 item 总数
    assert scan_run.coarse_match_count == 5  # 全部通过 min_score=55
    assert scan_run.advanced_match_count == 5  # 无 indicator_plan / portfolio_filter
    assert scan_run.result_rows_written == 5  # 写入 5 条 ScanResult
    # degraded_reason 在正常扫描时为 None
    assert scan_run.degraded_reason is None


def test_scan_run_cache_key_format(db_session):
    """ScanRun.cache_key 应与 run_fast_scan 返回的 cache_key 一致。"""
    _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    scan_run = db_session.query(ScanRun).one()
    assert scan_run.cache_key == result["cache_key"]


# ============================================================================
# 7. ScanResult 只物化 Top 300
# ============================================================================

def test_scan_result_top_k_limit(db_session):
    """snapshot 内有 1000 个 items，limit=300 → ScanResult 只写 300 条。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=1000)
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, limit=300, db=db_session,
    )

    # 返回的 results 数量受 limit 限制
    assert len(result["results"]) == 300
    assert result["result_rows_written"] == 300

    # 数据库中 ScanResult 总数也受 limit 限制
    scan_result_count = db_session.query(ScanResult).count()
    assert scan_result_count == 300
    # 不为所有 1000 个标的写三类结果
    assert scan_result_count <= 300


def test_scan_result_default_limit_300(db_session):
    """默认 limit=300 → 即使 snapshot 内有 500 个 items 也只写 300 条。"""
    _setup_snapshot_with_n_items(db_session, item_count=500)
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,  # 使用默认 limit=300
    )

    assert len(result["results"]) == 300
    assert result["result_rows_written"] == 300
    assert db_session.query(ScanResult).count() == 300


def test_scan_result_only_executable_type(db_session):
    """ScanResult 只写最终 executable 候选，不为每个标的写 Quality / Timing 三类。"""
    _setup_snapshot_with_n_items(db_session, item_count=10)
    db_session.commit()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 所有 ScanResult 的 result_type 都是 "executable"
    results = db_session.query(ScanResult).all()
    assert len(results) == 10
    assert all(r.result_type == "executable" for r in results)


# ============================================================================
# 8. 缓存命中后 ScanResult 不重复写入
# ============================================================================

def test_cache_hit_does_not_duplicate_scan_results(db_session):
    """第一次写入 N 条 ScanResult，第二次命中缓存 → 总数仍为 N。"""
    _setup_snapshot_with_n_items(db_session, item_count=5)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    after_first = db_session.query(ScanResult).count()
    assert after_first == r1["result_rows_written"]
    assert after_first == 5

    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    after_second = db_session.query(ScanResult).count()

    # 命中缓存 → ScanResult 不重复写
    assert r2["cache_hit"] is True
    assert r2["result_rows_written"] == 0
    assert after_second == after_first  # 总数不变


def test_cache_hit_does_not_create_new_scan_run(db_session):
    """缓存命中不创建新 ScanRun（只复用已有）。"""
    _setup_snapshot_with_n_items(db_session, item_count=3)
    db_session.commit()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    after_first = db_session.query(ScanRun).count()
    assert after_first == 1

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    after_second = db_session.query(ScanRun).count()
    # 命中缓存不创建新 ScanRun
    assert after_second == after_first


def test_cache_hit_returns_cached_scan_run_id(db_session):
    """缓存命中返回的 cached_from_scan_run_id 指向首次创建的 ScanRun。"""
    _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    first_scan_run = db_session.query(ScanRun).one()
    assert r2["cached_from_scan_run_id"] == first_scan_run.id
    assert r2["cached_at"] == first_scan_run.created_at


# ============================================================================
# 9. schema 补丁幂等
# ============================================================================

def test_ensure_sqlite_scan_run_cache_columns_idempotent():
    """_ensure_sqlite_scan_run_cache_columns 调用两次不报错（幂等）。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    # 第一次调用：补齐缺失列
    _ensure_sqlite_scan_run_cache_columns(engine)
    # 第二次调用：列已存在，不应报错
    _ensure_sqlite_scan_run_cache_columns(engine)

    # 验证所有新增字段都存在
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("scan_runs")}
    expected_new_columns = {
        "snapshot_id",
        "cache_key",
        "cache_hit",
        "total_in_snapshot",
        "coarse_match_count",
        "advanced_match_count",
        "result_rows_written",
        "degraded_reason",
    }
    assert expected_new_columns.issubset(columns)


def test_ensure_sqlite_scan_run_cache_columns_on_old_schema():
    """在旧 schema（无新增列）上调用补丁应正确补齐字段。"""
    # 创建只包含原 scan_runs 字段的旧表
    engine = create_engine("sqlite:///:memory:")
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE scan_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "preset_id INTEGER, "
            "run_name TEXT, "
            "scope_snapshot TEXT, "
            "filters_snapshot TEXT, "
            "portfolio_id INTEGER, "
            "portfolio_rule_id INTEGER, "
            "status TEXT, "
            "started_at DATETIME, "
            "finished_at DATETIME, "
            "created_at DATETIME)"
        ))

    # 调用补丁
    _ensure_sqlite_scan_run_cache_columns(engine)

    # 验证字段已补齐
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("scan_runs")}
    assert "cache_key" in columns
    assert "snapshot_id" in columns
    assert "total_in_snapshot" in columns
    assert "degraded_reason" in columns


# ============================================================================
# 额外场景：缓存命中也不发起 HTTP 请求
# ============================================================================

def test_cache_hit_does_not_initiate_http_requests(db_session, monkeypatch):
    """缓存命中路径也不应发起任何 HTTP 请求。"""
    _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    # 首次扫描写入缓存
    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 安装监控钩子
    def _http_violation(*args, **kwargs):
        raise AssertionError(
            "fast_scan 缓存命中路径不应发起任何 HTTP 请求，"
            f"但调用了 args={args!r} kwargs={kwargs!r}"
        )

    try:
        import urllib.request as _urllib_request
        monkeypatch.setattr(_urllib_request, "urlopen", _http_violation)
    except ImportError:
        pass

    try:
        import requests as _requests
        monkeypatch.setattr(_requests, "get", _http_violation, raising=False)
        monkeypatch.setattr(_requests, "post", _http_violation, raising=False)
        if hasattr(_requests, "Session"):
            monkeypatch.setattr(_requests.Session, "request", _http_violation, raising=False)
    except ImportError:
        pass

    try:
        import httpx as _httpx
        if hasattr(_httpx, "Client"):
            monkeypatch.setattr(_httpx.Client, "get", _http_violation, raising=False)
            monkeypatch.setattr(_httpx.Client, "post", _http_violation, raising=False)
            monkeypatch.setattr(_httpx.Client, "request", _http_violation, raising=False)
    except ImportError:
        pass

    # 第二次扫描命中缓存，不应发起任何 HTTP 请求
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r2["cache_hit"] is True


# ============================================================================
# 额外场景：_find_cached_scan_run 直接测试
# ============================================================================

def test_find_cached_scan_run_returns_none_when_no_match(db_session):
    """无任何 ScanRun 时返回 None。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    result = discovery_fast_scan._find_cached_scan_run(
        db_session, cache_key="nonexistent_key", snapshot_id=snap.id,
    )
    assert result is None


def test_find_cached_scan_run_returns_match(db_session):
    """存在相同 cache_key + snapshot_id 的 done 状态 ScanRun → 返回该 ScanRun。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 用相同的 cache_key 查找
    scan_run = db_session.query(ScanRun).one()
    found = discovery_fast_scan._find_cached_scan_run(
        db_session,
        cache_key=scan_run.cache_key,
        snapshot_id=snap.id,
    )
    assert found is not None
    assert found.id == scan_run.id


def test_find_cached_scan_run_ignores_failed_status(db_session):
    """status=failed 的 ScanRun 不应被命中。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    # 手动插入一条 status=failed 的 ScanRun
    failed_run = ScanRun(
        run_name="failed-scan",
        scope_snapshot="{}",
        filters_snapshot="{}",
        status="failed",
        started_at=datetime(2026, 7, 19, 10, 0, 0),
        finished_at=datetime(2026, 7, 19, 10, 0, 1),
        snapshot_id=snap.id,
        cache_key="some_key",
    )
    db_session.add(failed_run)
    db_session.commit()

    found = discovery_fast_scan._find_cached_scan_run(
        db_session, cache_key="some_key", snapshot_id=snap.id,
    )
    assert found is None  # failed 状态不被命中


def test_find_cached_scan_run_ignores_running_status(db_session):
    """status=running 的 ScanRun 不应被命中。"""
    snap, _ = _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    running_run = ScanRun(
        run_name="running-scan",
        scope_snapshot="{}",
        filters_snapshot="{}",
        status="running",
        started_at=datetime(2026, 7, 19, 10, 0, 0),
        snapshot_id=snap.id,
        cache_key="some_key",
    )
    db_session.add(running_run)
    db_session.commit()

    found = discovery_fast_scan._find_cached_scan_run(
        db_session, cache_key="some_key", snapshot_id=snap.id,
    )
    assert found is None  # running 状态不被命中


def test_find_cached_scan_run_ignores_different_snapshot_id(db_session):
    """snapshot_id 不匹配的 ScanRun 不应被命中。"""
    snap1, _ = _setup_snapshot_with_n_items(
        db_session, item_count=2,
        trade_date=datetime(2026, 7, 18, 0, 0, 0),
        generated_at=datetime(2026, 7, 18, 10, 0, 0),
        symbol_prefix="S1",
    )
    snap2, _ = _setup_snapshot_with_n_items(
        db_session, item_count=2,
        trade_date=datetime(2026, 7, 19, 0, 0, 0),
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_prefix="S2",
    )
    db_session.commit()

    # 在 snap1 上写入 ScanRun，cache_key 含 snap1.id
    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    # 此时读到的 ready 是 snap2（更新），所以 cache_key 内含 snap2.id
    # 现在用 snap1.id + snap2 的 cache_key 查找，应不命中
    snap2_run = db_session.query(ScanRun).filter(ScanRun.snapshot_id == snap2.id).one()

    found = discovery_fast_scan._find_cached_scan_run(
        db_session,
        cache_key=snap2_run.cache_key,
        snapshot_id=snap1.id,  # 不同的 snapshot_id
    )
    assert found is None  # snapshot_id 不匹配不命中


# ============================================================================
# 额外场景：_reuse_cached_results 直接测试
# ============================================================================

def test_reuse_cached_results_returns_correct_structure(db_session):
    """_reuse_cached_results 返回结构正确：results + summary。"""
    _setup_snapshot_with_n_items(db_session, item_count=3)
    db_session.commit()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    scan_run = db_session.query(ScanRun).one()
    results, summary = discovery_fast_scan._reuse_cached_results(db_session, scan_run)

    # results 结构
    assert len(results) == 3
    for r in results:
        assert "symbol_id" in r
        assert "quality_score" in r
        assert "timing_score" in r
        assert "priority_score" in r
        assert "stage" in r
        assert "action" in r

    # summary 结构
    assert summary["cache_hit"] is True
    assert summary["cached_scan_run_id"] == scan_run.id
    assert summary["cached_at"] == scan_run.created_at
    assert summary["result_count"] == 3


def test_reuse_cached_results_does_not_write(db_session):
    """_reuse_cached_results 不应写入新的 ScanResult 或 ScanRun。"""
    _setup_snapshot_with_n_items(db_session, item_count=2)
    db_session.commit()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    before_results = db_session.query(ScanResult).count()
    before_runs = db_session.query(ScanRun).count()

    scan_run = db_session.query(ScanRun).one()
    discovery_fast_scan._reuse_cached_results(db_session, scan_run)

    after_results = db_session.query(ScanResult).count()
    after_runs = db_session.query(ScanRun).count()

    assert after_results == before_results  # 不写 ScanResult
    assert after_runs == before_runs  # 不写 ScanRun
