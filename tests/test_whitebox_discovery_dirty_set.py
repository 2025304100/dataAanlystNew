"""白盒测试 - WP-P.3 增量失效规则。

覆盖：
1. 空库无快照 → compute_dirty_symbols 返回空列表
2. 有 universe 无快照 → 所有标的 dirty（NEW_SYMBOL_ADDED）
3. 有快照且数据未变化 → 返回空
4. 有快照但某标的数据变化 → 各对应 DirtyReason
5. 快照过期 → 所有标的 dirty（SNAPSHOT_EXPIRED）
6. 新快照覆盖时旧快照 superseded
7. 同一 scope 同时只允许一个 building
8. should_trigger_full_rebuild 各场景
9. bar_count < 5 边界（不抛 NameError）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.capital_flow import CapitalFlow
from app.models.daily_bar import DailyBar
from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.models.universe import UniverseSymbol
from app.services.discovery_dirty_set import (
    DirtyReason,
    DirtySymbol,
    compute_dirty_symbols,
    is_snapshot_building_for_scope,
    mark_snapshot_superseded,
    should_trigger_full_rebuild,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数
# ============================================================================

def _naive_utc(dt: datetime) -> datetime:
    """确保 datetime 为 naive UTC。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _make_universe_symbol(
    db_session,
    *,
    symbol: str,
    asset_type: str = "stock",
    region: str = "cn",
    market: str = "sz",
    bar_count: int = 10,
    last_synced_at: datetime | None = None,
    created_at: datetime | None = None,
) -> UniverseSymbol:
    """创建一个 universe_symbol 记录。"""
    if created_at is None:
        created_at = datetime(2026, 7, 1, 0, 0, 0)
    us = UniverseSymbol(
        symbol=symbol,
        name=f"测试标的 {symbol}",
        asset_type=asset_type,
        market=market,
        region=region,
        bar_count=bar_count,
        last_synced_at=last_synced_at,
        is_synced=1,
        created_at=created_at,
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
    """创建一个 Symbol 业务表记录。"""
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
    scoring_config_id: int | None = 1,
    scoring_config_version: int | None = 1,
    weight_mode: str | None = "manual",
    factor_model_run_id: str | None = None,
) -> DiscoveryScoreSnapshot:
    """创建一个快照记录。"""
    if trade_date is None:
        trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = DiscoveryScoreSnapshot(
        scope=scope,
        trade_date=trade_date,
        status=status,
        scoring_config_id=scoring_config_id,
        scoring_config_version=scoring_config_version,
        weight_mode=weight_mode,
        factor_model_run_id=factor_model_run_id,
        generated_at=generated_at,
    )
    db_session.add(snap)
    db_session.flush()
    return snap


# ============================================================================
# 1. 空库无快照
# ============================================================================

def test_empty_db_no_snapshot_returns_empty(db_session):
    """空库（无 universe 数据）+ 无快照 → compute_dirty_symbols 返回空列表。"""
    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=None
    )
    assert result == []


def test_empty_db_no_snapshot_triggers_full_rebuild(db_session):
    """空库 + 无快照 → should_trigger_full_rebuild 返回 True。"""
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=None
    )
    assert trigger is True
    assert reason == "no_snapshot"


# ============================================================================
# 2. 有 universe 无快照
# ============================================================================

def test_universe_no_snapshot_all_dirty_new_symbol(db_session):
    """有 5 个 universe_symbols + 无快照 → 5 个 dirty，原因 NEW_SYMBOL_ADDED。"""
    for i in range(5):
        _make_universe_symbol(
            db_session,
            symbol=f"00000{i}",
            bar_count=10,
            created_at=datetime(2026, 7, 1, 0, 0, 0),
        )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=None
    )
    assert len(result) == 5
    for dirty in result:
        assert DirtyReason.NEW_SYMBOL_ADDED in dirty.reasons
        assert dirty.universe_symbol_id is not None
        assert dirty.symbol is not None


def test_universe_no_snapshot_triggers_full_rebuild(db_session):
    """有 universe + 无快照 → should_trigger_full_rebuild 返回 True。"""
    _make_universe_symbol(db_session, symbol="000001", bar_count=10)
    db_session.commit()

    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=None
    )
    assert trigger is True
    assert reason == "no_snapshot"


