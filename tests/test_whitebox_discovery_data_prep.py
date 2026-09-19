"""白盒测试 - WP-P.4 数据准备与用户扫描分离。

覆盖：
1. start_data_prep_task 任务创建与并发保护
2. get_ready_snapshot 在 None/building/ready/superseded 各场景的行为
3. run_fast_scan 无快照时返回 degraded_reason
4. run_fast_scan 有快照时返回正确 cache_key 与 coarse_match_count
5. run_fast_scan HTTP 门禁（monkeypatch urllib/requests/httpx，断言零调用）
6. _assert_no_http_request 在入口和出口都被调用
7. cache_hit 在 WP-P.4 中始终返回 False（WP-P.6 占位）
8. API 路由集成：POST /discovery/fast-scan、POST /discovery/data-prep、GET /discovery/snapshot/status
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db.session import get_db
from app.models import *  # noqa: F401,F403 - 确保所有模型被注册到 Base.metadata
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services import discovery_data_prep, discovery_fast_scan

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
    symbol_count: int | None = None,
    dirty_symbol_count: int | None = None,
) -> DiscoveryScoreSnapshot:
    """创建一个快照记录。"""
    if trade_date is None:
        trade_date = datetime(2026, 7, 19, 0, 0, 0)
    snap = DiscoveryScoreSnapshot(
        scope=scope,
        trade_date=trade_date,
        status=status,
        generated_at=generated_at,
        symbol_count=symbol_count,
        dirty_symbol_count=dirty_symbol_count,
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
) -> DiscoveryScoreSnapshotItem:
    """创建一个快照 item 记录。"""
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


# ============================================================================
# 1. start_data_prep_task 创建与并发保护
# ============================================================================

def test_start_data_prep_task_creates_task(db_session, monkeypatch):
    """start_data_prep_task 创建 AsyncTaskRecord 并返回 AsyncTaskRead。"""
    # 拦截 worker 启动，避免真实执行数据同步
    started_workers: list[tuple] = []
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: started_workers.append((task_id, worker_func)),
    )

    task_read = discovery_data_prep.start_data_prep_task(
        scope="cn-stock",
        trade_date=date(2026, 7, 19),
        force_full_rebuild=False,
        trigger_fast_scan_after_ready=False,
    )

    assert task_read.id  # uuid4.hex
    assert task_read.task_type == discovery_data_prep.TASK_TYPE_DATA_PREP
    assert task_read.status in ("queued", "running")
    assert len(started_workers) == 1
    assert started_workers[0][0] == task_read.id


def test_start_data_prep_task_concurrency_protection(db_session, monkeypatch):
    """同一 scope 已有 queued/running 任务时，再次调用返回同一任务。"""
    # 拦截 worker 启动
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: None,
    )

    first = discovery_data_prep.start_data_prep_task(scope="cn-stock")
    second = discovery_data_prep.start_data_prep_task(scope="cn_stock")  # 兼容格式

    assert first.id == second.id  # 同一任务


def test_start_data_prep_task_invalid_scope_raises(db_session, monkeypatch):
    """非法 scope 应抛出 ValueError。"""
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: None,
    )
    with pytest.raises(ValueError):
        discovery_data_prep.start_data_prep_task(scope="invalid-scope")


# ============================================================================
# 2. get_ready_snapshot 各场景
# ============================================================================

def test_get_ready_snapshot_returns_none_when_empty(db_session):
    """空库无任何快照 → 返回 None。"""
    result = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert result is None


def test_get_ready_snapshot_returns_ready(db_session):
    """存在 ready 快照 → 返回该快照。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
    )
    db_session.commit()

    result = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert result is not None
    assert result.id == snap.id
    assert result.status == "ready"


def test_get_ready_snapshot_never_returns_building(db_session):
    """存在 building 快照但无 ready → 返回 None（绝不读半成品）。"""
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        generated_at=None,
    )
    db_session.commit()

    result = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert result is None


def test_get_ready_snapshot_never_returns_superseded(db_session):
    """存在 superseded 快照但无 ready → 返回 None。"""
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="superseded",
        generated_at=datetime(2026, 7, 18, 10, 0, 0),
    )
    db_session.commit()

    result = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert result is None


def test_get_ready_snapshot_returns_latest_ready(db_session):
    """多个 ready 快照 → 返回 generated_at 最新的。

    注意：DiscoveryScoreSnapshot 有唯一约束 (scope, status, trade_date)，
    所以两个 ready 快照的 trade_date 必须不同。
    """
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        trade_date=datetime(2026, 7, 18, 0, 0, 0),
        generated_at=datetime(2026, 7, 18, 10, 0, 0),
    )
    latest = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        trade_date=datetime(2026, 7, 19, 0, 0, 0),
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
    )
    db_session.commit()

    result = discovery_fast_scan.get_ready_snapshot(db_session, "cn_stock")
    assert result is not None
    assert result.id == latest.id


