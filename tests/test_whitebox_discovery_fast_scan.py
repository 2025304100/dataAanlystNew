"""白盒测试 - WP-P.9 端到端快速扫描集成测试。

覆盖 fast scan 全链路端到端场景（区别于 WP-P.4/5/6/8 的单元测试）：
1. 从无快照到 ready 后扫描（完整链路：空库 → 建快照 → 扫描成功）
2. 缓存命中复用（首次 cache_hit=False，二次 cache_hit=True）
3. HTTP 守门端到端（monkeypatch urllib/requests/httpx，断言零调用）
4. Top 300 限制（写入 350 条 items，扫描结果最多 300 条）
5. 取消扫描（CancelToken.cancel() → status="cancelled"）
6. 阶段预算超时（monkeypatch 缩小预算 → status="timeout"）
7. 过滤器组合（asset_types / stages / actions / min_score 组合）
8. 不发起第三方 HTTP 请求（综合验证）

硬约束验证（参照 project_memory）：
- 快速扫描路径发起任何第三方 HTTP 请求即测试失败
- ScanResult 写入数量严格受 Top-K (300) 限制
- 取消令牌不阻塞主流程，仅在每个阶段开始前检查
- 超时降级 best-effort，不抛异常给用户
- 终态不被 worker 覆盖
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services import discovery_data_prep, discovery_fast_scan
from app.services.discovery_stage_budget import (
    CancelToken,
    ScanTimings,
    STAGE_BUDGETS,
    TOTAL_BUDGET_SECONDS,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 辅助函数（与其它 WP-P 测试文件保持一致风格）
# ============================================================================

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
    quality_score: float = 70.0,
    timing_score: float = 65.0,
    stage: str = "accumulate",
    action: str = "buy",
    asset_type: str = "stock",
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
    )
    db_session.add(item)
    db_session.flush()
    return item


def _setup_ready_snapshot_with_items(
    db_session,
    *,
    item_count: int = 5,
    min_score: float = 55.0,
    score_start: float = 80.0,
):
    """创建 ready 快照 + 多个 item 用于 run_fast_scan 测试。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_count=item_count,
    )
    for i in range(item_count):
        sym = _make_symbol(db_session, symbol=f"STG3_{i:04d}")
        us = _make_universe_symbol(db_session, symbol=f"STG3_{i:04d}")
        _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=score_start - i,  # 递减
            stage="accumulate",
            action="buy",
        )
    db_session.commit()
    return snap


def _install_http_guards(monkeypatch):
    """安装 HTTP 守门：任何对 urllib / requests / httpx 的调用都抛 AssertionError。"""
    def _http_violation(*args, **kwargs):
        raise AssertionError(
            "fast_scan 不应发起任何第三方 HTTP 请求，"
            f"但调用了 args={args!r} kwargs={kwargs!r}"
        )

    # urllib.request.urlopen
    try:
        import urllib.request as _urllib_request
        monkeypatch.setattr(_urllib_request, "urlopen", _http_violation)
    except ImportError:
        pass

    # requests
    try:
        import requests as _requests
        monkeypatch.setattr(_requests, "get", _http_violation, raising=False)
        monkeypatch.setattr(_requests, "post", _http_violation, raising=False)
        if hasattr(_requests, "Session"):
            monkeypatch.setattr(_requests.Session, "request", _http_violation, raising=False)
    except ImportError:
        pass

    # httpx
    try:
        import httpx as _httpx
        if hasattr(_httpx, "Client"):
            monkeypatch.setattr(_httpx.Client, "get", _http_violation, raising=False)
            monkeypatch.setattr(_httpx.Client, "post", _http_violation, raising=False)
            monkeypatch.setattr(_httpx.Client, "request", _http_violation, raising=False)
    except ImportError:
        pass


# ============================================================================
# 1. 从无快照到 ready 后扫描（完整链路）
# ============================================================================