# ============================================================================
# 3. 有快照且数据未变化
# ============================================================================

def test_snapshot_no_data_change_returns_empty(db_session):
    """有快照 + 所有数据 updated_at 都早于快照 → 返回空。"""
    before_snapshot = datetime(2026, 7, 18, 9, 0, 0)
    snapshot_time = datetime(2026, 7, 19, 10, 0, 0)

    # 创建 3 个 universe_symbols，bar_count 充足，last_synced_at 早于快照
    for i in range(3):
        _make_universe_symbol(
            db_session,
            symbol=f"00000{i}",
            bar_count=10,
            last_synced_at=before_snapshot,
            created_at=datetime(2026, 7, 1, 0, 0, 0),
        )
    db_session.commit()

    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert result == []


# ============================================================================
# 4. 有快照但某标的数据变化
# ============================================================================

def test_snapshot_with_various_data_changes(db_session):
    """3 个标的分别有不同的数据变化 → 各对应 DirtyReason。

    - 标的 0：daily_bar updated_at 晚于快照 → LATEST_BAR_CHANGED
    - 标的 1：stock_valuation updated_at 晚于快照 → FINANCIAL_REPORT_UPDATED
    - 标的 2：capital_flow / hot_rank / lhb / tail_accumulation 各变化 → 4 个原因
    """
    before_snapshot = datetime(2026, 7, 18, 9, 0, 0)
    snapshot_time = datetime(2026, 7, 19, 10, 0, 0)
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 创建 3 个 universe_symbols + 3 个 Symbol 业务记录
    universe_symbols = []
    symbols = []
    for i in range(3):
        us = _make_universe_symbol(
            db_session,
            symbol=f"00000{i}",
            bar_count=10,  # bar_count 充足，避免触发 bar_count < 5
            last_synced_at=before_snapshot,
            created_at=datetime(2026, 7, 1, 0, 0, 0),
        )
        universe_symbols.append(us)
        sym = _make_symbol(db_session, symbol=f"00000{i}")
        symbols.append(sym)

    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()

    # 标的 0：daily_bar 在快照后新增
    db_session.add(DailyBar(
        symbol_id=symbols[0].id,
        trade_date=date(2026, 7, 19),
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000000.0,
        created_at=after_snapshot,
    ))

    # 标的 1：stock_valuation 在快照后新增
    db_session.add(StockValuation(
        symbol_id=symbols[1].id,
        trade_date=date(2026, 7, 19),
        pe_ttm=15.0,
        created_at=after_snapshot,
    ))

    # 标的 2：capital_flow / hot_rank / lhb / tail_accumulation 在快照后新增
    db_session.add(CapitalFlow(
        symbol_id=symbols[2].id,
        trade_date=date(2026, 7, 19),
        main_net_inflow=1000000.0,
        created_at=after_snapshot,
    ))
    db_session.add(StockHotRankSnapshot(
        symbol="000002",
        trade_date=date(2026, 7, 19),
        hot_rank=10,
        hot_rank_total=5000,
        hot_rank_pct=99.8,
        created_at=after_snapshot,
    ))
    db_session.add(LhbInstitutionTrade(
        symbol="000002",
        trade_date=date(2026, 7, 19),
        buyer_institution_count=3,
        created_at=after_snapshot,
    ))
    db_session.add(TailAccumulationSnapshot(
        symbol="000002",
        trade_date=date(2026, 7, 19),
        minute_count=240,
        tail_minute_count=30,
        day_amount=100000000.0,
        tail_amount=10000000.0,
        tail_amount_share=0.1,
        tail_activity_ratio=2.0,
        tail_return=0.005,
        close_location=0.8,
        proxy_score=70.0,
        created_at=after_snapshot,
    ))
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap
    )

    # 应返回 3 个 dirty 标的
    assert len(result) == 3

    # 按 symbol 找到对应的 dirty 记录
    dirty_by_symbol = {d.symbol: d for d in result}

    # 标的 0：LATEST_BAR_CHANGED
    d0 = dirty_by_symbol["000000"]
    assert DirtyReason.LATEST_BAR_CHANGED in d0.reasons
    assert d0.symbol_id == symbols[0].id

    # 标的 1：FINANCIAL_REPORT_UPDATED
    d1 = dirty_by_symbol["000001"]
    assert DirtyReason.FINANCIAL_REPORT_UPDATED in d1.reasons
    # 标的 1 不应有 LATEST_BAR_CHANGED（bar_count 充足且无 daily_bar 新记录）
    assert DirtyReason.LATEST_BAR_CHANGED not in d1.reasons

    # 标的 2：4 个原因全部命中
    d2 = dirty_by_symbol["000002"]
    assert DirtyReason.CAPITAL_FLOW_UPDATED in d2.reasons
    assert DirtyReason.HOT_RANK_UPDATED in d2.reasons
    assert DirtyReason.LHB_UPDATED in d2.reasons
    assert DirtyReason.TAIL_ACCUMULATION_UPDATED in d2.reasons