def test_get_ready_snapshot_supports_hyphen_scope(db_session):
    """连字符格式 scope（cn-stock）应等价于下划线格式（cn_stock）。"""
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
    )
    db_session.commit()

    result = discovery_fast_scan.get_ready_snapshot(db_session, "cn-stock")
    assert result is not None
    assert result.scope == "cn_stock"


# ============================================================================
# 3. run_fast_scan 无快照
# ============================================================================

def test_run_fast_scan_no_snapshot_returns_degraded(db_session):
    """无可用快照 → degraded_reason="no_ready_snapshot"，recommended_action 提示用户。"""
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )

    assert result["snapshot_id"] is None
    assert result["degraded_reason"] == "no_ready_snapshot"
    assert result["recommended_action"] is not None
    assert result["coarse_match_count"] == 0
    assert result["advanced_match_count"] == 0
    assert result["result_rows_written"] == 0
    assert result["cache_hit"] is False
    assert result["results"] == []
    # 应在 10 秒内返回
    assert result["duration_ms"] < 10_000


def test_run_fast_scan_too_many_indicators_returns_degraded(db_session):
    """自定义指标超 5 个 → degraded_reason="too_many_custom_indicators"。"""
    # 先建一个 ready 快照，避免被 no_ready_snapshot 分支提前返回
    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_count=10,
    )
    db_session.commit()

    indicator_plan = {f"ind_{i}": {"formula": "close"} for i in range(6)}
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        indicator_plan=indicator_plan,
        db=db_session,
    )

    assert result["degraded_reason"] == "too_many_custom_indicators"
    assert result["recommended_action"] is not None
    assert "5" in result["recommended_action"]


# ============================================================================
# 4. run_fast_scan 有快照
# ============================================================================

