"""白盒测试 - WP-P.9 增量失效规则端到端集成测试。

覆盖 dirty 集合与快照重建端到端场景（区别于 WP-P.3 的单元测试）：
1. 首次构建全量 dirty（无快照 + universe → NEW_SYMBOL_ADDED → 触发首次快照构建）
2. 数据更新触发 dirty（daily_bar / stock_valuation / capital_flow 等更新 → 对应 DirtyReason）
3. 配置变更触发全量重建（scoring_config_version / weight_mode / factor_model 变化）
4. 同 scope 并发保护（start_data_prep_task 同 scope 重复调用返回同一任务）
5. dirty 集合只包含真实变化标的（混合场景：部分变化部分未变化）

硬约束验证（参照 project_memory）：
- 关联场景测试（dirty 集合 → 快照重建 → get_ready_snapshot 全链路）
- 边界测试（bar_count < 5 不抛 NameError）
- 异步任务并发保护（同一 scope 同时只允许一个 data_prep 任务）
- 终态不被 worker 覆盖
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
from app.models.score import Score
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.models.universe import UniverseSymbol
from app.services import discovery_data_prep, discovery_fast_scan
from app.services.discovery_dirty_set import (
    DirtyReason,
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


def _make_score(
    db_session,
    *,
    symbol_id: int,
    trade_date: date = date(2026, 7, 19),
    priority_score: float = 75.0,
    quality_score: float = 70.0,
    timing_score: float = 65.0,
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=quality_score,
        quality_grade="B",
        timing_score=timing_score,
        priority_score=priority_score,
        stage="accumulate",
        action="buy",
        scoring_config_id=1,
        scoring_config_version=1,
        weight_mode="manual",
        created_at=datetime(2026, 7, 19, 9, 0, 0),
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
    scoring_config_id: int | None = 1,
    scoring_config_version: int | None = 1,
    weight_mode: str | None = "manual",
    factor_model_run_id: str | None = None,
    symbol_count: int = 0,
) -> DiscoveryScoreSnapshot:
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
        symbol_count=symbol_count,
    )
    db_session.add(snap)
    db_session.flush()
    return snap


def _setup_universe_with_scores(
    db_session,
    *,
    symbol_codes: list[str],
    snapshot_time: datetime | None = None,
    bar_count: int = 10,
) -> tuple[list[Symbol], list[UniverseSymbol], DiscoveryScoreSnapshot]:
    """准备 universe + symbols + score + ready 快照（用于增量场景测试）。

    所有数据 created_at 都早于 snapshot_time，确保首次计算 dirty 集合为空。
    """
    if snapshot_time is None:
        snapshot_time = datetime(2026, 7, 19, 10, 0, 0)
    before_snapshot = datetime(2026, 7, 18, 9, 0, 0)

    symbols = []
    universe_symbols = []
    for code in symbol_codes:
        sym = _make_symbol(db_session, symbol=code)
        us = _make_universe_symbol(
            db_session,
            symbol=code,
            bar_count=bar_count,
            last_synced_at=before_snapshot,
        )
        _make_score(
            db_session,
            symbol_id=sym.id,
            trade_date=date(2026, 7, 18),
        )
        symbols.append(sym)
        universe_symbols.append(us)

    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=snapshot_time,
    )
    db_session.commit()
    return symbols, universe_symbols, snap


# ============================================================================
# 1. 首次构建全量 dirty
# ============================================================================

def test_first_build_all_symbols_dirty_new_symbol_added(db_session):
    """无快照 + universe 有 5 个标的 → 全部 dirty (NEW_SYMBOL_ADDED)。

    端到端验证：
    1. 创建 5 个 universe_symbols
    2. compute_dirty_symbols 返回 5 个 dirty，原因 NEW_SYMBOL_ADDED
    3. should_trigger_full_rebuild=True (reason=no_snapshot)
    4. _build_ready_snapshot 生成 ready 快照
    5. compute_dirty_symbols 再次调用返回空（数据未变化）
    """
    codes = ["000001", "000002", "000003", "000004", "000005"]
    symbols = []
    for code in codes:
        sym = _make_symbol(db_session, symbol=code)
        _make_universe_symbol(db_session, symbol=code, bar_count=10)
        _make_score(db_session, symbol_id=sym.id)
        symbols.append(sym)
    db_session.commit()

    # 1. compute_dirty_symbols 返回 5 个 dirty
    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=None)
    assert len(dirty) == 5
    for d in dirty:
        assert DirtyReason.NEW_SYMBOL_ADDED in d.reasons

    # 2. should_trigger_full_rebuild=True
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=None,
    )
    assert trigger is True
    assert reason == "no_snapshot"

    # 3. _build_ready_snapshot 生成 ready 快照
    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=True,
        full_rebuild_reason="no_snapshot",
        dirty_symbols=dirty,
        source_task_id="task-first-build",
    )
    assert snap_id is not None
    db_session.expire_all()
    snap = db_session.get(DiscoveryScoreSnapshot, snap_id)
    assert snap.status == "ready"
    assert snap.dirty_symbol_count == 5

    # 4. 再次计算 dirty 应为空（数据未变化）
    dirty_again = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap,
    )
    assert dirty_again == []


def test_first_build_with_no_universe_returns_empty_dirty(db_session):
    """空库（无 universe）+ 无快照 → dirty 集合为空，但仍触发全量重建。"""
    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=None)
    assert dirty == []

    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=None,
    )
    assert trigger is True
    assert reason == "no_snapshot"


# ============================================================================
# 2. 数据更新触发 dirty
# ============================================================================

def test_daily_bar_update_triggers_latest_bar_changed(db_session):
    """daily_bar 在快照后新增 → dirty (LATEST_BAR_CHANGED)。"""
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000001", "000002"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 给 symbols[0] 新增 daily_bar
    db_session.add(DailyBar(
        symbol_id=symbols[0].id,
        trade_date=date(2026, 7, 19),
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000000.0,
        created_at=after_snapshot,
    ))
    db_session.commit()

    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    assert DirtyReason.LATEST_BAR_CHANGED in dirty[0].reasons
    assert dirty[0].symbol == "000001"


def test_financial_report_update_triggers_financial_report_updated(db_session):
    """stock_valuation 在快照后新增 → dirty (FINANCIAL_REPORT_UPDATED)。"""
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000010"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    db_session.add(StockValuation(
        symbol_id=symbols[0].id,
        trade_date=date(2026, 7, 19),
        pe_ttm=15.0,
        created_at=after_snapshot,
    ))
    db_session.commit()

    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    assert DirtyReason.FINANCIAL_REPORT_UPDATED in dirty[0].reasons


def test_multiple_data_updates_aggregate_reasons(db_session):
    """同一标的多类数据更新 → dirty 包含多个 DirtyReason。"""
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000020"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 同时更新 4 类数据
    db_session.add(DailyBar(
        symbol_id=symbols[0].id,
        trade_date=date(2026, 7, 19),
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000000.0,
        created_at=after_snapshot,
    ))
    db_session.add(CapitalFlow(
        symbol_id=symbols[0].id,
        trade_date=date(2026, 7, 19),
        main_net_inflow=1000000.0,
        created_at=after_snapshot,
    ))
    db_session.add(StockHotRankSnapshot(
        symbol="000020",
        trade_date=date(2026, 7, 19),
        hot_rank=10, hot_rank_total=5000, hot_rank_pct=99.8,
        created_at=after_snapshot,
    ))
    db_session.add(TailAccumulationSnapshot(
        symbol="000020",
        trade_date=date(2026, 7, 19),
        minute_count=240, tail_minute_count=30,
        day_amount=100000000.0, tail_amount=10000000.0,
        tail_amount_share=0.1, tail_activity_ratio=2.0,
        tail_return=0.005, close_location=0.8, proxy_score=70.0,
        created_at=after_snapshot,
    ))
    db_session.commit()

    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    reasons = dirty[0].reasons
    assert DirtyReason.LATEST_BAR_CHANGED in reasons
    assert DirtyReason.CAPITAL_FLOW_UPDATED in reasons
    assert DirtyReason.HOT_RANK_UPDATED in reasons
    assert DirtyReason.TAIL_ACCUMULATION_UPDATED in reasons


def test_universe_resync_triggers_universe_resynced(db_session):
    """universe_symbols.last_synced_at 晚于快照 → UNIVERSE_RESYNCED。"""
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000030"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 更新 last_synced_at
    symbols[0].last_synced_at = None  # universe_symbols 不在 symbols 表
    # 找到 universe_symbol
    us = db_session.query(UniverseSymbol).filter_by(symbol="000030").one()
    us.last_synced_at = after_snapshot
    db_session.commit()

    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    assert DirtyReason.UNIVERSE_RESYNCED in dirty[0].reasons


def test_snapshot_expired_triggers_all_dirty(db_session):
    """快照过期 → 所有标的 dirty (SNAPSHOT_EXPIRED)。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expired_time = now - timedelta(days=10)

    codes = ["000040", "000041", "000042"]
    for code in codes:
        sym = _make_symbol(db_session, symbol=code)
        _make_universe_symbol(
            db_session,
            symbol=code,
            bar_count=10,
            last_synced_at=expired_time - timedelta(hours=1),
        )
        _make_score(db_session, symbol_id=sym.id, trade_date=date(2026, 7, 9))
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        trade_date=expired_time,
        generated_at=expired_time,
    )
    db_session.commit()

    dirty = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap,
        snapshot_max_age_days=7,
    )
    assert len(dirty) == 3
    for d in dirty:
        assert DirtyReason.SNAPSHOT_EXPIRED in d.reasons