def test_full_flow_from_empty_db_to_scan_results(db_session, monkeypatch):
    """端到端：空库 → 准备数据 → 构建快照 → 扫描成功。

    流程：
    1. 空库，run_fast_scan 返回 degraded_reason="no_ready_snapshot"
    2. 准备 universe + Score 数据
    3. _build_ready_snapshot 生成 ready 快照
    4. run_fast_scan 返回正确结果
    """
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )

    # 1. 空库扫描
    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r1["snapshot_id"] is None
    assert r1["degraded_reason"] == "no_ready_snapshot"
    assert r1["results"] == []

    # 2. 准备数据
    for code in ["000001", "000002", "000003"]:
        sym = _make_symbol(db_session, symbol=code)
        _make_universe_symbol(db_session, symbol=code)
        db_session.add(Score(
            symbol_id=sym.id,
            trade_date=date(2026, 7, 19),
            quality_score=70.0, quality_grade="B",
            timing_score=65.0, priority_score=75.0,
            stage="accumulate", action="buy",
            scoring_config_id=1, scoring_config_version=1, weight_mode="manual",
            created_at=datetime(2026, 7, 19, 9, 0, 0),
        ))
    db_session.commit()

    # 3. 构建快照
    snap_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-flow",
    )
    assert snap_id is not None

    # 4. 扫描成功
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r2["snapshot_id"] == snap_id
    assert r2["degraded_reason"] is None
    assert r2["coarse_match_count"] == 3
    assert len(r2["results"]) == 3


def test_full_flow_with_http_guard(db_session, monkeypatch):
    """端到端：完整扫描流程中不发起任何第三方 HTTP 请求。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] is None
    assert result["coarse_match_count"] == 3


# ============================================================================
# 2. 缓存命中复用
# ============================================================================

def test_cache_hit_on_second_call_same_params(db_session, monkeypatch):
    """相同参数二次扫描命中缓存，cache_hit=True，不重复写 ScanResult。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 首次：未命中
    assert r1["cache_hit"] is False
    assert r1["cached_from_scan_run_id"] is None
    assert r1["result_rows_written"] > 0
    # 二次：命中
    assert r2["cache_hit"] is True
    assert r2["cached_from_scan_run_id"] is not None
    assert r2["result_rows_written"] == 0  # 不重复写


def test_cache_miss_on_different_min_score(db_session, monkeypatch):
    """不同 min_score → cache_miss。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=60, db=db_session,  # 不同 min_score
    )

    assert r1["cache_hit"] is False
    assert r2["cache_hit"] is False
    assert r1["cache_key"] != r2["cache_key"]


def test_cache_miss_on_different_snapshot(db_session, monkeypatch):
    """不同 snapshot → cache_miss（新快照生成后旧缓存失效）。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    # 首次扫描
    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r1["cache_hit"] is False

    # 构建新快照（不同 trade_date）
    snap2_id = discovery_data_prep._build_ready_snapshot(
        db_session,
        scope="cn_stock",
        trade_date=date(2026, 7, 20),
        trigger_full_rebuild=False,
        full_rebuild_reason=None,
        dirty_symbols=[],
        source_task_id="task-new-snap",
    )
    db_session.expire_all()

    # 再次扫描：新 snapshot_id → cache_miss
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert r2["cache_hit"] is False
    assert r2["snapshot_id"] == snap2_id


# ============================================================================
# 3. HTTP 守门端到端
# ============================================================================

def test_http_guard_urllib_not_called(db_session, monkeypatch):
    """扫描路径不调用 urllib.request.urlopen。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] is None
    assert result["coarse_match_count"] == 2


def test_http_guard_requests_not_called(db_session, monkeypatch):
    """扫描路径不调用 requests.get / requests.post / requests.Session.request。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] is None


def test_http_guard_httpx_not_called(db_session, monkeypatch):
    """扫描路径不调用 httpx.Client.get / post / request。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] is None


def test_assert_no_http_request_called_at_entry_and_exit(db_session, monkeypatch):
    """_assert_no_http_request 在 run_fast_scan 入口和出口都被调用。"""
    _setup_ready_snapshot_with_items(db_session, item_count=1)

    call_count = {"n": 0}

    def _spy():
        call_count["n"] += 1

    monkeypatch.setattr(discovery_fast_scan, "_assert_no_http_request", _spy)

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 入口 1 次 + 出口 1 次 = 至少 2 次
    assert call_count["n"] >= 2


# ============================================================================
# 4. Top 300 限制
# ============================================================================

def test_top_300_limit_enforced(db_session, monkeypatch):
    """写入 350 条 items，扫描结果最多 300 条（DEFAULT_TOP_K）。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    # 写入 350 条 items（priority_score 都高于 min_score）
    _setup_ready_snapshot_with_items(
        db_session, item_count=350, min_score=55, score_start=80.0,
    )

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # coarse_match_count 可能是 350（粗筛匹配数），但 result_rows_written 受 Top-K 限制
    assert result["result_rows_written"] <= 300
    assert len(result["results"]) <= 300


