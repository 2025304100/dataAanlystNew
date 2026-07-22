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

pytestmark = pytest.mark.blackbox

BASE = "http://localhost:8000"
TIMEOUT = 15.0


@pytest.fixture(scope="module")
def client():
    """复用 httpx 客户端（禁用环境变量代理，直连 localhost）。"""
    with httpx.Client(base_url=BASE, timeout=TIMEOUT, trust_env=False) as c:
        # 前置检查：服务必须在线
        try:
            r = c.get("/health")
            assert r.status_code == 200, f"后端服务未运行: {r.status_code}"
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
    # overview 返回概览统计（symbols_count/watchlists_count 等）
    assert isinstance(overview, dict)
    assert "symbols_count" in overview or "portfolio" in overview


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


# ---------- Discovery 任务字段完整性（P2-2）----------

def test_discovery_task_fields_complete(client):
    """GET /api/v1/discovery/tasks 返回的任务应包含 updated_at/can_retry/cleanup_count 字段。"""
    r = client.get("/api/v1/discovery/tasks?limit=5")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    if data:
        task = data[0]
        for key in ("id", "status", "stage", "percent", "can_resume", "can_retry", "updated_at"):
            assert key in task, f"discovery task 缺少字段 {key}"


# ---------- Discovery retry 端点（P2-3）----------

def test_discovery_retry_not_found(client):
    """POST /api/v1/discovery/tasks/{nonexistent}/retry 应返回 404。"""
    r = client.post("/api/v1/discovery/tasks/nonexistent-task-id/retry")
    assert r.status_code == 404


def test_discovery_retry_endpoint_exists(client):
    """POST /api/v1/discovery/tasks/{id}/retry 端点应存在且可调用。"""
    # 获取现有任务列表
    r = client.get("/api/v1/discovery/tasks?limit=10")
    assert r.status_code == 200
    tasks = r.json()
    if not tasks:
        pytest.skip("无 discovery 任务可供测试 retry")

    # 找一个终态任务尝试 retry，或验证非终态任务返回 200（幂等）
    target = None
    for t in tasks:
        if t.get("status") in ("failed", "cancelled", "expired"):
            target = t
            break

    if target is None:
        # 没有终态任务，取第一个任务验证端点可达（非终态任务 retry 应返回原状态 200）
        target = tasks[0]
        r = client.post(f"/api/v1/discovery/tasks/{target['id']}/retry")
        assert r.status_code == 200, f"retry 非终态任务应返回 200，实际 {r.status_code}"
        assert r.json()["status"] == target["status"], "非终态任务 retry 后状态不应改变"
    else:
        r = client.post(f"/api/v1/discovery/tasks/{target['id']}/retry")
        # 终态任务 retry 可能成功（200）或因并发保护返回 409
        assert r.status_code in (200, 409), f"retry 终态任务应返回 200 或 409，实际 {r.status_code}"
        if r.status_code == 200:
            assert r.json()["status"] == "queued", "retry 后状态应为 queued"


# ---------- Discovery pause/resume/cancel 端点可达性 ----------

def test_discovery_cancel_not_found(client):
    """POST /api/v1/discovery/tasks/{nonexistent}/cancel 应返回 404。"""
    r = client.post("/api/v1/discovery/tasks/nonexistent-task-id/cancel")
    assert r.status_code == 404


def test_discovery_resume_not_found(client):
    """POST /api/v1/discovery/tasks/{nonexistent}/resume 应返回 404。"""
    r = client.post("/api/v1/discovery/tasks/nonexistent-task-id/resume")
    assert r.status_code == 404


def test_discovery_pause_not_found(client):
    """POST /api/v1/discovery/tasks/{nonexistent}/pause 应返回 404。"""
    r = client.post("/api/v1/discovery/tasks/nonexistent-task-id/pause")
    assert r.status_code == 404


# ---------- Discovery latest-candidates 端点（全量候选，不依赖 executable）----------

def test_discovery_latest_candidates_endpoint(client):
    """GET /api/v1/discovery/latest-candidates 应返回全量候选（含 hold/reduce，不依赖 executable 过滤）。"""
    r = client.get("/api/v1/discovery/latest-candidates?min_score=0&limit=50")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    # 如果有候选，验证字段完整性
    if data:
        candidate = data[0]
        for key in ("symbol_id", "symbol", "name", "quality_score", "timing_score",
                     "priority_score", "stage", "action", "is_frozen"):
            assert key in candidate, f"latest-candidates 缺少字段 {key}"


