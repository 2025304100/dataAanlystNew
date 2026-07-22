"""白盒测试 - WP4.4 组合成员 API 端点。

覆盖 app/api/routes/portfolios.py 中新增的成员端点：
1. GET  /portfolios/{id}/members 列表
2. GET  /portfolios/{id}/members?status=active 状态筛选
3. GET  /portfolios/{id}/members?include_archived=true 包含归档
4. POST /portfolios/{id}/members 创建成员
5. POST /portfolios/{id}/members 重复创建返回 409
6. PATCH /portfolios/{id}/members/{member_id} 更新字段
7. POST /portfolios/{id}/members/{member_id}/archive 无持仓归档
8. POST /portfolios/{id}/members/{member_id}/archive 有持仓返回 409
9. POST /portfolios/{id}/members/{member_id}/archive {"force": true} 强制归档
10. POST /portfolios/{id}/members/{member_id}/pause 暂停
11. POST /portfolios/{id}/members/{member_id}/restore 恢复
12. POST /portfolios/{id}/members/backfill 回填持仓
13. POST /portfolios/{id}/members/backfill {"dry_run": true} 干跑模式
14. 404 错误：PATCH / archive 不存在的 member_id

测试用 SQLite 内存库（db_session fixture）+ FastAPI TestClient + get_db 依赖覆盖。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.portfolio import Portfolio, Position
from app.models.symbol import Symbol


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_portfolio(db_session, name: str = "QA-API-PM-PF") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="cash",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


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


def _make_position(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    quantity: float = 100.0,
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=10.0,
        latest_price=12.0,
        market_value=quantity * 12.0,
        position_pct=0.1,
        asset_type="stock",
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


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
# 1. GET /portfolios/{id}/members 列表
# ----------------------------------------------------------------------------


def test_list_portfolio_members_returns_list(client, db_session):
    """【WP4.4】GET /portfolios/{id}/members 返回成员列表。"""
    pf = _make_portfolio(db_session, name="QA-API-List-PF")
    sym1 = _make_symbol(db_session, symbol="610001")
    sym2 = _make_symbol(db_session, symbol="610002")

    r1 = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym1.id, "note": "第一只"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym2.id, "note": "第二只"},
    )
    assert r2.status_code == 200

    resp = client.get(f"/api/v1/portfolios/{pf.id}/members")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    # 字段完整性
    item = data[0]
    for key in (
        "id",
        "portfolio_id",
        "symbol_id",
        "status",
        "execution_mode",
        "source_type",
        "effective_from",
        "effective_to",
        "manual_lock",
        "priority",
    ):
        assert key in item, f"member 缺字段 {key}"
    # portfolio_id 应等于路径参数
    assert item["portfolio_id"] == pf.id


# ----------------------------------------------------------------------------
# 2. GET 筛选 status
# ----------------------------------------------------------------------------


def test_list_portfolio_members_filter_status(client, db_session):
    """【WP4.4】GET ?status=active 只返回 active 成员。"""
    pf = _make_portfolio(db_session, name="QA-API-Status-PF")
    sym_active = _make_symbol(db_session, symbol="610010")
    sym_paused = _make_symbol(db_session, symbol="610011")
    sym_archived = _make_symbol(db_session, symbol="610012")

    # 创建 active / paused / archived 各一个
    r_active = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym_active.id, "status": "active"},
    )
    assert r_active.status_code == 200

    r_paused = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym_paused.id, "status": "paused"},
    )
    assert r_paused.status_code == 200

    r_archived = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym_archived.id, "status": "active"},
    )
    assert r_archived.status_code == 200
    archived_id = r_archived.json()["id"]
    # 归档第三条（无持仓）
    r_archive = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{archived_id}/archive"
    )
    assert r_archive.status_code == 200
    assert r_archive.json()["status"] == "archived"

    # ?status=active 只返回 active
    resp = client.get(f"/api/v1/portfolios/{pf.id}/members?status=active")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["status"] == "active"
    assert items[0]["symbol_id"] == sym_active.id

    # ?status=paused 只返回 paused
    resp_paused = client.get(f"/api/v1/portfolios/{pf.id}/members?status=paused")
    assert resp_paused.status_code == 200
    paused_items = resp_paused.json()
    assert len(paused_items) == 1
    assert paused_items[0]["status"] == "paused"


# ----------------------------------------------------------------------------
# 3. GET include_archived
# ----------------------------------------------------------------------------


def test_list_portfolio_members_include_archived(client, db_session):
    """【WP4.4】GET ?include_archived=true 包含归档成员。"""
    pf = _make_portfolio(db_session, name="QA-API-InclArch-PF")
    sym1 = _make_symbol(db_session, symbol="610020")
    sym2 = _make_symbol(db_session, symbol="610021")

    r1 = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym1.id},
    )
    r2 = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym2.id},
    )
    archived_id = r2.json()["id"]
    client.post(f"/api/v1/portfolios/{pf.id}/members/{archived_id}/archive")

    # 默认不含归档
    resp_default = client.get(f"/api/v1/portfolios/{pf.id}/members")
    assert resp_default.status_code == 200
    default_items = resp_default.json()
    assert len(default_items) == 1
    assert default_items[0]["status"] == "active"

    # include_archived=true 含归档
    resp_archived = client.get(
        f"/api/v1/portfolios/{pf.id}/members?include_archived=true"
    )
    assert resp_archived.status_code == 200
    archived_items = resp_archived.json()
    assert len(archived_items) == 2
    statuses = {item["status"] for item in archived_items}
    assert "archived" in statuses
    assert "active" in statuses


# ----------------------------------------------------------------------------
# 4. POST 创建成员
# ----------------------------------------------------------------------------


def test_post_portfolio_member_creates(client, db_session):
    """【WP4.4】POST /members 创建成员，返回 200 + 成员字段。"""
    pf = _make_portfolio(db_session, name="QA-API-Create-PF")
    sym = _make_symbol(db_session, symbol="610030")

    resp = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={
            "symbol_id": sym.id,
            "status": "active",
            "execution_mode": "manual",
            "source_type": "manual",
            "priority": 5,
            "note": "测试创建",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] is not None
    assert data["portfolio_id"] == pf.id
    assert data["symbol_id"] == sym.id
    assert data["status"] == "active"
    assert data["execution_mode"] == "manual"
    assert data["source_type"] == "manual"
    assert data["priority"] == 5
    assert data["note"] == "测试创建"
    assert data["effective_from"] is not None
    assert data["effective_to"] is None


# ----------------------------------------------------------------------------
# 5. POST 重复创建返回 409
# ----------------------------------------------------------------------------


def test_post_portfolio_member_duplicate_returns_409(client, db_session):
    """【WP4.4】同组合同标的重复创建返回 409。"""
    pf = _make_portfolio(db_session, name="QA-API-Dup-PF")
    sym = _make_symbol(db_session, symbol="610040")

    payload = {"symbol_id": sym.id}

    r1 = client.post(f"/api/v1/portfolios/{pf.id}/members", json=payload)
    assert r1.status_code == 200

    r2 = client.post(f"/api/v1/portfolios/{pf.id}/members", json=payload)
    assert r2.status_code == 409


# ----------------------------------------------------------------------------
# 6. PATCH 更新
# ----------------------------------------------------------------------------


def test_patch_portfolio_member_updates_fields(client, db_session):
    """【WP4.4】PATCH 更新 status / execution_mode / priority / note。"""
    pf = _make_portfolio(db_session, name="QA-API-Patch-PF")
    sym = _make_symbol(db_session, symbol="610050")

    r_create = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym.id, "priority": 1},
    )
    member_id = r_create.json()["id"]

    r_patch = client.patch(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}",
        json={
            "status": "paused",
            "execution_mode": "confirm",
            "priority": 99,
            "note": "更新后笔记",
        },
    )
    assert r_patch.status_code == 200
    data = r_patch.json()
    assert data["status"] == "paused"
    assert data["execution_mode"] == "confirm"
    assert data["priority"] == 99
    assert data["note"] == "更新后笔记"
    assert data["id"] == member_id


# ----------------------------------------------------------------------------
# 7. POST archive 无持仓
# ----------------------------------------------------------------------------


def test_archive_portfolio_member_no_position(client, db_session):
    """【WP4.4】无持仓成员归档：status='archived'，effective_to 非空。"""
    pf = _make_portfolio(db_session, name="QA-API-Archive-PF")
    sym = _make_symbol(db_session, symbol="610060")

    r_create = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym.id},
    )
    member_id = r_create.json()["id"]
    assert r_create.json()["effective_to"] is None

    r_archive = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}/archive"
    )
    assert r_archive.status_code == 200
    data = r_archive.json()
    assert data["status"] == "archived"
    assert data["effective_to"] is not None
    assert data["id"] == member_id


# ----------------------------------------------------------------------------
# 8. POST archive 有持仓返回 409
# ----------------------------------------------------------------------------


def test_archive_portfolio_member_with_position_returns_409(client, db_session):
    """【WP4.4】有持仓的成员归档返回 409 + MemberHasPositionError 信息。

    参照 spec line 213-215："用户尝试归档存在持仓的成员时，
    提示并要求选择'仅停止买入'或先卖出"。
    """
    pf = _make_portfolio(db_session, name="QA-API-ArchivePos-PF")
    sym = _make_symbol(db_session, symbol="610070")

    r_create = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym.id},
    )
    member_id = r_create.json()["id"]

    # 添加持仓
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )

    r_archive = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}/archive"
    )
    assert r_archive.status_code == 409
    body = r_archive.json()
    # HTTPException 被全局处理器包装为 UserError 协议
    # error_code 为 UNKNOWN_ERROR（409 未在白名单中），但 technical_details 保留 detail
    tech = body.get("technical_details") or {}
    err_msg = tech.get("error_message") or ""
    # detail 是 MemberHasPositionErrorSchema.model_dump() 转字符串，应包含关键信息
    assert "member_has_position" in err_msg or "持仓" in err_msg


# ----------------------------------------------------------------------------
# 9. POST archive force=True 强制归档
# ----------------------------------------------------------------------------


def test_archive_portfolio_member_force(client, db_session):
    """【WP4.4】force=True 即使有持仓也强制归档，且不删除持仓。"""
    pf = _make_portfolio(db_session, name="QA-API-Force-PF")
    sym = _make_symbol(db_session, symbol="610080")

    r_create = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym.id},
    )
    member_id = r_create.json()["id"]

    pos = _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )

    r_archive = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}/archive",
        json={"force": True},
    )
    assert r_archive.status_code == 200
    data = r_archive.json()
    assert data["status"] == "archived"
    assert data["effective_to"] is not None

    # 持仓不被删除（spec line 334："成员归档不会误删持仓"）
    db_session.expire_all()
    pos_after = db_session.get(Position, pos.id)
    assert pos_after is not None
    assert pos_after.quantity == 100.0


# ----------------------------------------------------------------------------
# 10. POST pause
# ----------------------------------------------------------------------------


def test_pause_portfolio_member(client, db_session):
    """【WP4.4】POST /pause 设置 status='paused'，effective_to 仍为 None。"""
    pf = _make_portfolio(db_session, name="QA-API-Pause-PF")
    sym = _make_symbol(db_session, symbol="610090")

    r_create = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym.id},
    )
    member_id = r_create.json()["id"]

    r_pause = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}/pause"
    )
    assert r_pause.status_code == 200
    data = r_pause.json()
    assert data["status"] == "paused"
    # pause 不归档：effective_to 仍为 None
    assert data["effective_to"] is None


# ----------------------------------------------------------------------------
# 11. POST restore
# ----------------------------------------------------------------------------


def test_restore_portfolio_member(client, db_session):
    """【WP4.4】归档后 POST /restore 恢复 status='active'，effective_to=None。"""
    pf = _make_portfolio(db_session, name="QA-API-Restore-PF")
    sym = _make_symbol(db_session, symbol="610100")

    r_create = client.post(
        f"/api/v1/portfolios/{pf.id}/members",
        json={"symbol_id": sym.id},
    )
    member_id = r_create.json()["id"]

    # 归档
    r_arch = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}/archive"
    )
    assert r_arch.status_code == 200
    assert r_arch.json()["status"] == "archived"

    # 恢复
    r_restore = client.post(
        f"/api/v1/portfolios/{pf.id}/members/{member_id}/restore"
    )
    assert r_restore.status_code == 200
    data = r_restore.json()
    assert data["status"] == "active"
    assert data["effective_to"] is None
    assert data["id"] == member_id


# ----------------------------------------------------------------------------
# 12. POST backfill
# ----------------------------------------------------------------------------


def test_backfill_portfolio_members_creates_from_positions(client, db_session):
    """【WP4.4】POST /backfill 对持仓创建 legacy_position 成员。"""
    pf = _make_portfolio(db_session, name="QA-API-Backfill-PF")
    sym = _make_symbol(db_session, symbol="610110")

    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )

    resp = client.post(f"/api/v1/portfolios/{pf.id}/members/backfill")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_positions"] == 1
    assert data["created_members"] == 1
    assert data["skipped_existing"] == 0
    assert data["errors"] == []

    # 验证成员已创建
    r_list = client.get(f"/api/v1/portfolios/{pf.id}/members")
    items = r_list.json()
    assert len(items) == 1
    assert items[0]["source_type"] == "legacy_position"
    assert items[0]["status"] == "active"


# ----------------------------------------------------------------------------
# 13. POST backfill dry_run
# ----------------------------------------------------------------------------


def test_backfill_portfolio_members_dry_run(client, db_session):
    """【WP4.4】POST /backfill {"dry_run": true} 只统计不写入。"""
    pf = _make_portfolio(db_session, name="QA-API-BackfillDryRun-PF")
    sym = _make_symbol(db_session, symbol="610120")

    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )

    resp = client.post(
        f"/api/v1/portfolios/{pf.id}/members/backfill",
        json={"dry_run": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_positions"] == 1
    # dry_run 模式下 created_members 计数加 1 但实际未写入
    assert data["created_members"] == 1

    # 验证成员未实际创建
    r_list = client.get(f"/api/v1/portfolios/{pf.id}/members")
    items = r_list.json()
    assert len(items) == 0


# ----------------------------------------------------------------------------
# 14. 404 错误
# ----------------------------------------------------------------------------


def test_patch_portfolio_member_not_found_returns_404(client, db_session):
    """【WP4.4】PATCH 不存在的 member_id 返回 404。"""
    pf = _make_portfolio(db_session, name="QA-API-404-Patch-PF")

    resp = client.patch(
        f"/api/v1/portfolios/{pf.id}/members/99999",
        json={"priority": 5},
    )
    assert resp.status_code == 404
    body = resp.json()
    # 全局异常处理器将 HTTPException 包装为 UserError 协议
    assert body.get("error_code") == "NOT_FOUND"


def test_archive_portfolio_member_not_found_returns_404(client, db_session):
    """【WP4.4】POST /archive 不存在返回 404。"""
    pf = _make_portfolio(db_session, name="QA-API-404-Archive-PF")

    resp = client.post(
        f"/api/v1/portfolios/{pf.id}/members/99999/archive"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error_code") == "NOT_FOUND"


def test_pause_portfolio_member_not_found_returns_404(client, db_session):
    """【WP4.4】POST /pause 不存在返回 404。"""
    pf = _make_portfolio(db_session, name="QA-API-404-Pause-PF")

    resp = client.post(
        f"/api/v1/portfolios/{pf.id}/members/99999/pause"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error_code") == "NOT_FOUND"


def test_restore_portfolio_member_not_found_returns_404(client, db_session):
    """【WP4.4】POST /restore 不存在返回 404。"""
    pf = _make_portfolio(db_session, name="QA-API-404-Restore-PF")

    resp = client.post(
        f"/api/v1/portfolios/{pf.id}/members/99999/restore"
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error_code") == "NOT_FOUND"