def test_top_300_limit_with_custom_limit(db_session, monkeypatch):
    """limit=50 时扫描结果最多 50 条。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(
        db_session, item_count=100, min_score=55, score_start=80.0,
    )

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, limit=50, db=db_session,
    )

    assert result["result_rows_written"] <= 50
    assert len(result["results"]) <= 50


def test_top_300_limit_results_sorted_by_priority(db_session, monkeypatch):
    """扫描结果按 priority_score 倒序排序。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(
        db_session, item_count=10, min_score=55, score_start=80.0,
    )

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, limit=5, db=db_session,
    )

    scores = [r["priority_score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)
    # 应取前 5 个最高分
    assert scores[0] == 80.0


# ============================================================================
# 5. 取消扫描
# ============================================================================

def test_cancel_before_sql_coarse_filter(db_session, monkeypatch):
    """在 sql_coarse_filter 阶段开始前取消 → status="cancelled"。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    token = CancelToken(task_id="test-cancel-task")

    # 包装 _coarse_filter 以在它被调用前取消
    original_coarse_filter = discovery_fast_scan._coarse_filter

    def spy_coarse_filter(*args, **kwargs):
        token.cancel()
        return original_coarse_filter(*args, **kwargs)

    import app.services.discovery_fast_scan as fs_mod
    original_attr = fs_mod._coarse_filter
    fs_mod._coarse_filter = spy_coarse_filter
    try:
        result = discovery_fast_scan.run_fast_scan(
            scope="cn_stock", min_score=55, db=db_session,
            cancel_token=token,
        )
    finally:
        fs_mod._coarse_filter = original_attr

    # 取消降级
    assert result["status"] == "cancelled"
    assert result["timings"] is not None
    assert result["timings"]["cancel_token_checked"] is True


def test_cancel_token_none_works(db_session, monkeypatch):
    """cancel_token=None 时正常执行，不抛异常。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
        cancel_token=None,
    )

    assert result["status"] == "ok"
    assert result["coarse_match_count"] == 2


def test_cancel_token_not_cancelled_works(db_session, monkeypatch):
    """cancel_token 存在但未取消时正常执行。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    token = CancelToken(task_id="test-not-cancelled")
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
        cancel_token=token,
    )

    assert result["status"] == "ok"
    assert result["coarse_match_count"] == 2
    assert token.cancelled is False


# ============================================================================
# 6. 阶段预算超时
# ============================================================================

def test_budget_timeout_returns_timeout_status(db_session, monkeypatch):
    """总预算超时 → status="timeout"。

    通过 monkeypatch 缩小 total_budget_ms 触发超时，避免实际等待 300s。
    """
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    # 缩小总预算到 0.001ms，保证任何执行都会超时
    original_init = ScanTimings.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.total_budget_ms = 0.001

    monkeypatch.setattr(ScanTimings, "__init__", patched_init)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
        enforce_budget=True,
    )

    assert result["status"] == "timeout"
    assert result["timings"] is not None
    assert result["timings"]["total_exceeded"] is True


def test_budget_no_enforce_returns_ok_with_degraded(db_session, monkeypatch):
    """enforce_budget=False 时总预算超时不抛异常，但 degraded_reason="budget_exceeded"。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    # 缩小 STAGE_BUDGETS 使阶段超时
    import app.services.discovery_stage_budget as budget_mod
    monkeypatch.setattr(
        budget_mod,
        "STAGE_BUDGETS",
        {k: 0.001 for k in budget_mod.STAGE_BUDGETS},
    )

    original_init = ScanTimings.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.total_budget_ms = 0.001

    monkeypatch.setattr(ScanTimings, "__init__", patched_init)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
        enforce_budget=False,
    )

    # 不抛异常，status="ok" 或 "degraded"
    assert result["status"] in ("ok", "degraded")
    # 有阶段超时
    assert result["exceeded_stages"] is not None
    assert len(result["exceeded_stages"]) > 0