def test_snapshot_with_universe_resynced(db_session):
    """universe_symbols.last_synced_at 晚于快照 → UNIVERSE_RESYNCED。"""
    snapshot_time = datetime(2026, 7, 19, 10, 0, 0)
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    us = _make_universe_symbol(
        db_session,
        symbol="000001",
        bar_count=10,
        last_synced_at=after_snapshot,  # 晚于快照
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert len(result) == 1
    assert DirtyReason.UNIVERSE_RESYNCED in result[0].reasons


# ============================================================================
# 5. 快照过期
# ============================================================================

def test_snapshot_expired_all_dirty(db_session):
    """快照 generated_at = now - 10 天，snapshot_max_age_days=7 → 所有标的 dirty，原因 SNAPSHOT_EXPIRED。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired_snapshot_time = now - timedelta(days=10)

    for i in range(3):
        _make_universe_symbol(
            db_session,
            symbol=f"00000{i}",
            bar_count=10,
            last_synced_at=expired_snapshot_time - timedelta(hours=1),
            created_at=datetime(2026, 7, 1, 0, 0, 0),
        )
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=expired_snapshot_time,
        trade_date=expired_snapshot_time,
    )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session,
        scope="cn_stock",
        current_snapshot=snap,
        snapshot_max_age_days=7,
    )
    assert len(result) == 3
    for dirty in result:
        assert DirtyReason.SNAPSHOT_EXPIRED in dirty.reasons


# ============================================================================
# 6. 新快照覆盖时旧快照 superseded
# ============================================================================

def test_new_snapshot_ready_then_old_superseded(db_session):
    """新快照 ready 前旧快照仍 ready；新快照 ready 后同事务将旧快照标记 superseded。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 创建快照 A（ready）
    snap_a = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        trade_date=trade_date,
        generated_at=now - timedelta(hours=1),
    )
    db_session.commit()
    assert snap_a.status == "ready"

    # 创建快照 B（building），同 scope + 同 trade_date + 不同 status
    snap_b = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=trade_date,
    )
    db_session.commit()
    assert snap_b.status == "building"

    # 此时 A 仍为 ready（新快照 ready 前旧快照仍可用）
    db_session.expire_all()
    snap_a_refreshed = db_session.get(DiscoveryScoreSnapshot, snap_a.id)
    assert snap_a_refreshed.status == "ready"

    # B 状态变 ready → 调用 mark_snapshot_superseded(A.id) → A.status='superseded'
    snap_b_refreshed = db_session.get(DiscoveryScoreSnapshot, snap_b.id)
    snap_b_refreshed.status = "ready"
    snap_b_refreshed.generated_at = now
    mark_snapshot_superseded(db_session, snap_a.id)
    db_session.commit()

    db_session.expire_all()
    snap_a_final = db_session.get(DiscoveryScoreSnapshot, snap_a.id)
    snap_b_final = db_session.get(DiscoveryScoreSnapshot, snap_b.id)
    assert snap_a_final.status == "superseded"
    assert snap_b_final.status == "ready"


def test_mark_snapshot_superseded_idempotent(db_session):
    """mark_snapshot_superseded 对已是 superseded 的快照幂等返回。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="superseded",
        trade_date=trade_date,
    )
    db_session.commit()
    # 不抛异常、不改变状态
    mark_snapshot_superseded(db_session, snap.id)
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(DiscoveryScoreSnapshot, snap.id).status == "superseded"


def test_mark_snapshot_superseded_skips_non_ready(db_session):
    """mark_snapshot_superseded 对 building 状态的快照跳过（不误覆盖）。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=trade_date,
    )
    db_session.commit()
    mark_snapshot_superseded(db_session, snap.id)
    db_session.commit()
    db_session.expire_all()
    # 仍是 building，未被改为 superseded
    assert db_session.get(DiscoveryScoreSnapshot, snap.id).status == "building"