# ============================================================================
# 3. 配置变更触发全量重建
# ============================================================================

def test_scoring_config_version_changed_triggers_full_rebuild(db_session):
    """scoring_config_version 变化 → should_trigger_full_rebuild=True。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        scoring_config_version=1,
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()

    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap,
        new_scoring_config_version=2,
    )
    assert trigger is True
    assert reason == "scoring_config_version_changed"


def test_weight_mode_changed_triggers_full_rebuild(db_session):
    """weight_mode 变化 → should_trigger_full_rebuild=True。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        weight_mode="manual",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()

    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap,
        new_weight_mode="ridge",
    )
    assert trigger is True
    assert reason == "weight_mode_changed"


def test_factor_model_changed_triggers_full_rebuild(db_session):
    """factor_model_run_id 变化 → should_trigger_full_rebuild=True。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        factor_model_run_id="run_v1",
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db_session.commit()

    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap,
        new_factor_model_run_id="run_v2",
    )
    assert trigger is True
    assert reason == "factor_model_changed"


def test_config_unchanged_does_not_trigger_full_rebuild(db_session):
    """配置未变 + 快照 ready → 不触发全量重建（走增量 dirty 重算）。"""
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
        db_session, scope="cn_stock", current_snapshot=snap,
        new_scoring_config_version=2,
        new_weight_mode="ridge",
        new_factor_model_run_id="run_v1",
    )
    assert trigger is False
    assert reason is None


def test_full_rebuild_then_build_new_snapshot(db_session):
    """端到端：配置变更 → 全量重建 → 旧快照 superseded。"""
    symbols, _, snap_a = _setup_universe_with_scores(
        db_session, symbol_codes=["000050"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    snap_a_id = snap_a.id

    # 模拟配置变更（scoring_config_version 1 → 2）
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap_a,
        new_scoring_config_version=2,
    )
    assert trigger is True

    # 全量重建：用新 trade_date 创建新快照
    snap_b_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 20),
        trigger_full_rebuild=True,
        full_rebuild_reason=reason,
        dirty_symbols=[],
        source_task_id="task-config-change",
    )
    db_session.expire_all()

    # 旧快照应被 superseded
    snap_a_final = db_session.get(DiscoveryScoreSnapshot, snap_a_id)
    assert snap_a_final.status == "superseded"

    # 新快照应为 ready
    snap_b = db_session.get(DiscoveryScoreSnapshot, snap_b_id)
    assert snap_b.status == "ready"

    # get_ready_snapshot 返回新快照
    ready = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert ready.id == snap_b_id


# ============================================================================
# 4. 同 scope 并发保护
# ============================================================================

def test_start_data_prep_task_concurrency_protection(db_session, monkeypatch):
    """同 scope 已有 queued/running 任务时，再次调用返回同一任务。

    端到端验证 project_memory 硬约束：异步任务并发保护。
    """
    # 拦截 worker 启动，避免真实执行
    started_workers: list[tuple] = []
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: started_workers.append((task_id, worker_func)),
    )

    first = discovery_data_prep.start_data_prep_task(scope="cn-stock")
    second = discovery_data_prep.start_data_prep_task(scope="cn_stock")  # 兼容格式

    # 应返回同一任务
    assert first.id == second.id
    # 只启动一次 worker
    assert len(started_workers) == 1


def test_start_data_prep_task_different_scopes_independent(db_session, monkeypatch):
    """不同 scope 的 data_prep 任务互不影响。"""
    started_workers: list[tuple] = []
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: started_workers.append((task_id, worker_func)),
    )

    stock_task = discovery_data_prep.start_data_prep_task(scope="cn-stock")
    etf_task = discovery_data_prep.start_data_prep_task(scope="cn-etf")

    # 不同任务
    assert stock_task.id != etf_task.id
    # 两个 worker 都启动
    assert len(started_workers) == 2


def test_is_snapshot_building_for_scope_concurrent_protection(db_session):
    """is_snapshot_building_for_scope 用于防止并发构建快照。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)

    # 初始无 building
    assert is_snapshot_building_for_scope(db_session, "cn_stock") is None

    # 创建 building 快照
    _make_snapshot(db_session, scope="cn_stock", status="building", trade_date=trade_date)
    db_session.commit()

    # 现在应返回该 building 快照
    found = is_snapshot_building_for_scope(db_session, "cn_stock")
    assert found is not None
    assert found.status == "building"

    # 不同 scope 仍无 building
    assert is_snapshot_building_for_scope(db_session, "cn_etf") is None