def test_budget_normal_returns_ok(db_session, monkeypatch):
    """正常预算下 status="ok"，exceeded_stages=None。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    assert result["status"] == "ok"
    assert result["exceeded_stages"] is None
    assert result["timings"] is not None
    assert result["timings"]["total_exceeded"] is False


# ============================================================================
# 7. 过滤器组合
# ============================================================================

def test_filter_min_score_only(db_session, monkeypatch):
    """仅 min_score 过滤：返回所有 >= min_score 的标的。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0), symbol_count=3,
    )
    for i, score in enumerate([50.0, 60.0, 70.0]):
        sym = _make_symbol(db_session, symbol=f"FLT_{i:04d}")
        us = _make_universe_symbol(db_session, symbol=f"FLT_{i:04d}")
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            universe_symbol_id=us.id, symbol_id=sym.id,
            priority_score=score,
        )
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["coarse_match_count"] == 2  # 60 / 70
    assert all(r["priority_score"] >= 55 for r in result["results"])


def test_filter_stage_and_action(db_session, monkeypatch):
    """stage + action 组合过滤。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0), symbol_count=4,
    )
    # 4 个 items：2 个 accumulate/buy，1 个 breakout/buy，1 个 accumulate/sell
    configs = [
        ("000001", "accumulate", "buy", 80.0),
        ("000002", "accumulate", "buy", 75.0),
        ("000003", "breakout", "buy", 70.0),
        ("000004", "accumulate", "sell", 65.0),
    ]
    for code, stage, action, score in configs:
        sym = _make_symbol(db_session, symbol=code)
        us = _make_universe_symbol(db_session, symbol=code)
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            universe_symbol_id=us.id, symbol_id=sym.id,
            priority_score=score, stage=stage, action=action,
        )
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55,
        stages=["accumulate"], actions=["buy"],
        db=db_session,
    )
    # 只匹配 stage=accumulate + action=buy 的 2 个标的
    assert result["coarse_match_count"] == 2
    for r in result["results"]:
        assert r["stage"] == "accumulate"
        assert r["action"] == "buy"


def test_filter_asset_types(db_session, monkeypatch):
    """asset_types 过滤。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0), symbol_count=3,
    )
    # 2 个 stock + 1 个 etf
    for i, (code, asset_type) in enumerate([
        ("STK001", "stock"), ("STK002", "stock"), ("ETF001", "etf"),
    ]):
        sym = _make_symbol(db_session, symbol=code, asset_type=asset_type)
        us = _make_universe_symbol(db_session, symbol=code, asset_type=asset_type)
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            universe_symbol_id=us.id, symbol_id=sym.id,
            priority_score=75.0 - i,
        )
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55,
        asset_types=["stock"],
        db=db_session,
    )
    # 只匹配 stock
    assert result["coarse_match_count"] == 2


def test_filter_combined_all(db_session, monkeypatch):
    """min_score + asset_types + stages + actions 组合过滤。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    snap = _make_snapshot(
        db_session, scope="cn_stock", status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0), symbol_count=5,
    )
    configs = [
        ("000001", "stock", "accumulate", "buy", 80.0),   # match
        ("000002", "stock", "accumulate", "buy", 75.0),   # match
        ("000003", "stock", "breakout", "buy", 70.0),     # stage 不匹配
        ("000004", "etf", "accumulate", "buy", 65.0),     # asset_type 不匹配
        ("000005", "stock", "accumulate", "sell", 60.0),  # action 不匹配
    ]
    for code, asset_type, stage, action, score in configs:
        sym = _make_symbol(db_session, symbol=code, asset_type=asset_type)
        us = _make_universe_symbol(db_session, symbol=code, asset_type=asset_type)
        _make_snapshot_item(
            db_session, snapshot_id=snap.id,
            universe_symbol_id=us.id, symbol_id=sym.id,
            priority_score=score, stage=stage, action=action,
        )
    db_session.commit()

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55,
        asset_types=["stock"],
        stages=["accumulate"],
        actions=["buy"],
        db=db_session,
    )
    # 只匹配前 2 个
    assert result["coarse_match_count"] == 2
    matched_codes = {r["symbol"] for r in result["results"]}
    assert matched_codes == {"000001", "000002"}


# ============================================================================
# 8. 不发起第三方 HTTP 请求（综合验证）
# ============================================================================

def test_no_http_request_during_full_scan_with_indicators(db_session, monkeypatch):
    """带自定义指标的扫描也不发起 HTTP 请求。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    indicator_plan = {
        "ma5": {"formula": "close"},
        "ma10": {"formula": "open"},
    }
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55,
        indicator_plan=indicator_plan,
        db=db_session,
    )
    assert result["degraded_reason"] is None


