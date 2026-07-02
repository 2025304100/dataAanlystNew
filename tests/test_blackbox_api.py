"""黑盒测试 - API 端到端。

不依赖后端内部实现，仅通过 HTTP 接口验证行为。
前置：后端服务运行在 http://localhost:8000。

覆盖：
- 健康检查
- 核心业务端点（dashboard/portfolios/symbols/discovery/macro/news）
- 字段完整性（响应应包含 schema 要求字段）
- 错误处理（不存在的资源、非法参数）
- 边界值（分页、空结果）
"""
from __future__ import annotations

import json
import pytest
import httpx

BASE = "http://localhost:8000"
TIMEOUT = 15.0


@pytest.fixture(scope="module")
def client():
    """复用 httpx 客户端。"""
    with httpx.Client(base_url=BASE, timeout=TIMEOUT) as c:
        # 前置检查：服务必须在线
        try:
            r = c.get("/health")
            assert r.status_code == 200, "后端服务未运行，请先启动后端"
        except Exception as e:
            pytest.skip(f"后端服务未运行（{e}），跳过黑盒测试")
        yield c


# ---------- 健康检查 ----------

def test_health_endpoint(client):
    """/health 应返回 200 + {"status": "ok"}。"""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------- 核心只读端点 ----------

def test_list_portfolios(client):
    """GET /api/v1/portfolios 应返回列表。"""
    r = client.get("/api/v1/portfolios")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    if data:
        p = data[0]
        # 字段完整性
        for key in ("id", "name", "total_capital", "investable_ratio"):
            assert key in p, f"portfolio 缺少字段 {key}"


def test_dashboard_overview(client):
    """GET /api/v1/dashboard/overview 应返回概览。"""
    r = client.get("/api/v1/portfolios")
    portfolios = r.json()
    if not portfolios:
        pytest.skip("无可用 portfolio")
    pid = portfolios[0]["id"]

    r = client.get(f"/api/v1/dashboard/overview?portfolio_id={pid}")
    assert r.status_code == 200
    overview = r.json()
    assert "portfolio" in overview


def test_dashboard_workbench(client):
    """工作台聚合接口应返回完整结构。"""
    r = client.get("/api/v1/portfolios")
    portfolios = r.json()
    if not portfolios:
        pytest.skip("无可用 portfolio")
    pid = portfolios[0]["id"]

    r = client.get(f"/api/v1/dashboard/workbench?portfolio_id={pid}&market_group=all")
    assert r.status_code == 200
    wb = r.json()
    # 验证关键聚合字段
    assert "portfolio" in wb
    # latest_scores 应为列表
    if "latest_scores" in wb:
        assert isinstance(wb["latest_scores"], list)


def test_list_symbols_pagination(client):
    """GET /api/v1/symbols 应支持分页。"""
    r = client.get("/api/v1/symbols?page=1&page_size=5")
    assert r.status_code == 200
    data = r.json()
    # 应为列表，且不超过 page_size
    if isinstance(data, list):
        assert len(data) <= 5


def test_watchlists(client):
    """GET /api/v1/watchlists 应返回列表。"""
    r = client.get("/api/v1/watchlists")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_discovery_tasks_list(client):
    """GET /api/v1/discovery/tasks 应返回列表。"""
    r = client.get("/api/v1/discovery/tasks")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_macro_overview(client):
    """GET /api/v1/macro/overview?region=cn 应返回宏观概览。"""
    r = client.get("/api/v1/macro/overview?region=cn")
    assert r.status_code == 200
    data = r.json()
    assert "region" in data or isinstance(data, dict)


def test_news_latest(client):
    """GET /api/v1/news/latest 应返回新闻列表。"""
    r = client.get("/api/v1/portfolios")
    portfolios = r.json()
    pid = portfolios[0]["id"] if portfolios else 1
    r = client.get(f"/api/v1/news/latest?portfolio_id={pid}&limit=5")
    assert r.status_code == 200


# ---------- 错误处理 ----------

def test_invalid_portfolio_id_returns_404(client):
    """不存在的 portfolio_id 应返回 4xx。"""
    r = client.get("/api/v1/dashboard/overview?portfolio_id=99999999")
    assert r.status_code in (400, 404, 422)


def test_missing_required_param_returns_422(client):
    """缺少必填参数应返回 422。"""
    # dashboard/overview 必须有 portfolio_id
    r = client.get("/api/v1/dashboard/overview")
    assert r.status_code == 422


def test_invalid_query_param_returns_422(client):
    """非法 query 参数类型应被 Pydantic 拒绝。"""
    r = client.get("/api/v1/symbols?page=abc")
    assert r.status_code == 422


# ---------- 边界值 ----------

def test_symbols_large_page(client):
    """[边界] 极大页码应返回空列表而非错误。"""
    r = client.get("/api/v1/symbols?page=99999&page_size=10")
    assert r.status_code == 200
    data = r.json()
    if isinstance(data, list):
        assert len(data) == 0


def test_symbols_zero_page_size(client):
    """[边界] page_size=0 应被拒绝或返回空。"""
    r = client.get("/api/v1/symbols?page=1&page_size=0")
    # 0 在多数实现里要么 422 要么返回空
    assert r.status_code in (200, 422)


# ---------- 异步任务端点 ----------

def test_get_async_task_not_found(client):
    """GET 不存在的异步任务应返回 404。"""
    r = client.get("/api/v1/market-data/sync/nonexistent-task-id")
    assert r.status_code in (404, 422, 400)


# ---------- 系统健康 ----------

def test_data_health_endpoint(client):
    """GET /api/v1/system/data-health 应返回数据健康信息。"""
    r = client.get("/api/v1/system/data-health")
    # 该端点可能需要 portfolio_id 也可能不需要
    if r.status_code == 422:
        # 需要 portfolio_id
        r2 = client.get("/api/v1/portfolios")
        portfolios = r2.json()
        if portfolios:
            r = client.get(f"/api/v1/system/data-health?portfolio_id={portfolios[0]['id']}")
    assert r.status_code in (200, 404), f"data-health 端点异常: {r.status_code}"


# ---------- 响应格式验证 ----------

def test_api_endpoints_return_json(client):
    """所有 /api/v1 端点应返回 JSON 而非 HTML。"""
    endpoints = [
        "/api/v1/portfolios",
        "/api/v1/discovery/tasks",
        "/api/v1/watchlists",
    ]
    for ep in endpoints:
        r = client.get(ep)
        assert r.headers.get("content-type", "").startswith("application/json"), \
            f"{ep} 应返回 JSON，实际 {r.headers.get('content-type')}"