# ============================================================================
# 7. 同一 scope 同时只允许一个 building
# ============================================================================

def test_is_snapshot_building_for_scope_returns_building(db_session):
    """创建 building 快照后，is_snapshot_building_for_scope 返回该快照。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        trade_date=trade_date,
    )
    db_session.commit()

    found = is_snapshot_building_for_scope(db_session, "cn_stock")
    assert found is not None
    assert found.id == snap.id


def test_is_snapshot_building_for_scope_returns_none_when_no_building(db_session):
    """没有 building 快照时返回 None。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        trade_date=trade_date,
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()

    found = is_snapshot_building_for_scope(db_session, "cn_stock")
    assert found is None


def test_is_snapshot_building_for_scope_isolates_by_scope(db_session):
    """不同 scope 的 building 快照互不影响。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    _make_snapshot(
        db_session,
        scope="cn_etf",
        status="building",
        trade_date=trade_date,
    )
    db_session.commit()

    # cn_stock 无 building
    assert is_snapshot_building_for_scope(db_session, "cn_stock") is None
    # cn_etf 有 building
    assert is_snapshot_building_for_scope(db_session, "cn_etf") is not None


# ============================================================================
# 8. should_trigger_full_rebuild 各场景
# ============================================================================

def test_should_trigger_full_rebuild_no_snapshot(db_session):
    """current_snapshot=None → 触发（no_snapshot）。"""
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=None
    )
    assert trigger is True
    assert reason == "no_snapshot"


def test_should_trigger_full_rebuild_failed_snapshot(db_session):
    """current_snapshot.status='failed' → 触发（snapshot_not_ready）。"""
    snap = _make_snapshot(db_session, scope="cn_stock", status="failed")
    db_session.commit()
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert trigger is True
    assert reason == "snapshot_not_ready"


def test_should_trigger_full_rebuild_superseded_snapshot(db_session):
    """current_snapshot.status='superseded' → 触发。"""
    snap = _make_snapshot(db_session, scope="cn_stock", status="superseded")
    db_session.commit()
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert trigger is True
    assert reason == "snapshot_not_ready"


def test_should_trigger_full_rebuild_scoring_config_version_changed(db_session):
    """scoring_config_version 变化 → 触发。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        scoring_config_version=1,
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()
    trigger, reason = should_trigger_full_rebuild(
        db_session,
        scope="cn_stock",
        current_snapshot=snap,
        new_scoring_config_version=2,
    )
    assert trigger is True
    assert reason == "scoring_config_version_changed"


def test_should_trigger_full_rebuild_weight_mode_changed(db_session):
    """weight_mode 变化 → 触发。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        weight_mode="manual",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()
    trigger, reason = should_trigger_full_rebuild(
        db_session,
        scope="cn_stock",
        current_snapshot=snap,
        new_weight_mode="ridge",
    )
    assert trigger is True
    assert reason == "weight_mode_changed"


def test_should_trigger_full_rebuild_factor_model_changed(db_session):
    """factor_model_run_id 变化 → 触发。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        factor_model_run_id="run_v1",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()
    trigger, reason = should_trigger_full_rebuild(
        db_session,
        scope="cn_stock",
        current_snapshot=snap,
        new_factor_model_run_id="run_v2",
    )
    assert trigger is True
    assert reason == "factor_model_changed"


def test_should_trigger_full_rebuild_no_trigger_when_only_universe_changed(db_session):
    """仅 universe 有新标的但配置未变 → 不触发全量重建（应走增量 dirty 重算）。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        scoring_config_version=1,
        weight_mode="manual",
        factor_model_run_id=None,
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()
    # 配置完全一致，仅 universe 变化（由 compute_dirty_symbols 处理）
    trigger, reason = should_trigger_full_rebuild(
        db_session,
        scope="cn_stock",
        current_snapshot=snap,
        new_scoring_config_version=1,
        new_weight_mode="manual",
        new_factor_model_run_id=None,
    )
    assert trigger is False
    assert reason is None


def test_should_trigger_full_rebuild_no_trigger_when_config_unchanged(db_session):
    """配置未变且快照 ready → 不触发。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        scoring_config_version=2,
        weight_mode="ridge",
        factor_model_run_id="run_v1",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()
    trigger, reason = should_trigger_full_rebuild(
        db_session,
        scope="cn_stock",
        current_snapshot=snap,
        new_scoring_config_version=2,
        new_weight_mode="ridge",
        new_factor_model_run_id="run_v1",
    )
    assert trigger is False
    assert reason is None