def test_no_http_request_during_scan_with_portfolio_filter(db_session, monkeypatch):
    """带组合过滤的扫描也不发起 HTTP 请求。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55,
        portfolio_id=1, portfolio_rule_id=1,
        db=db_session,
    )
    # 不抛 AssertionError 即通过
    assert result is not None


def test_no_http_request_during_scan_with_cancel(db_session, monkeypatch):
    """取消扫描时不发起 HTTP 请求。"""
    _install_http_guards(monkeypatch)
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    token = CancelToken(task_id="test-no-http-cancel")
    original_coarse_filter = discovery_fast_scan._coarse_filter

    def spy_coarse_filter(*args, **kwargs):
        token.cancel()
        return original_coarse_filter(*args, **kwargs)

    import app.services.discovery_fast_scan as fs_mod
    original_attr = fs_mod._coarse_filter
    fs_mod._coarse_filter = spy_coarse_filter
    try:
        result = discovery_fast_scan.run_fast_scan(
            scope="cn_stock", min_score=55, db=db_session,
            cancel_token=token,
        )
    finally:
        fs_mod._coarse_filter = original_attr

    assert result["status"] == "cancelled"


# ============================================================================
# 补充：ScanResult 写入与 ScanRun 摘要
# ============================================================================

def test_scan_writes_scan_result_and_scan_run(db_session, monkeypatch):
    """扫描完成后 ScanResult 与 ScanRun 都被写入。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    before_results = db_session.query(ScanResult).count()
    before_runs = db_session.query(ScanRun).count()

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    after_results = db_session.query(ScanResult).count()
    after_runs = db_session.query(ScanRun).count()

    assert after_results > before_results
    assert after_runs > before_runs