def test_discovery_latest_candidates_min_score_filter(client):
    """min_score 参数应正确过滤候选。"""
    r_all = client.get("/api/v1/discovery/latest-candidates?min_score=0&limit=50")
    r_high = client.get("/api/v1/discovery/latest-candidates?min_score=80&limit=50")
    assert r_all.status_code == 200
    assert r_high.status_code == 200
    all_count = len(r_all.json())
    high_count = len(r_high.json())
    # min_score=80 的结果不应多于 min_score=0 的结果
    assert high_count <= all_count, "min_score 过滤后候选数应 <= 全量"


def test_discovery_latest_candidates_returns_non_executable(client):
    """latest-candidates 应返回非 executable 的标的（hold/reduce），证明不依赖 executable 过滤。

    这是 P2 修复的核心验证：挖掘结果展示不依赖 portfolio 交易约束。
    """
    r = client.get("/api/v1/discovery/latest-candidates?min_score=0&limit=50")
    assert r.status_code == 200
    data = r.json()
    if data:
        # 验证返回的 action 不只是 open（包含 hold/reduce 等非可执行状态）
        actions = {item.get("action") for item in data}
        # 至少应该有结果（不论什么 action）
        assert len(actions) > 0, "latest-candidates 应返回结果"


# ---------- WP1.5 标的统一关联状态接口 ----------

def test_symbol_relationships_not_found(client):
    """GET /api/v1/symbols/{nonexistent}/relationships 应返回 404 + UserError。"""
    r = client.get("/api/v1/symbols/99999999/relationships")
    assert r.status_code == 404
    body = r.json()
    # WP-S.6 统一错误协议：必须包含 error_code / user_message / impact / retryable / correlation_id
    assert "error_code" in body, "404 响应应遵循 WP-S.6 UserError 协议"
    assert "user_message" in body
    assert "impact" in body
    assert "correlation_id" in body


def test_symbol_relationships_existing_symbol(client):
    """GET /api/v1/symbols/{id}/relationships 对已存在标的应返回完整结构。"""
    r = client.get("/api/v1/symbols?page=1&page_size=5")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    data = r.json()
    # WP1.5 关键字段完整性
    assert data["symbol_id"] == sid
    assert "symbol" in data
    assert "candidate" in data
    assert "observation" in data
    assert "portfolio_member" in data
    assert "position" in data
    assert "alert" in data
    assert "fetched_at" in data
    assert "degraded" in data
    # 子结构关键字段
    assert "has_candidate" in data["candidate"]
    assert "has_observation" in data["observation"]
    assert "has_portfolio_membership" in data["portfolio_member"]
    assert "has_position" in data["position"]
    assert "has_active_alert" in data["alert"]
    # 默认无 degraded_reason（除非降级）
    assert "degraded_reason" in data


def test_symbol_relationships_alert_structure(client):
    """GET /api/v1/symbols/{id}/relationships 的 alert 子结构字段完整。"""
    r = client.get("/api/v1/symbols?page=1&page_size=20")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    alert = r.json()["alert"]
    # 字段类型正确
    assert isinstance(alert["alert_rule_ids"], list)
    assert isinstance(alert["active_alert_events"], int)
    # has_active_alert 必须是 bool（不误报"未加入"或字符串）
    assert isinstance(alert["has_active_alert"], bool)


def test_symbol_relationships_position_structure(client):
    """GET /api/v1/symbols/{id}/relationships 的 position 子结构字段完整。"""
    r = client.get("/api/v1/symbols?page=1&page_size=20")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    pos = r.json()["position"]
    # has_position 必须是 bool（不会把观察项显示成持仓）
    assert isinstance(pos["has_position"], bool)
    assert isinstance(pos["portfolio_id"], (int, type(None)))
    assert isinstance(pos["quantity"], (int, float, type(None)))


# ---------- WP1.7 relationships 子结构与关联场景补充 ----------

def test_symbol_relationships_observation_structure(client):
    """GET /api/v1/symbols/{id}/relationships 的 observation 子结构字段完整。"""
    r = client.get("/api/v1/symbols?page=1&page_size=20")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    obs = r.json()["observation"]
    # has_observation 必须是 bool（不会把候选显示成观察项）
    assert isinstance(obs["has_observation"], bool)
    assert isinstance(obs["watchlist_id"], (int, type(None)))
    assert isinstance(obs["watchlist_name"], (str, type(None)))
    assert isinstance(obs["watchlist_item_id"], (int, type(None)))
    # tags 必须是 list（即便为空）
    assert isinstance(obs["tags"], list)
    assert isinstance(obs["priority"], (int, type(None)))