def _setup_ready_snapshot_with_items(db_session, *, item_count: int = 5):
    """创建 ready 快照 + 多个 item 用于 run_fast_scan 测试。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_count=item_count,
    )
    for i in range(item_count):
        sym = _make_symbol(db_session, symbol=f"00000{i}")
        us = _make_universe_symbol(db_session, symbol=f"00000{i}")
        _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=80.0 - i,  # 80, 79, 78, ... 让 min_score=55 都能命中
            stage="accumulate",
            action="buy",
        )
    db_session.commit()
    return snap


def test_run_fast_scan_with_snapshot_returns_results(db_session):
    """有 ready 快照 → 返回排序后的结果，coarse_match_count > 0。"""
    snap = _setup_ready_snapshot_with_items(db_session, item_count=5)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )

    assert result["snapshot_id"] == snap.id
    assert result["degraded_reason"] is None
    assert result["coarse_match_count"] == 5
    assert result["advanced_match_count"] == 5
    assert result["result_rows_written"] == 5
    assert len(result["results"]) == 5
    # 验证按 priority_score 倒序排序
    scores = [r["priority_score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


def test_run_fast_scan_cache_key_format(db_session):
    """cache_key 应包含 snapshot_id / scope / min_score / filter_hash 等要素。"""
    snap = _setup_ready_snapshot_with_items(db_session, item_count=3)

    result = discovery_fast_scan.run_fast_scan(
        scope="cn-stock",
        min_score=60,
        asset_types=["stock"],
        stages=["accumulate"],
        actions=["buy"],
        portfolio_id=42,
        portfolio_rule_id=7,
        limit=100,
        db=db_session,
    )

    cache_key = result["cache_key"]
    assert cache_key is not None
    assert str(snap.id) in cache_key
    assert "cn_stock" in cache_key  # 归一化为下划线
    assert "60" in cache_key  # min_score
    assert "42" in cache_key  # portfolio_id
    assert "7" in cache_key  # portfolio_rule_id
    assert "100" in cache_key  # limit


def test_run_fast_scan_cache_key_stable_for_same_params(db_session):
    """相同参数两次扫描应产生相同 cache_key（dict 顺序无关）。"""
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    indicator_plan = {"b": {"formula": "close"}, "a": {"formula": "open"}}
    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        asset_types=["stock", "etf"],
        indicator_plan=indicator_plan,
        db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        asset_types=["etf", "stock"],  # 顺序不同
        indicator_plan={"a": {"formula": "open"}, "b": {"formula": "close"}},  # 顺序不同
        db=db_session,
    )

    assert r1["cache_key"] == r2["cache_key"]


def test_run_fast_scan_min_score_filter(db_session):
    """min_score 应正确过滤掉低于阈值的标的。"""
    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_count=3,
    )
    # 三条记录：priority_score 分别 50 / 60 / 70
    for i, score in enumerate([50, 60, 70]):
        sym = _make_symbol(db_session, symbol=f"00000{i}")
        us = _make_universe_symbol(db_session, symbol=f"00000{i}")
        _make_snapshot_item(
            db_session,
            snapshot_id=snap.id,
            universe_symbol_id=us.id,
            symbol_id=sym.id,
            priority_score=score,
        )
    db_session.commit()

    # min_score=55 应只匹配 60 / 70 两条
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )
    assert result["coarse_match_count"] == 2
    assert all(r["priority_score"] >= 55 for r in result["results"])


# ============================================================================
# 5. run_fast_scan HTTP 门禁
# ============================================================================

def test_run_fast_scan_does_not_call_urllib_requests_httpx(db_session, monkeypatch):
    """扫描路径不应调用 urllib.request.urlopen / requests.Session / httpx.Client。"""
    snap = _setup_ready_snapshot_with_items(db_session, item_count=2)

    # 安装监控钩子：任何对 urllib / requests / httpx 的调用都抛 AssertionError
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

    # 执行扫描，若调用任何 HTTP 方法都会抛 AssertionError
    result = discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )
    assert result["degraded_reason"] is None
    assert result["coarse_match_count"] == 2


def test_assert_no_http_request_called_at_entry_and_exit(db_session, monkeypatch):
    """_assert_no_http_request 应在 run_fast_scan 入口和出口都被调用。"""
    _setup_ready_snapshot_with_items(db_session, item_count=1)

    call_count = {"n": 0}

    def _spy():
        call_count["n"] += 1

    monkeypatch.setattr(discovery_fast_scan, "_assert_no_http_request", _spy)

    discovery_fast_scan.run_fast_scan(
        scope="cn_stock",
        min_score=55,
        db=db_session,
    )

    # 入口 1 次 + 出口 1 次 = 至少 2 次
    assert call_count["n"] >= 2


def test_assert_no_http_request_default_is_noop():
    """_assert_no_http_request 默认实现为 no-op（不抛异常）。"""
    # 直接调用应不抛异常
    discovery_fast_scan._assert_no_http_request()


# ============================================================================
# 7. cache_hit 在 WP-P.6 中二次扫描命中缓存
# ============================================================================

def test_run_fast_scan_cache_hit_on_second_call_wp_p6(db_session):
    """WP-P.6：首次扫描 cache_hit=False；相同参数二次扫描 cache_hit=True。"""
    _setup_ready_snapshot_with_items(db_session, item_count=2)

    r1 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )
    r2 = discovery_fast_scan.run_fast_scan(
        scope="cn_stock", min_score=55, db=db_session,
    )

    # 首次：未命中缓存，写入新 ScanRun
    assert r1["cache_hit"] is False
    assert r1["cached_from_scan_run_id"] is None
    assert r1["result_rows_written"] > 0
    # 二次：相同参数命中缓存，不重复写 ScanResult
    assert r2["cache_hit"] is True
    assert r2["cached_from_scan_run_id"] is not None
    assert r2["result_rows_written"] == 0  # 命中缓存不重复写


# ============================================================================
# 8. API 路由集成测试
# ============================================================================

def test_discovery_routes_are_registered():
    """三个新路由已注册到主 app。"""
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/discovery/fast-scan" in paths
    assert "/api/v1/discovery/data-prep" in paths
    assert "/api/v1/discovery/snapshot/status" in paths


def test_fast_scan_api_route_no_snapshot(db_session, monkeypatch):
    """POST /discovery/fast-scan 在无快照时返回 degraded_reason。"""
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
        resp = client.post(
            "/api/v1/discovery/fast-scan",
            json={"scope": "cn-stock", "min_score": 55},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["snapshot_id"] is None
        assert body["degraded_reason"] == "no_ready_snapshot"
        assert body["scope"] == "cn_stock"
    finally:
        app.dependency_overrides.clear()


def test_fast_scan_api_route_with_snapshot(db_session, monkeypatch):
    """POST /discovery/fast-scan 在有快照时返回候选结果。"""
    from app.main import app

    _setup_ready_snapshot_with_items(db_session, item_count=3)

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
        resp = client.post(
            "/api/v1/discovery/fast-scan",
            json={"scope": "cn-stock", "min_score": 55, "limit": 50},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["snapshot_id"] is not None
        assert body["degraded_reason"] is None
        assert body["coarse_match_count"] == 3
        assert len(body["results"]) == 3
        assert body["cache_key"] is not None
    finally:
        app.dependency_overrides.clear()


def test_data_prep_api_route_creates_task(db_session, monkeypatch):
    """POST /discovery/data-prep 创建 data_prep 任务并返回 AsyncTaskRead。"""
    from app.main import app

    # 拦截 worker 启动
    monkeypatch.setattr(
        discovery_data_prep,
        "_start_worker",
        lambda task_id, worker_func: None,
    )

    resp = None
    try:
        # data-prep 路由不依赖 get_db，直接调用
        client = TestClient(app)
        resp = client.post(
            "/api/v1/discovery/data-prep",
            json={
                "scope": "cn-stock",
                "trade_date": "2026-07-19",
                "force_full_rebuild": False,
                "trigger_fast_scan_after_ready": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"]
        assert body["task_type"] == "discovery_data_prep"
        assert body["status"] in ("queued", "running")
    finally:
        # data-prep 路由不使用 dependency_overrides，无需清理
        pass


def test_snapshot_status_api_route_no_snapshot(db_session, monkeypatch):
    """GET /discovery/snapshot/status 在无快照时返回 has_ready_snapshot=False。"""
    from app.main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "cn-stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "cn_stock"
        assert body["has_ready_snapshot"] is False
        assert body["has_building_snapshot"] is False
        assert body["ready_snapshot_id"] is None
        assert body["recommended_action"] is not None
        assert "启动数据准备" in body["recommended_action"]
    finally:
        app.dependency_overrides.clear()


def test_snapshot_status_api_route_with_ready(db_session, monkeypatch):
    """GET /discovery/snapshot/status 在有 ready 快照时返回完整状态。"""
    from app.main import app

    snap = _make_snapshot(
        db_session,
        scope="cn_stock",
        status="ready",
        generated_at=datetime(2026, 7, 19, 10, 0, 0),
        symbol_count=100,
        dirty_symbol_count=5,
    )
    db_session.commit()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "cn-stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_ready_snapshot"] is True
        assert body["ready_snapshot_id"] == snap.id
        assert body["ready_snapshot_symbol_count"] == 100
        assert body["ready_snapshot_dirty_symbol_count"] == 5
        assert "可执行快速扫描" in body["recommended_action"]
    finally:
        app.dependency_overrides.clear()


def test_snapshot_status_api_route_with_building(db_session, monkeypatch):
    """GET /discovery/snapshot/status 在仅有 building 快照时返回正确提示。"""
    from app.main import app

    _make_snapshot(
        db_session,
        scope="cn_stock",
        status="building",
        generated_at=None,
    )
    db_session.commit()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        resp = client.get(
            "/api/v1/discovery/snapshot/status",
            params={"scope": "cn_stock"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_ready_snapshot"] is False
        assert body["has_building_snapshot"] is True
        assert "数据准备进行中" in body["recommended_action"]
    finally:
        app.dependency_overrides.clear()


# ============================================================================
# Schema 验证测试
# ============================================================================

def test_fast_scan_request_schema_defaults():
    """FastScanRequest 默认值正确。"""
    from app.schemas.discovery import FastScanRequest

    req = FastScanRequest()
    assert req.scope == "cn-stock"
    assert req.min_score == 55
    assert req.limit == 300
    assert req.asset_types is None
    assert req.indicator_plan is None


def test_data_prep_request_schema_defaults():
    """DataPrepRequest 默认值正确。"""
    from app.schemas.discovery import DataPrepRequest

    req = DataPrepRequest()
    assert req.scope == "cn-stock"
    assert req.trade_date is None
    assert req.force_full_rebuild is False
    assert req.trigger_fast_scan_after_ready is False
    assert req.fast_scan_params is None


def test_snapshot_status_read_schema_serialization():
    """SnapshotStatusRead 可正确序列化 None 与非 None 字段。"""
    from app.schemas.discovery import SnapshotStatusRead

    s = SnapshotStatusRead(scope="cn_stock")
    dumped = s.model_dump()
    assert dumped["scope"] == "cn_stock"
    assert dumped["has_ready_snapshot"] is False
    assert dumped["ready_snapshot_id"] is None
    assert dumped["recommended_action"] is None

    s2 = SnapshotStatusRead(
        scope="cn_stock",
        has_ready_snapshot=True,
        ready_snapshot_id=42,
        ready_snapshot_symbol_count=100,
        recommended_action="快照已就绪",
    )
    dumped2 = s2.model_dump()
    assert dumped2["has_ready_snapshot"] is True
    assert dumped2["ready_snapshot_id"] == 42
    assert dumped2["ready_snapshot_symbol_count"] == 100
