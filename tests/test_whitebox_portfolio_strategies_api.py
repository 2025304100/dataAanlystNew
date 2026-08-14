"""白盒测试 - P0 组合交易大改造：策略开关 API。

覆盖：
  1. GET  /portfolios/{id}/strategies            返回 4 条策略（兜底 seed）
  2. PATCH /portfolios/{id}/strategies/{key}     切换 enabled 成功
  3. PATCH /portfolios/{id}/strategies/{key}     更新 params_json 成功
  4. PATCH 空请求体（不改动字段）                返回 200 且数据未变
  5. PATCH 非法 strategy_key（=bullshit）        返回 400
  6. PATCH params_json 非合法 JSON               返回 422
  7. GET / PATCH 不存在的 portfolio_id           返回 404
  8. 新建组合（POST /portfolios）后立即 GET strategies → 应有 4 条 seed
  9. position / portfolio_member target_weight_pct 字段 ORM 可读

运行：
  pytest tests/test_whitebox_portfolio_strategies_api.py -v -s
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.router import api_router  # noqa: F401  (保证路由已注册)
from app.db.session import get_db
from app.models.portfolio import AutoTradeStrategy, Portfolio, Position


pytestmark = pytest.mark.whitebox


@pytest.fixture()
def client(db_session):
    from app.main import app

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _make_pf(db_session, name="QA-Strategies-PF", account_type="cash"):
    pf = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=200000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


# ---------------------------------------------------------------------------
# 1. GET /portfolios/{id}/strategies 返回 4 条
# ---------------------------------------------------------------------------


def test_list_portfolio_strategies_returns_four(client, db_session):
    """【P0】GET /portfolios/{id}/strategies → 4 条策略 + 正确默认启用状态。"""
    pf = _make_pf(db_session)
    # 注意：此处直接 ORM 创建，没有走 create_portfolio 路由，因此没有 seed。
    # 但 GET API 内部有兜底 seed，应返回 4 条。

    resp = client.get(f"/api/v1/portfolios/{pf.id}/strategies")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 4, f"期望 4 条，实际 {len(data)} 条：{data}"

    keys = {d["strategy_key"] for d in data}
    assert keys == {"ma_track", "momentum", "grid", "tp_sl"}, keys

    # 默认止盈止损 enabled=1，其他 0
    by_key = {d["strategy_key"]: d for d in data}
    assert by_key["tp_sl"]["enabled"] == 1
    assert by_key["ma_track"]["enabled"] == 0
    assert by_key["momentum"]["enabled"] == 0
    assert by_key["grid"]["enabled"] == 0

    # 兜底 seed 之后 DB 里真的落库了 4 条
    count = db_session.query(AutoTradeStrategy).filter(
        AutoTradeStrategy.portfolio_id == pf.id
    ).count()
    assert count == 4, f"DB 里应有 4 条，实际 {count}"


# ---------------------------------------------------------------------------
# 2. PATCH enabled 切换
# ---------------------------------------------------------------------------


def test_patch_strategy_enable_toggle(client, db_session):
    """【P0】PATCH /portfolios/{id}/strategies/ma_track 切换开关 + 落库。"""
    pf = _make_pf(db_session, name="QA-Toggle-PF")

    # 先 GET 一次兜底 seed
    client.get(f"/api/v1/portfolios/{pf.id}/strategies")

    # 启用均线
    resp = client.patch(
        f"/api/v1/portfolios/{pf.id}/strategies/ma_track",
        json={"enabled": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["strategy_key"] == "ma_track"
    assert body["enabled"] == 1

    # 数据库值正确
    s = db_session.query(AutoTradeStrategy).filter(
        AutoTradeStrategy.portfolio_id == pf.id,
        AutoTradeStrategy.strategy_key == "ma_track",
    ).one()
    assert s.enabled == 1

    # 再关闭
    resp2 = client.patch(
        f"/api/v1/portfolios/{pf.id}/strategies/ma_track",
        json={"enabled": False},
    )
    assert resp2.status_code == 200
    db_session.refresh(s)
    assert s.enabled == 0


# ---------------------------------------------------------------------------
# 3. PATCH params_json 更新
# ---------------------------------------------------------------------------


def test_patch_strategy_params_json(client, db_session):
    """【P0】PATCH params_json 更新参数 + JSON 校验。"""
    pf = _make_pf(db_session, name="QA-Params-PF")
    client.get(f"/api/v1/portfolios/{pf.id}/strategies")

    new_params = '{"grid_lines": 20, "upper_price_pct": 15, "lower_price_pct": 12}'
    resp = client.patch(
        f"/api/v1/portfolios/{pf.id}/strategies/grid",
        json={"params_json": new_params},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["params_json"] == new_params
    assert json.loads(body["params_json"])["grid_lines"] == 20


# ---------------------------------------------------------------------------
# 4. PATCH 空 body 不报错且不改动
# ---------------------------------------------------------------------------


def test_patch_empty_body_noop(client, db_session):
    """【P0】PATCH 空 payload（无任何字段）→ 200，数据不变。"""
    pf = _make_pf(db_session, name="QA-Noop-PF")
    client.get(f"/api/v1/portfolios/{pf.id}/strategies")

    before = db_session.query(AutoTradeStrategy).filter(
        AutoTradeStrategy.portfolio_id == pf.id,
        AutoTradeStrategy.strategy_key == "tp_sl",
    ).one()
    orig_updated = before.updated_at

    resp = client.patch(
        f"/api/v1/portfolios/{pf.id}/strategies/tp_sl",
        json={},
    )
    assert resp.status_code == 200, resp.text

    db_session.refresh(before)
    # tp_sl 默认启用，PATCH 空后仍启用
    assert before.enabled == 1
    # updated_at 未必变化（因为没改），这里只验证字段稳定
    assert resp.json()["enabled"] == 1


# ---------------------------------------------------------------------------
# 5. PATCH 非法 strategy_key → 400
# ---------------------------------------------------------------------------


def test_patch_invalid_strategy_key_400(client, db_session):
    """【P0】PATCH 非法 strategy_key → 400 + 错误信息。"""
    pf = _make_pf(db_session, name="QA-400-PF")

    resp = client.patch(
        f"/api/v1/portfolios/{pf.id}/strategies/bullshit",
        json={"enabled": True},
    )
    assert resp.status_code == 400, resp.text
    # 兼容：FastAPI 默认 {"detail":...} / 自定义 unified format technical_details.error_message
    payload = resp.json()
    td = payload.get("technical_details") or {}
    detail_text = (
        td.get("error_message")
        or payload.get("detail")
        or payload.get("message")
        or payload.get("user_message")
        or json.dumps(payload, ensure_ascii=False)
    )
    assert "Invalid strategy_key" in detail_text or "strategy_key" in detail_text, (
        f"响应中未识别到错误描述，实际 payload: {payload}"
    )


# ---------------------------------------------------------------------------
# 6. PATCH params_json 非合法 JSON → 422
# ---------------------------------------------------------------------------


def test_patch_bad_params_json_422(client, db_session):
    """【P0】PATCH params_json 是非法 JSON 字符串 → 422。"""
    pf = _make_pf(db_session, name="QA-422-PF")
    client.get(f"/api/v1/portfolios/{pf.id}/strategies")

    resp = client.patch(
        f"/api/v1/portfolios/{pf.id}/strategies/momentum",
        json={"params_json": "{not valid json}"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# 7. GET/PATCH 不存在 portfolio_id → 404
# ---------------------------------------------------------------------------


def test_portfolio_not_found_404(client):
    """【P0】GET/PATCH 不存在组合 → 404。"""
    r1 = client.get("/api/v1/portfolios/9999999/strategies")
    assert r1.status_code == 404, r1.text

    r2 = client.patch(
        "/api/v1/portfolios/9999999/strategies/ma_track",
        json={"enabled": True},
    )
    assert r2.status_code == 404, r2.text


# ---------------------------------------------------------------------------
# 8. POST 创建组合后 GET strategies → 立即可见 4 条（create_portfolio 内 seed）
# ---------------------------------------------------------------------------


def test_create_portfolio_then_get_strategies(client, db_session):
    """【P0】POST /portfolios → 立即 GET strategies 有 4 条 seed。"""
    # 先造个不重名的
    import random
    suffix = random.randint(10000, 99999)
    resp = client.post(
        "/api/v1/portfolios",
        json={
            "name": f"QA-Create-Seed-{suffix}",
            "account_type": "cash",
            "total_capital": 500000.0,
            "investable_ratio": 0.7,
            "cash_reserve_ratio": 0.3,
            "currency": "CNY",
            "is_default": False,
            "auto_trade_enabled": False,
        },
    )
    assert resp.status_code == 200, resp.text
    pf_id = resp.json()["id"]

    resp2 = client.get(f"/api/v1/portfolios/{pf_id}/strategies")
    assert resp2.status_code == 200, resp2.text
    assert len(resp2.json()) == 4


# ---------------------------------------------------------------------------
# 9. target_weight_pct ORM 可读（positions & portfolio_members）
# ---------------------------------------------------------------------------


def test_target_weight_pct_readable_on_models(db_session):
    """【P0】Position / PortfolioMember 模型 target_weight_pct / deviation_pct 字段可达。"""
    from app.models.portfolio_member import EXECUTION_MANUAL, SOURCE_MANUAL, PortfolioMember
    from app.models.symbol import Symbol

    pf = _make_pf(db_session, name="QA-Model-Field-PF")
    sym = Symbol(symbol="000999", name="字段可达测试", asset_type="stock", market="cn")
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)

    pos = Position(
        portfolio_id=pf.id, symbol_id=sym.id, quantity=1000,
        avg_cost=10.0, latest_price=11.0, market_value=11000.0,
        position_pct=50.0, asset_type="stock",
        target_weight_pct=40.0,  # 新字段
    )
    db_session.add(pos)

    pm = PortfolioMember(
        portfolio_id=pf.id, symbol_id=sym.id,
        status="active", execution_mode=EXECUTION_MANUAL,
        source_type=SOURCE_MANUAL, source_id=None,
        note="字段验证",
        target_weight_pct=40.0,
        deviation_pct=10.0,   # 实际-目标=50-40=10
    )
    db_session.add(pm)
    db_session.commit()
    db_session.refresh(pos)
    db_session.refresh(pm)

    assert pos.target_weight_pct == pytest.approx(40.0)
    assert pm.target_weight_pct == pytest.approx(40.0)
    assert pm.deviation_pct == pytest.approx(10.0)
