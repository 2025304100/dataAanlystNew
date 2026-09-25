"""DEF-4/DEF-8 回归 · 面板缓存 + 数仓忙降级（2026-09-24 全面测试报告）。

覆盖：
1. `load_screening_panel` 进程级缓存：同键命中不重算、异键各算各、
   `clear_screening_panel_cache()` 失效、TTL=0 过期重算；
2. `preview_unavailable` 降级工厂：阻断语义（WAREHOUSE_UNAVAILABLE）而非 500；
3. 路由层：DuckDB IOException → 200 降级（presets `available=False` /
   preview `can_generate=False`），不再 500。
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.services.factors.candidate_pool import presets as P
from app.services.factors.candidate_pool import rules as R

pytestmark = pytest.mark.whitebox


# ══════════════════════════════════════════════════════════
# 1. 面板缓存
# ══════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _clean_panel_cache():
    R.clear_screening_panel_cache()
    yield
    R.clear_screening_panel_cache()


def _patch_panel_loader(monkeypatch, calls: dict):
    sentinel = object()

    def _load(db, rules, *, as_of_date, warehouse=None):
        calls["n"] += 1
        calls.setdefault("dates", []).append(as_of_date)
        return sentinel

    monkeypatch.setattr(R, "_load_screening_panel_uncached", _load)
    return sentinel


def test_panel_cache_same_key_loaded_once(db_session, monkeypatch):
    """哨兵 1：同 (markets, window, loss_years, as_of_date) 只装配一次。"""
    calls: dict = {"n": 0}
    _patch_panel_loader(monkeypatch, calls)
    rules = R.CompiledRules()

    p1 = R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))
    p2 = R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))

    assert p1 is p2
    assert calls["n"] == 1, "同键第二次调用必须命中缓存"


def test_panel_cache_different_key_reloads(db_session, monkeypatch):
    """哨兵 2：不同 as_of_date / 规则参数 → 各自装配（缓存键隔离）。"""
    calls: dict = {"n": 0}
    _patch_panel_loader(monkeypatch, calls)
    rules = R.CompiledRules()

    R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))
    R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 2))
    R.load_screening_panel(db_session, R.CompiledRules(liquidity_window=10),
                           as_of_date=date(2026, 9, 1))

    assert calls["n"] == 3


def test_panel_cache_clear_forces_reload(db_session, monkeypatch):
    """哨兵 3：clear_screening_panel_cache() 后强制重算（镜像补数用）。"""
    calls: dict = {"n": 0}
    _patch_panel_loader(monkeypatch, calls)
    rules = R.CompiledRules()

    R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))
    assert R.clear_screening_panel_cache() >= 1
    R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))

    assert calls["n"] == 2


def test_panel_cache_ttl_expiry(db_session, monkeypatch):
    """哨兵 4：TTL 过期（这里置 0）→ 重算。"""
    calls: dict = {"n": 0}
    _patch_panel_loader(monkeypatch, calls)
    monkeypatch.setattr(R, "PANEL_CACHE_TTL_SECONDS", 0)
    rules = R.CompiledRules()

    R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))
    R.load_screening_panel(db_session, rules, as_of_date=date(2026, 9, 1))

    assert calls["n"] == 2


# ══════════════════════════════════════════════════════════
# 2. 降级工厂 + 路由降级
# ══════════════════════════════════════════════════════════


def test_preview_unavailable_shape():
    result = R.preview_unavailable(
        reason_zh="数据仓库忙，请稍后重试。",
        filter_config={"market_cap": {"total_market_cap": {"min": 0}}},
    )
    assert result.can_generate is False
    assert result.blocking_issues[0]["reason"] == R.REASON_WAREHOUSE_UNAVAILABLE
    d = result.to_dict()
    assert d["can_generate"] is False
    assert d["hits"] == 0
    assert d["blocking_issues"][0]["detail_zh"] == "数据仓库忙，请稍后重试。"


def _client(db_session) -> TestClient:
    from app.api.routes import mining_candidate_pool as MP

    app = FastAPI()
    app.include_router(MP.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_preview_route_degrades_on_duckdb_busy(db_session, monkeypatch):
    """哨兵 5：preview 撞 DuckDB IOException → 200 + 阻断语义（禁止 500）。"""
    def _busy(*a, **kw):
        raise duckdb.IOException("File is already open in another process")

    monkeypatch.setattr(
        "app.api.routes.mining_candidate_pool.pool_rules.preview_filter", _busy)
    resp = _client(db_session).post("/factor-mining/candidate-pools/preview",
                                    json={"filter_config": {}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_generate"] is False
    assert body["blocking_issues"][0]["reason"] == R.REASON_WAREHOUSE_UNAVAILABLE
    assert "稍后重试" in body["blocking_issues"][0]["detail_zh"]


def test_presets_route_degrades_on_duckdb_busy(db_session, monkeypatch):
    """哨兵 6：presets 撞 DuckDB IOException → 200 + available=False（禁止 500）。"""
    def _busy(*a, **kw):
        raise duckdb.IOException("File is already open in another process")

    monkeypatch.setattr(
        "app.api.routes.mining_candidate_pool.pool_presets.get_filter_presets", _busy)
    resp = _client(db_session).get("/factor-mining/candidate-pools/filter-presets")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert "稍后重试" in body["reason_zh"]
    assert body["presets"] == []