def test_is_snapshot_building_for_scope_after_ready(db_session):
    """building → ready 后，is_snapshot_building_for_scope 返回 None。"""
    trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="building", trade_date=trade_date,
    )
    db_session.commit()
    assert is_snapshot_building_for_scope(db_session, "cn_stock") is not None

    # 切换到 ready
    snap.status = "ready"
    snap.generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.commit()

    assert is_snapshot_building_for_scope(db_session, "cn_stock") is None


# ============================================================================
# 5. dirty 集合只包含真实变化标的
# ============================================================================

def test_dirty_set_only_contains_changed_symbols(db_session):
    """混合场景：3 个标的中只有 1 个数据变化 → dirty 只包含 1 个。"""
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000060", "000061", "000062"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 只给 symbols[1] 新增 daily_bar
    db_session.add(DailyBar(
        symbol_id=symbols[1].id,
        trade_date=date(2026, 7, 19),
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000000.0,
        created_at=after_snapshot,
    ))
    db_session.commit()

    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    assert dirty[0].symbol == "000061"
    assert DirtyReason.LATEST_BAR_CHANGED in dirty[0].reasons


def test_dirty_set_empty_when_no_data_changed(db_session):
    """所有数据 updated_at 都早于快照 → dirty 集合为空。"""
    _setup_universe_with_scores(
        db_session, symbol_codes=["000070", "000071"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )

    # 无任何数据更新
    snap = db_session.query(DiscoveryScoreSnapshot).filter_by(scope="cn_stock").one()
    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert dirty == []


def test_dirty_set_includes_new_symbol_added(db_session):
    """快照后 universe 新增标的 → dirty 包含 NEW_SYMBOL_ADDED。"""
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000080", "000081"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 新增一个标的（created_at 晚于快照）
    new_sym = _make_symbol(db_session, symbol="000082")
    _make_universe_symbol(
        db_session,
        symbol="000082",
        bar_count=10,
        last_synced_at=after_snapshot,
        created_at=after_snapshot,
    )
    _make_score(db_session, symbol_id=new_sym.id, trade_date=date(2026, 7, 19))
    db_session.commit()

    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    assert dirty[0].symbol == "000082"
    assert DirtyReason.NEW_SYMBOL_ADDED in dirty[0].reasons


def test_dirty_set_bar_count_below_threshold(db_session):
    """bar_count < 5 → dirty (LATEST_BAR_CHANGED)，不抛 NameError。

    参照 project_memory 硬约束：data credibility 计算必须处理 bar_count < 5 边界。
    """
    symbols, _, snap = _setup_universe_with_scores(
        db_session, symbol_codes=["000090"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
        bar_count=3,  # < 5
    )

    # 不应抛 NameError 或其它异常
    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap)
    assert len(dirty) == 1
    assert DirtyReason.LATEST_BAR_CHANGED in dirty[0].reasons


def test_dirty_set_then_incremental_rebuild(db_session):
    """端到端：dirty 集合 → 增量重建 → 新快照 ready → dirty 清空。"""
    symbols, _, snap_a = _setup_universe_with_scores(
        db_session, symbol_codes=["000100", "000101"],
        snapshot_time=datetime(2026, 7, 19, 10, 0, 0),
    )
    snap_a_id = snap_a.id
    after_snapshot = datetime(2026, 7, 19, 11, 0, 0)

    # 给 symbols[0] 新增 daily_bar
    db_session.add(DailyBar(
        symbol_id=symbols[0].id,
        trade_date=date(2026, 7, 19),
        open=10.0, high=11.0, low=9.5, close=10.5,
        volume=1000000.0,
        created_at=after_snapshot,
    ))
    db_session.commit()

    # 1. 计算 dirty 集合
    dirty = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=snap_a)
    assert len(dirty) == 1

    # 2. should_trigger_full_rebuild=False（配置未变，走增量）
    trigger, reason = should_trigger_full_rebuild(
        db_session, scope="cn_stock", current_snapshot=snap_a,
        new_scoring_config_version=1,  # 与 snap_a 相同
        new_weight_mode="manual",
        new_factor_model_run_id=None,
    )
    assert trigger is False
    assert reason is None

    # 3. 增量重建（用新 trade_date）
    snap_b_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 20),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=dirty,
        source_task_id="task-incremental",
    )
    db_session.expire_all()

    # 4. 旧快照 superseded
    snap_a_final = db_session.get(DiscoveryScoreSnapshot, snap_a_id)
    assert snap_a_final.status == "superseded"

    # 5. 新快照 ready，dirty_symbol_count=1
    snap_b = db_session.get(DiscoveryScoreSnapshot, snap_b_id)
    assert snap_b.status == "ready"
    assert snap_b.dirty_symbol_count == 1

    # 6. 再次计算 dirty 应为空（新快照后无新变化）
    dirty_after = compute_dirty_symbols(
        db_session, scope="cn_stock", current_snapshot=snap_b,
    )
    assert dirty_after == []


# ============================================================================
# 补充：scope 兼容性与非法输入
# ============================================================================

def test_dirty_set_supports_hyphen_scope(db_session):
    """cn-stock 与 cn_stock 等价。"""
    _make_universe_symbol(db_session, symbol="000110", bar_count=10)
    db_session.commit()

    # 两种格式应返回相同结果
    r1 = compute_dirty_symbols(db_session, scope="cn-stock", current_snapshot=None)
    r2 = compute_dirty_symbols(db_session, scope="cn_stock", current_snapshot=None)
    assert len(r1) == len(r2) == 1


def test_dirty_set_invalid_scope_raises(db_session):
    """非法 scope 抛 ValueError。"""
    with pytest.raises(ValueError):
        compute_dirty_symbols(db_session, scope="invalid-scope", current_snapshot=None)


def test_start_data_prep_task_invalid_scope_raises(db_session, monkeypatch):
    """start_data_prep_task 非法 scope 抛 ValueError。"""
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: None,
    )
    with pytest.raises(ValueError):
        discovery_data_prep.start_data_prep_task(scope="invalid-scope")