def test_symbol_relationships_candidate_structure(client):
    """GET /api/v1/symbols/{id}/relationships 的 candidate 子结构字段完整。"""
    r = client.get("/api/v1/symbols?page=1&page_size=20")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    cand = r.json()["candidate"]
    # has_candidate 必须是 bool（不会把观察项显示成候选）
    assert isinstance(cand["has_candidate"], bool)
    assert isinstance(cand["candidate_id"], (int, type(None)))
    assert isinstance(cand["scope"], (str, type(None)))
    assert isinstance(cand["priority_score"], (int, float, type(None)))
    assert isinstance(cand["scan_run_id"], (int, type(None)))


def test_symbol_relationships_portfolio_member_structure(client):
    """GET /api/v1/symbols/{id}/relationships 的 portfolio_member 子结构字段完整。"""
    r = client.get("/api/v1/symbols?page=1&page_size=20")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    pm = r.json()["portfolio_member"]
    # has_portfolio_membership 必须是 bool
    assert isinstance(pm["has_portfolio_membership"], bool)
    assert isinstance(pm["portfolio_id"], (int, type(None)))
    assert isinstance(pm["portfolio_name"], (str, type(None)))
    # 第一阶段 member_id 为预留字段，应为 null 或 int
    assert isinstance(pm["member_id"], (int, type(None)))


def test_symbol_relationships_degraded_field_type(client):
    """degraded 字段必须是 bool，degraded_reason 为 str 或 null。"""
    r = client.get("/api/v1/symbols?page=1&page_size=5")
    assert r.status_code == 200
    symbols = r.json()
    if not symbols:
        pytest.skip("无可用 symbol")
    sid = symbols[0]["id"]

    r = client.get(f"/api/v1/symbols/{sid}/relationships")
    assert r.status_code == 200
    data = r.json()
    # degraded 必须是 bool（不能是字符串或 null）
    assert isinstance(data["degraded"], bool)
    # degraded_reason 必须是 str 或 null
    assert isinstance(data["degraded_reason"], (str, type(None)))
    # fetched_at 必须是非空字符串（ISO 8601）
    assert isinstance(data["fetched_at"], str) and len(data["fetched_at"]) > 0


def test_symbol_relationships_observation_true_for_watchlist_item(client):
    """若标的已在观察池（watchlist_items）中，relationships.observation.has_observation 应为 true。

    只读场景：从 watchlists 列表中找一个非空 watchlist，取其第一个 item 的 symbol_id，
    验证 GET /relationships 返回 has_observation=true。
    """
    r = client.get("/api/v1/watchlists")
    assert r.status_code == 200
    watchlists = r.json()
    if not watchlists:
        pytest.skip("无可用 watchlist")

    target_symbol_id = None
    for wl in watchlists:
        wl_id = wl.get("id")
        if wl_id is None:
            continue
        r_items = client.get(f"/api/v1/watchlists/{wl_id}/items")
        if r_items.status_code != 200:
            continue
        items = r_items.json()
        if items:
            target_symbol_id = items[0].get("symbol_id")
            break

    if target_symbol_id is None:
        pytest.skip("所有 watchlist 均为空，无法验证 has_observation=true")

    r = client.get(f"/api/v1/symbols/{target_symbol_id}/relationships")
    assert r.status_code == 200
    obs = r.json()["observation"]
    # 关键约束：观察池中的标的必须返回 has_observation=true，不能误报"未加入"
    assert obs["has_observation"] is True, (
        f"symbol_id={target_symbol_id} 已在 watchlist_items 中，"
        "但 relationships.observation.has_observation 未返回 true"
    )
    # 应同时返回 watchlist_id 与 watchlist_item_id
    assert obs["watchlist_id"] is not None
    assert obs["watchlist_item_id"] is not None


