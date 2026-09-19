"""白盒测试 - 行情数据批量修复接口。

覆盖：
- POST /api/v1/market-data/repair/batch 正常批量修复
- 部分失败时返回正确的 ok/empty/failed/missing 计数
- 空 symbol_ids 列表校验失败（422）
- 与设置页单标同步使用同源 sync_symbol_daily_bars 服务

测试用 SQLite 内存库（db_session fixture）+ FastAPI TestClient + get_db 依赖覆盖。
不依赖真实 akshare 网络请求：mock sync_symbol_daily_bars。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.symbol import Symbol

pytestmark = pytest.mark.whitebox


def _make_symbol(db_session, symbol: str = "600000", asset_type: str = "stock") -> Symbol:
    s = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type=asset_type,
        market="sh",
        is_active=1,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


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
    app.dependency_overrides.pop(get_db, None)


def test_repair_batch_market_data_success(client, db_session):
    """批量修复 3 只标的，全部成功。"""
    sym1 = _make_symbol(db_session, "600001")
    sym2 = _make_symbol(db_session, "600002")
    sym3 = _make_symbol(db_session, "600003")

    def _fake_sync(*, db, symbol, start_date, end_date, adjust):
        return {
            "symbol_id": symbol.id,
            "symbol": symbol.symbol,
            "asset_type": symbol.asset_type,
            "status": "ok",
            "inserted": 10,
            "updated": 0,
            "rows": 10,
            "start_date": str(start_date or date.today()),
            "end_date": str(end_date or date.today()),
        }

    with patch("app.api.routes.market_data.sync_symbol_daily_bars", side_effect=_fake_sync):
        resp = client.post(
            "/api/v1/market-data/repair/batch",
            json={"symbol_ids": [sym1.id, sym2.id, sym3.id], "adjust": "qfq", "auto_score": False},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["total"] == 3
    assert data["ok_count"] == 3
    assert data["empty_count"] == 0
    assert data["failed_count"] == 0
    assert data["missing_count"] == 0
    assert data["missing_ids"] == []


def test_repair_batch_market_data_partial_failure(client, db_session):
    """批量修复混合结果：1 成功、1 空、1 失败、1 不存在 symbol_id。"""
    sym1 = _make_symbol(db_session, "600004")
    sym2 = _make_symbol(db_session, "600005")
    sym3 = _make_symbol(db_session, "600006")

    def _fake_sync(*, db, symbol, start_date, end_date, adjust):
        if symbol.symbol == "600004":
            return {"symbol_id": symbol.id, "symbol": symbol.symbol, "asset_type": symbol.asset_type, "status": "ok", "inserted": 5, "updated": 0, "rows": 5, "start_date": "2026-01-01", "end_date": "2026-07-24"}
        if symbol.symbol == "600005":
            return {"symbol_id": symbol.id, "symbol": symbol.symbol, "asset_type": symbol.asset_type, "status": "empty", "inserted": 0, "updated": 0, "rows": 0, "start_date": "2026-01-01", "end_date": "2026-07-24"}
        return {"symbol_id": symbol.id, "symbol": symbol.symbol, "asset_type": symbol.asset_type, "status": "network_error", "inserted": 0, "updated": 0, "rows": 0, "start_date": "2026-01-01", "end_date": "2026-07-24"}

    with patch("app.api.routes.market_data.sync_symbol_daily_bars", side_effect=_fake_sync):
        resp = client.post(
            "/api/v1/market-data/repair/batch",
            json={"symbol_ids": [sym1.id, sym2.id, sym3.id, 99999], "adjust": "qfq", "auto_score": False},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False
    assert data["total"] == 4
    assert data["ok_count"] == 1
    assert data["empty_count"] == 1
    assert data["failed_count"] == 1
    assert data["missing_count"] == 1
    assert data["missing_ids"] == [99999]
    assert len(data["failed_symbols"]) == 1
    assert data["failed_symbols"][0]["symbol"] == "600006"


def test_repair_batch_market_data_empty_symbol_ids(client):
    """symbol_ids 为空列表时应返回 422 校验错误。"""
    resp = client.post(
        "/api/v1/market-data/repair/batch",
        json={"symbol_ids": [], "adjust": "qfq"},
    )
    assert resp.status_code == 422


def test_repair_batch_uses_same_sync_function_as_market_data_update(client, db_session):
    """确认批量修复与 /market-data/update 使用同源的 sync_symbol_daily_bars。"""
    sym = _make_symbol(db_session, "600007")

    called = []

    def _fake_sync(*, db, symbol, start_date, end_date, adjust):
        called.append(symbol.symbol)
        return {"symbol_id": symbol.id, "symbol": symbol.symbol, "asset_type": symbol.asset_type, "status": "ok", "inserted": 1, "updated": 0, "rows": 1, "start_date": "2026-01-01", "end_date": "2026-07-24"}

    with patch("app.api.routes.market_data.sync_symbol_daily_bars", side_effect=_fake_sync):
        resp = client.post(
            "/api/v1/market-data/repair/batch",
            json={"symbol_ids": [sym.id], "adjust": "qfq", "auto_score": False},
        )

    assert resp.status_code == 200
    assert called == ["600007"]