# ============================================================================
# 9. bar_count < 5 边界
# ============================================================================

def test_bar_count_below_threshold_marked_dirty(db_session):
    """universe_symbol 无 daily_bar 数据且 bar_count < 5 → dirty（LATEST_BAR_CHANGED），不抛 NameError。"""
    snapshot_time = datetime(2026, 7, 19, 10, 0, 0)
    before_snapshot = datetime(2026, 7, 18, 9, 0, 0)

    # 创建 universe_symbol，bar_count=0（无 daily_bar 数据）
    _make_universe_symbol(
        db_session,
        symbol="000001",
        bar_count=0,
        last_synced_at=before_snapshot,
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()

    # 不应抛 NameError 或其它异常
    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert len(result) == 1
    assert DirtyReason.LATEST_BAR_CHANGED in result[0].reasons


def test_bar_count_below_four_marked_dirty(db_session):
    """bar_count=3（< 5）→ dirty（LATEST_BAR_CHANGED）。"""
    snapshot_time = datetime(2026, 7, 19, 10, 0, 0)
    before_snapshot = datetime(2026, 7, 18, 9, 0, 0)

    _make_universe_symbol(
        db_session,
        symbol="000002",
        bar_count=3,
        last_synced_at=before_snapshot,
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert len(result) == 1
    assert DirtyReason.LATEST_BAR_CHANGED in result[0].reasons


def test_bar_count_below_threshold_no_snapshot_marked_new_symbol(db_session):
    """无快照 + bar_count=0 → dirty（NEW_SYMBOL_ADDED），不抛异常。"""
    _make_universe_symbol(
        db_session,
        symbol="000003",
        bar_count=0,
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=None
    )
    assert len(result) == 1
    assert DirtyReason.NEW_SYMBOL_ADDED in result[0].reasons


# ============================================================================
# 补充：scope 兼容性
# ============================================================================

def test_compute_dirty_symbols_supports_hyphen_scope(db_session):
    """scope 兼容连字符格式（cn-stock）。"""
    _make_universe_symbol(
        db_session,
        symbol="000001",
        bar_count=10,
        created_at=datetime(2026, 7, 1, 0, 0, 0),
    )
    db_session.commit()

    # cn-stock 与 cn_stock 等价
    result = compute_dirty_symbols(
        db_session, scope="cn-stock", current_snapshot=None
    )
    assert len(result) == 1


def test_compute_dirty_symbols_invalid_scope_raises(db_session):
    """非法 scope 抛 ValueError。"""
    with pytest.raises(ValueError):
        compute_dirty_symbols(
            db_session, scope="invalid_scope", current_snapshot=None
        )


def test_compute_dirty_symbols_sorted_by_last_changed_at_desc(db_session):
    """结果按 last_changed_at 倒序排序。"""
    snapshot_time = datetime(2026, 7, 19, 10, 0, 0)
    before_snapshot = datetime(2026, 7, 18, 9, 0, 0)

    # 创建 3 个 universe_symbols，分别有不同的 last_synced_at
    times = [
        datetime(2026, 7, 19, 11, 0, 0),  # 最晚
        datetime(2026, 7, 19, 12, 0, 0),  # 中间
        datetime(2026, 7, 19, 13, 0, 0),  # 最早（但都晚于快照）
    ]
    for i, ts in enumerate(times):
        _make_universe_symbol(
            db_session,
            symbol=f"00000{i}",
            bar_count=10,
            last_synced_at=ts,
            created_at=datetime(2026, 7, 1, 0, 0, 0),
        )
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()

    result = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap
    )
    assert len(result) == 3
    # 验证倒序
    timestamps = [d.last_changed_at for d in result]
    assert timestamps == sorted(timestamps, reverse=True)