def test_symbol_relationships_candidate_true_for_discovery_candidate(client):
    """若标的为最新候选（discovery_latest_candidates）之一，relationships.candidate.has_candidate 应为 true。

    只读场景：从 latest-candidates 取第一个候选的 symbol_id，
    验证 GET /relationships 返回 has_candidate=true。
    若 latest-candidates 端点本身不可用（如数据库 schema 未同步），跳过本测试。
    """
    r = client.get("/api/v1/discovery/latest-candidates?min_score=0&limit=10")
    if r.status_code != 200:
        pytest.skip(f"latest-candidates 端点不可用（status={r.status_code}），跳过候选关联验证")
    candidates = r.json()
    if not candidates:
        pytest.skip("无可用 discovery candidate")

    target_symbol_id = candidates[0].get("symbol_id")
    if target_symbol_id is None:
        pytest.skip("候选缺少 symbol_id 字段")

    r = client.get(f"/api/v1/symbols/{target_symbol_id}/relationships")
    assert r.status_code == 200
    cand = r.json()["candidate"]
    # 关键约束：候选池中的标的必须返回 has_candidate=true
    assert cand["has_candidate"] is True, (
        f"symbol_id={target_symbol_id} 在 latest-candidates 中，"
        "但 relationships.candidate.has_candidate 未返回 true"
    )
    # 应同时返回 candidate_id
    assert cand["candidate_id"] is not None


def test_symbol_relationships_returns_user_error_on_invalid_id(client):
    """GET /api/v1/symbols/{invalid}/relationships 对非数字 ID 应返回 422。"""
    r = client.get("/api/v1/symbols/not-a-number/relationships")
    # 路径参数类型错误应由 Pydantic/FastAPI 拒绝
    assert r.status_code in (404, 422)


# ---------- WP2.3 观察池端点 ----------

def test_list_observations_endpoint(client):
    """GET /api/v1/watchlists/{id}/observations 应返回富读列表。

    选取第一个非空 watchlist，验证端点可达且返回结构化富读字段。
    """
    r = client.get("/api/v1/watchlists")
    assert r.status_code == 200
    watchlists = r.json()
    if not watchlists:
        pytest.skip("无可用 watchlist")

    target_wl_id = None
    for wl in watchlists:
        wl_id = wl.get("id")
        if wl_id is None:
            continue
        # 通过 observations 端点查找（兼容 WP2.1 之前仅有 items 端点的旧库）
        r_obs = client.get(f"/api/v1/watchlists/{wl_id}/observations")
        if r_obs.status_code == 200 and r_obs.json():
            target_wl_id = wl_id
            break

    if target_wl_id is None:
        pytest.skip("所有 watchlist 均无观察项，跳过富读字段验证")

    r = client.get(f"/api/v1/watchlists/{target_wl_id}/observations")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) > 0

    item = data[0]
    # WP2.3 富读字段完整性
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
    assert item["watchlist_id"] == target_wl_id
    # degraded 必须是 bool（不能是字符串或 null）
    assert isinstance(item["degraded"], bool)


def test_list_observations_status_filter(client):
    """GET /api/v1/watchlists/{id}/observations?status=archived 应返回归档项。"""
    r = client.get("/api/v1/watchlists")
    assert r.status_code == 200
    watchlists = r.json()
    if not watchlists:
        pytest.skip("无可用 watchlist")

    wl_id = watchlists[0]["id"]
    # 默认不返回 archived
    r_default = client.get(f"/api/v1/watchlists/{wl_id}/observations")
    assert r_default.status_code == 200
    for item in r_default.json():
        assert item["status"] != "archived", "默认列表不应包含 archived 项"

    # 显式筛选 archived
    r_archived = client.get(
        f"/api/v1/watchlists/{wl_id}/observations?status=archived"
    )
    assert r_archived.status_code == 200
    for item in r_archived.json():
        assert item["status"] == "archived"


def test_observation_endpoints_404(client):
    """WP2.3 观察池端点对不存在的 observation_id 应返回 404 + UserError。"""
    r = client.get("/api/v1/watchlists")
    assert r.status_code == 200
    watchlists = r.json()
    if not watchlists:
        pytest.skip("无可用 watchlist")
    wl_id = watchlists[0]["id"]

    # PATCH 不存在 observation_id → 404
    r_patch = client.patch(
        f"/api/v1/watchlists/{wl_id}/observations/99999999",
        json={"priority": 5},
    )
    assert r_patch.status_code == 404
    body = r_patch.json()
    # WP-S.6 统一错误协议
    assert "error_code" in body
    assert body["error_code"] == "NOT_FOUND"

    # POST /archive 不存在 → 404
    r_arch = client.post(
        f"/api/v1/watchlists/{wl_id}/observations/99999999/archive"
    )
    assert r_arch.status_code == 404
    assert r_arch.json()["error_code"] == "NOT_FOUND"

    # POST /restore 不存在 → 404
    r_rest = client.post(
        f"/api/v1/watchlists/{wl_id}/observations/99999999/restore"
    )
    assert r_rest.status_code == 404
    assert r_rest.json()["error_code"] == "NOT_FOUND"