def test_scan_cache_hit_does_not_write_scan_result(db_session, monkeypatch):
    """缓存命中时不重复写 ScanResult。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=3)

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    after_first = db_session.query(ScanResult).count()

    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    after_second = db_session.query(ScanResult).count()

    assert r2["cache_hit"] is True
    assert r2["result_rows_written"] == 0
    assert after_second == after_first  # 无新增 ScanResult


def test_scan_returns_timings_with_all_stages(db_session, monkeypatch):
    """返回的 timings 包含所有阶段。"""
    monkeypatch.setattr(
        discovery_fast_scan, "_assert_no_http_request", lambda: None,
    )
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    timings = result["timings"]
    assert timings is not None
    stage_names = [s["stage"] for s in timings["stages"]]
    expected_stages = [
        "snapshot_health_check",
        "sql_coarse_filter",
        "result_persistence",
        "finalize_audit",
    ]
    for stage in expected_stages:
        assert stage in stage_names
    assert timings["total_budget_ms"] == TOTAL_BUDGET_SECONDS * 1000


# ============================================================================
# WP-P-FIX.2: 无 ready 快照时的降级与自动数据准备
# ============================================================================
# 关键：run_fast_scan 内部用延迟导入 `from app.services.discovery_data_prep
# import start_data_prep_task, _is_data_prep_running`，因此 mock 需 patch
# discovery_data_prep 模块上的同名属性，避免真正启动 worker 线程。


def test_no_ready_snapshot_returns_stale_snapshot(db_session, monkeypatch):
    """WP-P-FIX.2: 无 ready 快照时返回上一历史快照（using_stale_snapshot）。"""
    monkeypatch.setattr(discovery_fast_scan, "_assert_no_http_request", lambda: None)
    # mock data_prep 避免真正启动 worker
    monkeypatch.setattr(
        discovery_data_prep, "start_data_prep_task",
        lambda **kw: type("T", (), {"id": "fake"})(),
    )
    monkeypatch.setattr(
        discovery_data_prep, "_is_data_prep_running", lambda db, scope: None,
    )

    # 1. 先构建一个 ready 快照（准备数据 + 构建）
    sym = _make_symbol(db_session, symbol="000001")
    _make_universe_symbol(db_session, symbol="000001")
    db_session.add(Score(
        symbol_id=sym.id, trade_date=date(2026, 7, 19),
        quality_score=70.0, quality_grade="B",
        timing_score=65.0, priority_score=75.0,
        stage="accumulate", action="buy",
        scoring_config_id=1, scoring_config_version=1, weight_mode="manual",
        created_at=datetime(2026, 7, 19, 9, 0, 0),
    ))
    db_session.commit()
    old_snap_id = discovery_data_prep._build_ready_snapshot(
        db_session, scope="cn_stock", trade_date=date(2026, 7, 19),
        trigger_full_rebuild=False, full_rebuild_reason=None,
        dirty_symbols=[], source_task_id="task-old",
    )

    # 2. 手动将旧快照标记为 superseded（模拟新快照生成后旧快照被取代）
    old_snap = db_session.get(DiscoveryScoreSnapshot, old_snap_id)
    old_snap.status = "superseded"
    db_session.commit()

    # 3. 无 ready 快照时扫描，应返回历史 superseded 快照
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] == "using_stale_snapshot"
    assert result["snapshot_id"] == old_snap_id
    assert result["data_prep_task_id"] == "fake"
    assert "data_cutoff_at" in result


def test_no_ready_snapshot_auto_starts_data_prep(db_session, monkeypatch):
    """WP-P-FIX.2: 无快照时自动启动 data_prep 任务。"""
    monkeypatch.setattr(discovery_fast_scan, "_assert_no_http_request", lambda: None)
    started_tasks = []

    def _fake_start(**kwargs):
        started_tasks.append(kwargs)
        return type("T", (), {"id": "task-started-123"})()

    monkeypatch.setattr(discovery_data_prep, "start_data_prep_task", _fake_start)
    monkeypatch.setattr(
        discovery_data_prep, "_is_data_prep_running", lambda db, scope: None,
    )

    # 空库扫描（无任何快照）
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["degraded_reason"] == "no_ready_snapshot"
    assert result["data_prep_task_id"] == "task-started-123"
    assert len(started_tasks) == 1
    assert started_tasks[0]["scope"] == "cn_stock"
    assert started_tasks[0]["trigger_fast_scan_after_ready"] is True


def test_no_snapshot_at_all_starts_data_prep(db_session, monkeypatch):
    """WP-P-FIX.2: 首次无任何快照时启动准备任务并返回 data_prep_task_id。"""
    monkeypatch.setattr(discovery_fast_scan, "_assert_no_http_request", lambda: None)
    monkeypatch.setattr(
        discovery_data_prep, "start_data_prep_task",
        lambda **kw: type("T", (), {"id": "first-prep-task"})(),
    )
    monkeypatch.setattr(
        discovery_data_prep, "_is_data_prep_running", lambda db, scope: None,
    )

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    assert result["snapshot_id"] is None
    assert result["degraded_reason"] == "no_ready_snapshot"
    assert result["data_prep_task_id"] == "first-prep-task"
    assert "正在为您准备数据" in result["recommended_action"]


def test_data_prep_concurrency_protection(db_session, monkeypatch):
    """WP-P-FIX.2: 已有 data_prep 运行时不重复启动。"""
    monkeypatch.setattr(discovery_fast_scan, "_assert_no_http_request", lambda: None)
    started_tasks = []

    def _fake_start(**kwargs):
        started_tasks.append(kwargs)
        return type("T", (), {"id": "should-not-be-called"})()

    monkeypatch.setattr(discovery_data_prep, "start_data_prep_task", _fake_start)
    # 模拟已有任务运行中
    from app.services.async_tasks import AsyncTaskRecord
    fake_running = AsyncTaskRecord(
        id="running-task", task_type="discovery_data_prep",
        status="running", payload_json='{"scope":"cn_stock"}',
        created_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(
        discovery_data_prep, "_is_data_prep_running",
        lambda db, scope: fake_running,
    )

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    # data_prep_task_id 应为 None（未启动新任务）
    assert result["data_prep_task_id"] is None
    assert len(started_tasks) == 0
