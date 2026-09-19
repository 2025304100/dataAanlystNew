"""白盒测试 - WP2.3 观察池 API 端点。

覆盖 app/api/routes/watchlists.py 中新增的观察池端点：
1. GET  /watchlists/{id}/observations 列表富读
2. GET  /watchlists/{id}/observations?status=archived 状态筛选
3. GET  /watchlists/{id}/observations?origin_type=candidate 来源筛选
4. POST /watchlists/{id}/observations 幂等加入（重复请求返回已有记录，非 409）
5. POST /watchlists/{id}/observations/from-candidate 候选加入（单事务写来源 + 评分快照）
6. PATCH /watchlists/{id}/observations/{observation_id} 更新字段
7. POST /watchlists/{id}/observations/{observation_id}/archive 归档
8. POST /watchlists/{id}/observations/{observation_id}/restore 恢复
9. POST /watchlists/{id}/observations/batch-import 批量幂等导入
10. 404 错误：PATCH / archive 不存在的 observation_id

测试用 SQLite 内存库（db_session fixture）+ FastAPI TestClient + get_db 依赖覆盖。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_db
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.opportunity_transition_event import (
    EVENT_CANDIDATE_TO_OBSERVATION,
    EVENT_EXCLUDE,
    EVENT_RESTORE,
    OpportunityTransitionEvent,
    TYPE_OBSERVATION,
)
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_watchlist(db_session, name: str = "QA-API-Obs-WL") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="qa")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


def _make_symbol(db_session, symbol: str = "600000") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market="cn",
        theme="测试",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_scan_run(db_session, name: str = "QA-API-ScanRun") -> ScanRun:
    run = ScanRun(
        run_name=name,
        scope_snapshot="cn_stock",
        status="done",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_universe_symbol(db_session, symbol: str = "600000") -> UniverseSymbol:
    u = UniverseSymbol(
        symbol=symbol,
        name=f"基础-{symbol}",
        asset_type="stock",
        market="sh",
        region="cn",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_candidate(
    db_session,
    *,
    scan_run_id: int,
    universe_symbol_id: int,
    symbol: str = "600000",
    name: str = "QA-API-Candidate",
    quality_score: float = 78.0,
    timing_score: float = 72.0,
    priority_score: float = 82.5,
    stage: str = "hold",
    action: str = "buy",
) -> DiscoveryCandidate:
    c = DiscoveryCandidate(
        scan_run_id=scan_run_id,
        universe_symbol_id=universe_symbol_id,
        symbol=symbol,
        name=name,
        asset_type="stock",
        quality_score=quality_score,
        timing_score=timing_score,
        priority_score=priority_score,
        stage=stage,
        action=action,
        is_promoted=0,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def client(db_session):
    """构造 TestClient，依赖覆盖让 get_db 返回测试 session。"""
    from app.main import app

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    # 清理依赖覆盖，避免影响后续测试
    app.dependency_overrides.pop(get_db, None)


# ----------------------------------------------------------------------------
# 1. GET /watchlists/{id}/observations 列表富读
# ----------------------------------------------------------------------------


def test_list_observations_returns_rich_list(client, db_session):
    """【WP2.3】GET /watchlists/{id}/observations 返回富读列表。"""
    wl = _make_watchlist(db_session, name="QA-API-List")
    sym1 = _make_symbol(db_session, symbol="610001")
    sym2 = _make_symbol(db_session, symbol="610002")

    # 创建 2 个观察项
    client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym1.id, "note": "第一只"},
    )
    client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym2.id, "note": "第二只"},
    )

    resp = client.get(f"/api/v1/watchlists/{wl.id}/observations")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    # 富读字段完整性
    item = data[0]
    for key in (
        "watchlist_item_id",
        "watchlist_id",
        "symbol_id",
        "status",
        "origin_type",
        "priority",
        "tags",
        "degraded",
    ):
        assert key in item, f"observation 富读缺字段 {key}"
    # watchlist_id 应等于路径参数
    assert item["watchlist_id"] == wl.id


# ----------------------------------------------------------------------------
# 2. GET 筛选 status
# ----------------------------------------------------------------------------


def test_list_observations_filter_status_archived(client, db_session):
    """【WP2.3】GET ?status=archived 只返回归档项；默认不返回 archived。"""
    wl = _make_watchlist(db_session, name="QA-API-Status")
    sym_watching = _make_symbol(db_session, symbol="610010")
    sym_archived = _make_symbol(db_session, symbol="610011")

    r1 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym_watching.id},
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym_archived.id},
    )
    assert r2.status_code == 200
    archived_id = r2.json()["watchlist_item_id"]

    # 归档第二条
    resp_arch = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/{archived_id}/archive"
    )
    assert resp_arch.status_code == 200
    assert resp_arch.json()["status"] == "archived"

    # 默认列表只返回 watching
    resp_default = client.get(f"/api/v1/watchlists/{wl.id}/observations")
    assert resp_default.status_code == 200
    default_items = resp_default.json()
    assert len(default_items) == 1
    assert default_items[0]["status"] == "watching"

    # ?status=archived 只返回归档项
    resp_archived = client.get(
        f"/api/v1/watchlists/{wl.id}/observations?status=archived"
    )
    assert resp_archived.status_code == 200
    archived_items = resp_archived.json()
    assert len(archived_items) == 1
    assert archived_items[0]["status"] == "archived"
    assert archived_items[0]["watchlist_item_id"] == archived_id


# ----------------------------------------------------------------------------
# 3. GET 筛选 origin_type
# ----------------------------------------------------------------------------


def test_list_observations_filter_origin_type(client, db_session):
    """【WP2.3】GET ?origin_type=candidate 只返回候选来源项。"""
    wl = _make_watchlist(db_session, name="QA-API-Origin")
    sym_manual = _make_symbol(db_session, symbol="610020")
    sym_candidate = _make_symbol(db_session, symbol="610021")
    run = _make_scan_run(db_session, name="QA-API-ScanRun-Origin")
    u = _make_universe_symbol(db_session, symbol="610021")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="610021",
    )

    # 1 个 manual
    client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym_manual.id, "origin_type": "manual"},
    )

    # 1 个 candidate（走 from-candidate 端点，单事务写来源）
    resp_cand = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/from-candidate",
        json={"candidate_id": candidate.id, "watchlist_id": wl.id},
    )
    assert resp_cand.status_code == 200
    assert resp_cand.json()["origin_type"] == "candidate"

    # 筛选 candidate
    resp_filtered = client.get(
        f"/api/v1/watchlists/{wl.id}/observations?origin_type=candidate"
    )
    assert resp_filtered.status_code == 200
    items = resp_filtered.json()
    assert len(items) == 1
    assert items[0]["origin_type"] == "candidate"
    assert items[0]["origin_id"] == candidate.id

    # 筛选 manual
    resp_manual = client.get(
        f"/api/v1/watchlists/{wl.id}/observations?origin_type=manual"
    )
    assert resp_manual.status_code == 200
    items_manual = resp_manual.json()
    assert len(items_manual) == 1
    assert items_manual[0]["origin_type"] == "manual"


# ----------------------------------------------------------------------------
# 4. POST 幂等加入
# ----------------------------------------------------------------------------


def test_post_observation_idempotent_returns_same_record(client, db_session):
    """【WP2.3】POST /observations 重复请求返回相同 watchlist_item_id（非 409）。

    参照 project_memory 硬约束："幂等加入返回已有记录（非 409）"。
    """
    wl = _make_watchlist(db_session, name="QA-API-Idem")
    sym = _make_symbol(db_session, symbol="610030")

    payload = {
        "watchlist_id": wl.id,
        "symbol_id": sym.id,
        "priority": 5,
        "tags": ["科技", "龙头"],
        "note": "第一次",
    }

    # 第一次 POST → 创建新记录
    resp1 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations", json=payload
    )
    assert resp1.status_code == 200  # FastAPI POST 默认 200，未声明 status_code
    item1 = resp1.json()
    assert item1["watchlist_id"] == wl.id
    assert item1["symbol_id"] == sym.id
    assert item1["priority"] == 5
    assert item1["tags"] == ["科技", "龙头"]

    # 第二次 POST 同参数 → 返回已有记录（非 409）
    resp2 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations", json=payload
    )
    assert resp2.status_code == 200
    item2 = resp2.json()

    # 两次返回相同 watchlist_item_id
    assert item1["watchlist_item_id"] == item2["watchlist_item_id"]
    # 第一次的 note 不被覆盖（幂等）
    assert item2["note"] == "第一次"


# ----------------------------------------------------------------------------
# 5. POST /from-candidate 单事务
# ----------------------------------------------------------------------------


def test_post_observation_from_candidate_writes_origin_and_snapshot(client, db_session):
    """【WP2.3】POST /from-candidate 返回富读，origin_type='candidate'，origin_id=candidate.id。"""
    wl = _make_watchlist(db_session, name="QA-API-Cand")
    sym = _make_symbol(db_session, symbol="610040")
    run = _make_scan_run(db_session, name="QA-API-ScanRun-Cand")
    u = _make_universe_symbol(db_session, symbol="610040")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="610040",
        quality_score=78.0,
        timing_score=72.0,
        priority_score=82.5,
        stage="hold",
        action="buy",
    )

    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/from-candidate",
        json={
            "candidate_id": candidate.id,
            "watchlist_id": wl.id,
            "note": "候选加入",
            "priority": 3,
            "tags": ["候选", "科技"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    # 来源：origin_type='candidate', origin_id=candidate.id
    assert data["origin_type"] == "candidate"
    assert data["origin_id"] == candidate.id
    assert data["symbol_id"] == sym.id
    assert data["status"] == "watching"
    assert data["priority"] == 3
    assert data["note"] == "候选加入"
    assert data["tags"] == ["候选", "科技"]

    # 评分快照已写入（best-effort 复制候选字段）
    assert data["score_snapshot"] is not None
    assert data["score_snapshot"]["source"] == "discovery_candidate"
    assert data["score_snapshot"]["candidate_id"] == candidate.id
    assert data["score_snapshot"]["quality_score"] == 78.0


# ----------------------------------------------------------------------------
# 6. PATCH 更新
# ----------------------------------------------------------------------------


def test_patch_observation_updates_fields(client, db_session):
    """【WP2.3】PATCH 更新 priority / tags / status / note。"""
    wl = _make_watchlist(db_session, name="QA-API-Patch")
    sym = _make_symbol(db_session, symbol="610050")

    resp_add = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id, "priority": 1},
    )
    item_id = resp_add.json()["watchlist_item_id"]

    resp_patch = client.patch(
        f"/api/v1/watchlists/{wl.id}/observations/{item_id}",
        json={
            "priority": 99,
            "tags": ["高优", "科技"],
            "status": "ready",
            "note": "更新后笔记",
        },
    )
    assert resp_patch.status_code == 200
    data = resp_patch.json()
    assert data["priority"] == 99
    assert data["tags"] == ["高优", "科技"]
    assert data["status"] == "ready"
    assert data["note"] == "更新后笔记"
    assert data["watchlist_item_id"] == item_id


# ----------------------------------------------------------------------------
# 7. POST archive
# ----------------------------------------------------------------------------


def test_archive_observation_sets_status_archived(client, db_session):
    """【WP2.3】POST /archive 设置 status='archived'。"""
    wl = _make_watchlist(db_session, name="QA-API-Archive")
    sym = _make_symbol(db_session, symbol="610060")

    resp_add = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id},
    )
    item_id = resp_add.json()["watchlist_item_id"]
    assert resp_add.json()["status"] == "watching"

    resp_arch = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/{item_id}/archive"
    )
    assert resp_arch.status_code == 200
    data = resp_arch.json()
    assert data["status"] == "archived"
    assert data["archived_at"] is not None
    assert data["watchlist_item_id"] == item_id


# ----------------------------------------------------------------------------
# 8. POST restore
# ----------------------------------------------------------------------------


def test_restore_observation_resets_status_watching(client, db_session):
    """【WP2.3】归档后 POST /restore 恢复 status='watching'。"""
    wl = _make_watchlist(db_session, name="QA-API-Restore")
    sym = _make_symbol(db_session, symbol="610070")

    resp_add = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id},
    )
    item_id = resp_add.json()["watchlist_item_id"]

    # 归档
    client.post(f"/api/v1/watchlists/{wl.id}/observations/{item_id}/archive")

    # 恢复
    resp_restore = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/{item_id}/restore"
    )
    assert resp_restore.status_code == 200
    data = resp_restore.json()
    assert data["status"] == "watching"
    assert data["archived_at"] is None
    assert data["watchlist_item_id"] == item_id


# ----------------------------------------------------------------------------
# 9. POST batch-import
# ----------------------------------------------------------------------------


def test_batch_import_observations_idempotent(client, db_session):
    """【WP2.3】批量导入 5 个，3 新 + 2 已存在 → imported=3, existing=2。"""
    wl = _make_watchlist(db_session, name="QA-API-Batch")
    sym_new1 = _make_symbol(db_session, symbol="610080")
    sym_new2 = _make_symbol(db_session, symbol="610081")
    sym_new3 = _make_symbol(db_session, symbol="610082")
    sym_exist1 = _make_symbol(db_session, symbol="610083")
    sym_exist2 = _make_symbol(db_session, symbol="610084")

    # 先创建 2 个已存在项
    client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym_exist1.id},
    )
    client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym_exist2.id},
    )

    # 批量导入 5 个
    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/batch-import",
        json={
            "watchlist_id": wl.id,
            "items": [
                {"symbol_id": sym_new1.id, "priority": 1},
                {"symbol_id": sym_new2.id, "priority": 2},
                {"symbol_id": sym_new3.id, "priority": 3},
                {"symbol_id": sym_exist1.id, "priority": 0},
                {"symbol_id": sym_exist2.id, "priority": 0},
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] == 3
    assert data["existing"] == 2
    assert data["failed"] == 0
    assert data["errors"] == []

    # 重复执行 → 全部 existing（验证幂等）
    resp2 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/batch-import",
        json={
            "watchlist_id": wl.id,
            "items": [
                {"symbol_id": sym_new1.id},
                {"symbol_id": sym_new2.id},
                {"symbol_id": sym_new3.id},
            ],
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["imported"] == 0
    assert data2["existing"] == 3
    assert data2["failed"] == 0


# ----------------------------------------------------------------------------
# 10. 404 错误
# ----------------------------------------------------------------------------


def test_patch_observation_not_found_returns_404(client, db_session):
    """【WP2.3】PATCH 不存在的 observation_id 返回 404。"""
    wl = _make_watchlist(db_session, name="QA-API-404-Patch")

    resp = client.patch(
        f"/api/v1/watchlists/{wl.id}/observations/99999",
        json={"priority": 5},
    )
    assert resp.status_code == 404
    body = resp.json()
    # 全局异常处理器将 HTTPException 包装为 UserError 协议
    assert body.get("error_code") == "NOT_FOUND"


def test_archive_observation_not_found_returns_404(client, db_session):
    """【WP2.3】POST /archive 不存在返回 404。"""
    wl = _make_watchlist(db_session, name="QA-API-404-Archive")

    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/99999/archive"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error_code") == "NOT_FOUND"


def test_restore_observation_not_found_returns_404(client, db_session):
    """【WP2.3】POST /restore 不存在返回 404。"""
    wl = _make_watchlist(db_session, name="QA-API-404-Restore")

    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/99999/restore"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error_code") == "NOT_FOUND"


# ----------------------------------------------------------------------------
# 11. 集成：归档后再次 POST 同 symbol 自动恢复
# ----------------------------------------------------------------------------


def test_post_after_archive_restores_via_api(client, db_session):
    """【WP2.3】归档后再次 POST 同 (watchlist, symbol) → 返回同一记录且 status='watching'。"""
    wl = _make_watchlist(db_session, name="QA-API-ReAdd")
    sym = _make_symbol(db_session, symbol="610090")

    # 第一次加入
    resp1 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id},
    )
    item_id = resp1.json()["watchlist_item_id"]

    # 归档
    client.post(f"/api/v1/watchlists/{wl.id}/observations/{item_id}/archive")

    # 再次 POST 同 (watchlist, symbol) → 恢复
    resp2 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id},
    )
    assert resp2.status_code == 200
    data = resp2.json()
    # 返回同一条记录，状态已恢复
    assert data["watchlist_item_id"] == item_id
    assert data["status"] == "watching"
    assert data["archived_at"] is None


# ============================================================================
# UAT-PAGES.1 P1-02：路由层审计事件 + 真实契约 + 双击幂等
#
# 覆盖：
# 1. POST /from-candidate 写入 OpportunityTransitionEvent 审计事件
# 2. POST /archive 写入 exclude 审计事件
# 3. POST /restore 写入 restore 审计事件
# 4. 候选双击 /from-candidate 只产生 1 个观察项 + 1 个审计事件
# 5. GET /observations 返回 ObservationRead 完整字段（真实契约）
# ============================================================================


def test_post_from_candidate_writes_audit_event(client, db_session):
    """【UAT-PAGES.1 P1-02 审计】POST /from-candidate 写入候选→观察审计事件。"""
    wl = _make_watchlist(db_session, name="QA-API-Audit-Cand")
    sym = _make_symbol(db_session, symbol="610100")
    run = _make_scan_run(db_session, name="QA-API-ScanRun-Audit")
    u = _make_universe_symbol(db_session, symbol="610100")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="610100",
    )

    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/from-candidate",
        json={"candidate_id": candidate.id, "watchlist_id": wl.id},
    )
    assert resp.status_code == 200

    # 审计事件已写入
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_CANDIDATE_TO_OBSERVATION,
            OpportunityTransitionEvent.source_id == candidate.id,
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].target_type == TYPE_OBSERVATION
    assert events[0].target_id == wl.id


def test_archive_endpoint_writes_audit_event(client, db_session):
    """【UAT-PAGES.1 P1-02 审计】POST /archive 写入 exclude 审计事件。"""
    wl = _make_watchlist(db_session, name="QA-API-Audit-Arch")
    sym = _make_symbol(db_session, symbol="610110")

    resp_add = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id},
    )
    item_id = resp_add.json()["watchlist_item_id"]

    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/{item_id}/archive"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"

    # exclude 审计事件已写入
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_EXCLUDE,
            OpportunityTransitionEvent.source_id == item_id,
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].source_type == TYPE_OBSERVATION


def test_restore_endpoint_writes_audit_event(client, db_session):
    """【UAT-PAGES.1 P1-02 审计】POST /restore 写入 restore 审计事件。"""
    wl = _make_watchlist(db_session, name="QA-API-Audit-Rst")
    sym = _make_symbol(db_session, symbol="610120")

    resp_add = client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={"watchlist_id": wl.id, "symbol_id": sym.id},
    )
    item_id = resp_add.json()["watchlist_item_id"]

    # 先归档
    client.post(f"/api/v1/watchlists/{wl.id}/observations/{item_id}/archive")

    # 恢复
    resp = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/{item_id}/restore"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "watching"

    # restore 审计事件已写入
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_RESTORE,
            OpportunityTransitionEvent.source_id == item_id,
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].source_type == TYPE_OBSERVATION


def test_from_candidate_double_click_idempotent(client, db_session):
    """【UAT-PAGES.1 P1-02 幂等】候选双击 /from-candidate 只产生 1 个观察项 + 1 个审计事件。"""
    wl = _make_watchlist(db_session, name="QA-API-DBL")
    sym = _make_symbol(db_session, symbol="610130")
    run = _make_scan_run(db_session, name="QA-API-ScanRun-DBL")
    u = _make_universe_symbol(db_session, symbol="610130")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="610130",
    )

    # 第一次点击
    resp1 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/from-candidate",
        json={"candidate_id": candidate.id, "watchlist_id": wl.id},
    )
    assert resp1.status_code == 200
    item_id_1 = resp1.json()["watchlist_item_id"]

    # 第二次点击（双击）
    resp2 = client.post(
        f"/api/v1/watchlists/{wl.id}/observations/from-candidate",
        json={"candidate_id": candidate.id, "watchlist_id": wl.id},
    )
    assert resp2.status_code == 200
    item_id_2 = resp2.json()["watchlist_item_id"]

    # 返回同一观察项
    assert item_id_1 == item_id_2

    # DB 中只有 1 个观察项
    items = db_session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id)
    ).scalars().all()
    assert len(items) == 1

    # DB 中只有 1 个候选→观察审计事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_CANDIDATE_TO_OBSERVATION
        )
    ).scalars().all()
    assert len(events) == 1


def test_list_observations_returns_complete_contract(client, db_session):
    """【UAT-PAGES.1 P1-02 真实契约】GET /observations 返回 ObservationRead 完整字段。

    前后端字段一致：前端 ObservationItem 接口需要的所有字段在 API 响应中存在，
    不会出现 undefined 导致渲染异常。
    """
    wl = _make_watchlist(db_session, name="QA-API-Contract")
    sym = _make_symbol(db_session, symbol="610140")

    client.post(
        f"/api/v1/watchlists/{wl.id}/observations",
        json={
            "watchlist_id": wl.id,
            "symbol_id": sym.id,
            "priority": 50,
            "tags": ["契约"],
            "note": "契约测试",
        },
    )

    resp = client.get(f"/api/v1/watchlists/{wl.id}/observations")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    item = data[0]

    # 断言 ObservationRead 全部字段在响应中存在（不缺字段）
    expected_fields = [
        "watchlist_item_id", "watchlist_id", "watchlist_name", "symbol_id",
        "symbol", "added_at", "updated_at", "archived_at", "origin_type",
        "origin_id", "reason", "score_snapshot", "status", "priority",
        "tags", "note", "target_portfolio_id", "target_portfolio_name",
        "latest_price", "latest_price_date", "price_change_pct",
        "latest_total_score", "latest_quality_score", "latest_timing_score",
        "latest_score_date", "data_credibility", "bar_count", "has_position",
        "position_portfolio_name", "degraded", "degraded_reason",
    ]
    for field in expected_fields:
        assert field in item, f"API 响应缺少字段: {field}"

    # 关键字段值正确
    assert item["symbol"] == "610140"
    assert item["origin_type"] == "manual"
    assert item["status"] == "watching"
    assert item["priority"] == 50
    assert item["tags"] == ["契约"]
    assert item["note"] == "契约测试"
    assert item["has_position"] is False
    assert item["degraded"] is False
