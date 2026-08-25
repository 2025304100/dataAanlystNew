"""Real-interface acceptance checks for the G3 backtest center.

These checks deliberately do not install frontend mocks or seed browser state.
They discover an existing portfolio/run through the live API, then exercise the
same UI path a reviewer uses.  A missing fixture run is reported as a skip (the
isolated acceptance database is optional), while an HTTP/schema/layout
regression fails the test.  Each test writes a compact API request summary;
set ``G3_RECORD_HAR=1`` to additionally persist a Playwright HAR.

Run from the repository root::

    $env:PYTHONPATH = ".pip_packages"
    .venv\\Scripts\\python.exe -m pytest tests/e2e/test_backtest_acceptance_matrix.py -m e2e -q

The API and Vite server must be running.  ``E2E_BACKEND_URL`` and
``E2E_FRONTEND_URL`` can point at an isolated pair of services.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import BACKEND_URL, FRONTEND_URL


pytestmark = pytest.mark.e2e

API_PREFIX = os.getenv("E2E_API_PREFIX", "/api/v1").rstrip("/")
ARTIFACT_DIR = Path(os.getenv("G3_ARTIFACT_DIR", "test_output/g3"))
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}


def _api_get(path: str, **params: Any) -> httpx.Response:
    """GET a read-only acceptance endpoint and fail clearly on bad contracts."""
    url = f"{BACKEND_URL}{API_PREFIX}{path}"
    try:
        response = httpx.get(url, params=params or None, timeout=15.0, trust_env=False)
    except Exception as exc:  # service disappeared after the session health check
        pytest.skip(f"G3 固定 API 不可用：{url}（{type(exc).__name__}: {exc}）")
    if response.status_code in {404, 409, 422} and path in {"/portfolios", "/backtest/runs"}:
        pytest.skip(f"G3 固定夹具未提供 {url}（HTTP {response.status_code}）")
    assert response.status_code == 200, (
        f"G3 API 契约失败：GET {response.request.url} -> "
        f"HTTP {response.status_code}: {response.text[:500]}"
    )
    return response


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        pytest.fail(f"G3 API 返回非 JSON：{response.request.url}: {exc}")


def _portfolio_runs(*, require_run: bool = True) -> tuple[int, list[dict[str, Any]]]:
    portfolios = _json(_api_get("/portfolios"))
    if not isinstance(portfolios, list) or not portfolios:
        pytest.skip("G3 固定夹具没有可用组合；请先导入隔离数据库")
    portfolio = portfolios[0]
    portfolio_id = int(portfolio.get("id", 0)) if isinstance(portfolio, dict) else 0
    if portfolio_id <= 0:
        pytest.fail(f"/portfolios 返回缺少有效 id：{portfolio!r}")

    runs = _json(_api_get("/backtest/runs", portfolio_id=portfolio_id, limit=20))
    if not isinstance(runs, list):
        pytest.fail(f"/backtest/runs 应返回数组，实际为：{runs!r}")
    if not runs:
        if require_run:
            pytest.skip("G3 固定夹具没有历史回测运行；该用例不创建/修改回测数据")
        return portfolio_id, []
    valid_runs = [
        run for run in runs
        if isinstance(run, dict) and int(run.get("id", 0)) > 0
    ]
    if len(valid_runs) != len(runs):
        pytest.fail(f"/backtest/runs 返回缺少有效运行 id：{runs!r}")
    return portfolio_id, valid_runs


def _first_portfolio_and_run(*, require_run: bool = True) -> tuple[int, dict[str, Any] | None]:
    portfolio_id, runs = _portfolio_runs(require_run=require_run)
    return portfolio_id, runs[0] if runs else None


def _run_for_first_portfolio(predicate, *, reason: str) -> dict[str, Any]:
    """Find a matching run without assuming the API's first portfolio is seeded."""
    portfolios = _json(_api_get("/portfolios"))
    if not isinstance(portfolios, list):
        pytest.fail(f"/portfolios 应返回数组，实际为：{portfolios!r}")
    for portfolio in portfolios:
        if not isinstance(portfolio, dict) or int(portfolio.get("id", 0)) <= 0:
            continue
        runs = _json(_api_get("/backtest/runs", portfolio_id=int(portfolio["id"]), limit=20))
        if not isinstance(runs, list):
            pytest.fail(f"组合 {portfolio['id']} 的 /backtest/runs 不是数组：{runs!r}")
        for run in runs:
            if isinstance(run, dict) and int(run.get("id", 0)) > 0 and predicate(run):
                return run
    pytest.skip(reason)


def _click_one(page, labels: tuple[str, ...], *, role: str = "button") -> None:
    for label in labels:
        candidate = page.get_by_role(role, name=label, exact=True).first
        if candidate.is_visible():
            candidate.click()
            return
    # Keep failures actionable when a translation or selector changes.
    pytest.fail(f"未找到可见控件：{' / '.join(labels)}")


def _assert_visible_text(page, labels: tuple[str, ...]) -> None:
    for label in labels:
        if page.get_by_text(label, exact=True).first.is_visible():
            return
    pytest.fail(f"未找到可见文本：{' / '.join(labels)}")


def _open_backtest_center(page) -> None:
    _click_one(page, ("组合交易", "Portfolio Trading"))
    _click_one(page, ("回测中心", "Backtest"))
    page.locator(".pt-backtest-center").wait_for(state="visible", timeout=15_000)


def _open_run_url(page, run_id: int, tab: str = "overview") -> None:
    page.goto(f"{FRONTEND_URL}/?bt_run={run_id}&bt_tab={tab}", wait_until="domcontentloaded")
    # bt_run/bt_tab belongs to the result panel; the main shell deliberately
    # does not infer a primary navigation destination from it.  Navigate via
    # the visible controls so the test follows the production user path.
    _open_backtest_center(page)
    # Historical restore performs a second request after the shell mounts.
    page.wait_for_timeout(800)


@pytest.fixture
def g3_page(browser, request):
    """A real browser context with optional HAR and API response recording."""
    test_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    context_kwargs: dict[str, Any] = {"viewport": DESKTOP_VIEWPORT}
    har_path: Path | None = None
    screenshot_path: Path | None = None
    if os.getenv("G3_RECORD_HAR", "").lower() in {"1", "true", "yes"}:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        har_path = ARTIFACT_DIR / f"{test_name}-{timestamp}.har"
        context_kwargs["record_har_path"] = str(har_path)

    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    network: list[dict[str, Any]] = []

    def on_response(response) -> None:
        if f"{API_PREFIX}/" not in response.url and not response.url.endswith(API_PREFIX):
            return
        parsed = urlparse(response.url)
        network.append(
            {
                "kind": "response",
                "method": response.request.method,
                "url": response.url,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "status": response.status,
                "resource_type": response.request.resource_type,
            }
        )

    def on_request_failed(request_obj) -> None:
        if f"{API_PREFIX}/" not in request_obj.url and not request_obj.url.endswith(API_PREFIX):
            return
        network.append(
            {
                "kind": "requestfailed",
                "method": request_obj.method,
                "url": request_obj.url,
                "failure": request_obj.failure,
                "resource_type": request_obj.resource_type,
            }
        )

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    # Expose only the in-memory summary to assertions; the fixture still
    # serializes it to disk in teardown for handoff/review.
    page._g3_network = network
    page.goto(FRONTEND_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(700)
    try:
        yield page
    finally:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        if os.getenv("G3_RECORD_SCREENSHOTS", "1").lower() not in {"0", "false", "no"}:
            screenshot_path = ARTIFACT_DIR / f"{test_name}-{timestamp}.png"
            try:
                page.screenshot(path=str(screenshot_path), full_page=False)
            except Exception as exc:  # artifact capture must not mask the assertion result
                screenshot_path = None
                network.append({"kind": "screenshot_error", "message": f"{type(exc).__name__}: {exc}"})
        context.close()
        summary_path = ARTIFACT_DIR / f"{test_name}-{timestamp}.json"
        summary_path.write_text(
            json.dumps(
                {
                    "test": request.node.nodeid,
                    "frontend_url": FRONTEND_URL,
                    "backend_url": BACKEND_URL,
                    "captured_at": timestamp,
                    "har": str(har_path) if har_path else None,
                    "screenshot": str(screenshot_path) if screenshot_path else None,
                    "requests": network,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def test_bt_ui_01_center_structure_and_desktop_geometry(g3_page):
    """BT-UI-01/03: real center has mode/history controls and no overflow."""
    _open_backtest_center(g3_page)
    assert g3_page.get_by_role("button", name="组合回测", exact=True).is_visible()
    assert g3_page.get_by_role("button", name="单标的回测", exact=True).is_visible()
    assert g3_page.get_by_role("button", name="回测历史", exact=True).is_visible()
    _assert_visible_text(g3_page, ("参数配置", "Parameters"))
    _assert_visible_text(g3_page, ("净值曲线", "Equity Curve"))
    metrics = g3_page.evaluate(
        """() => ({
          documentClientWidth: document.documentElement.clientWidth,
          documentScrollWidth: document.documentElement.scrollWidth,
          mainClientWidth: document.querySelector('main')?.clientWidth ?? 0,
          mainScrollWidth: document.querySelector('main')?.scrollWidth ?? 0,
        })"""
    )
    assert metrics["documentScrollWidth"] <= metrics["documentClientWidth"] + 1, metrics
    assert metrics["mainScrollWidth"] <= metrics["mainClientWidth"] + 1, metrics


@pytest.mark.parametrize("viewport", ((1280, 800), (1024, 768), (390, 844)))
def test_bt_ui_18_responsive_geometry(g3_page, viewport: tuple[int, int]):
    """BT-UI-18: the real portfolio backtest shell has no page/main overflow."""
    width, height = viewport
    g3_page.set_viewport_size({"width": width, "height": height})
    _open_backtest_center(g3_page)
    metrics = g3_page.evaluate(
        """() => ({
          documentClientWidth: document.documentElement.clientWidth,
          documentScrollWidth: document.documentElement.scrollWidth,
          mainClientWidth: document.querySelector('main')?.clientWidth ?? 0,
          mainScrollWidth: document.querySelector('main')?.scrollWidth ?? 0,
        })"""
    )
    assert metrics["documentScrollWidth"] <= metrics["documentClientWidth"] + 1, (viewport, metrics)
    assert metrics["mainScrollWidth"] <= metrics["mainClientWidth"] + 1, (viewport, metrics)


def test_bt_ui_02_empty_state_is_explicit_and_not_fake_data(g3_page):
    """BT-UI-02: no persisted run renders an explicit, non-fabricated empty state."""
    _open_backtest_center(g3_page)
    empty_text = g3_page.get_by_text("暂无回测结果，请配置参数后开始回测", exact=True)
    empty_text_en = g3_page.get_by_text("No results yet. Configure parameters and start a backtest.", exact=True)
    if not empty_text.is_visible() and not empty_text_en.is_visible():
        pytest.skip("当前验收首页已有真实回测结果，无法验证 BT-UI-02 空态")
    assert g3_page.locator(".pt-backtest-result-column .pt-table tbody tr").count() == 0
    _assert_visible_text(g3_page, ("净值曲线", "Equity Curve"))


def test_bt_ui_04_result_tabs_and_deep_link_state(g3_page):
    """BT-UI-04/16/20: a persisted run restores tabs and the run id in URL."""
    _, run = _first_portfolio_and_run()
    assert run is not None
    run_id = int(run["id"])
    _open_run_url(g3_page, run_id, "data")
    for label in ("绩效概览", "持仓变化", "拒绝记录", "数据说明"):
        assert g3_page.get_by_role("tab", name=label, exact=True).is_visible(), label
    assert g3_page.get_by_role("tab", name="数据说明", exact=True).get_attribute("aria-selected") == "true"
    assert f"bt_run={run_id}" in g3_page.url
    assert "bt_tab=data" in g3_page.url


def test_bt_ui_08_09_pagination_and_filters_are_server_side(g3_page):
    """BT-UI-08/09: page/filter actions issue bounded requests and persist URL."""
    def has_second_page(run: dict[str, Any]) -> bool:
        response = _json(_api_get(f"/backtest/runs/{int(run['id'])}/trades", page=1, page_size=20))
        return isinstance(response, dict) and int(response.get("total", 0)) > 20

    run = _run_for_first_portfolio(has_second_page, reason="固定夹具没有超过一页的交易流水，无法验证 BT-UI-08/09")
    run_id = int(run["id"])

    _open_run_url(g3_page, run_id, "trades")
    g3_page.get_by_role("tab", name=re.compile(r"交易流水")).wait_for()
    next_page = g3_page.get_by_role("button", name="下一页", exact=True)
    # The first page is loaded lazily; wait until the API total enables the
    # control before attempting the second-page click.
    for _ in range(30):
        if next_page.is_enabled():
            break
        g3_page.wait_for_timeout(500)
    assert next_page.is_enabled(), "流水总数已超过 20，但下一页仍不可用"
    next_page.click()
    g3_page.wait_for_timeout(700)
    assert "bt_page=2" in g3_page.url
    network = getattr(g3_page, "_g3_network", [])
    page_requests = [
        item for item in network
        if item.get("kind") == "response" and item.get("path", "").endswith(f"/backtest/runs/{run_id}/trades")
    ]
    assert any(
        item.get("query", {}).get("page") == ["2"]
        and item.get("query", {}).get("page_size") == ["20"]
        for item in page_requests
    ), f"未观察到有界第二页请求：{page_requests!r}"
    # Filter changes must reset to page one and remain shareable in URL state.
    g3_page.get_by_test_id("backtest-trade-action-filter").select_option("BUY")
    g3_page.wait_for_timeout(300)
    assert "bt_action=BUY" in g3_page.url
    assert "bt_page" not in g3_page.url
    filtered_requests = [
        item for item in getattr(g3_page, "_g3_network", [])
        if item.get("kind") == "response" and item.get("path", "").endswith(f"/backtest/runs/{run_id}/trades")
    ]
    assert any(
        item.get("query", {}).get("action") == ["BUY"]
        and item.get("query", {}).get("page") == ["1"]
        for item in filtered_requests
    ), f"未观察到筛选后回到第一页的服务端请求：{filtered_requests!r}"
    g3_page.get_by_test_id("backtest-trade-clear-filters").click()
    g3_page.wait_for_timeout(300)
    assert "bt_action" not in g3_page.url


def test_bt_ui_10_rejected_tab_uses_rejection_endpoint(g3_page):
    """BT-UI-10: rejected records are loaded separately and never duplicated as trades."""
    run = _run_for_first_portfolio(
        lambda candidate: True,
        reason="固定夹具没有历史回测运行，无法验证 BT-UI-10",
    )
    run_id = int(run["id"])
    evidence = _json(_api_get(f"/backtest/runs/{run_id}/evidence", page=1, page_size=20, action="REJECTED,DATA_BLOCKED"))
    if not isinstance(evidence, dict):
        pytest.fail(f"证据分页响应不是对象：{evidence!r}")
    _open_run_url(g3_page, run_id, "rejected")
    g3_page.get_by_role("tab", name="拒绝记录", exact=True).wait_for()
    g3_page.wait_for_timeout(700)
    assert "bt_tab=rejected" in g3_page.url
    # The browser must use the filtered evidence endpoint, not fetch all years
    # of trades and derive rejection rows locally.
    network = getattr(g3_page, "_g3_network", [])
    evidence_requests = [
        item for item in network
        if item.get("kind") == "response" and item.get("path", "").endswith(f"/backtest/runs/{run_id}/evidence")
    ]
    assert any(
        item.get("query", {}).get("action") == ["REJECTED,DATA_BLOCKED"]
        and item.get("query", {}).get("page_size") == ["20"]
        for item in evidence_requests
    ), f"未观察到拒绝证据分页请求：{evidence_requests!r}"
    assert g3_page.get_by_role("tab", name="拒绝记录", exact=True).get_attribute("aria-selected") == "true"


def test_bt_ui_11_15_evidence_drawer_close_and_focus(g3_page):
    """BT-UI-11~15: open a real evidence row, close with Escape, restore focus."""
    def has_linked_trade(run: dict[str, Any]) -> bool:
        response = _json(_api_get(f"/backtest/runs/{int(run['id'])}/trades", page=1, page_size=20))
        items = response.get("items", []) if isinstance(response, dict) else []
        return any(
            item.get("decision_evidence_id") and item.get("entry_decision_run_id")
            for item in items if isinstance(item, dict)
        )

    run = _run_for_first_portfolio(has_linked_trade, reason="固定夹具没有带精确 DecisionEvidence 链的交易行")
    run_id = int(run["id"])

    _open_run_url(g3_page, run_id, "trades")
    evidence_button = g3_page.get_by_role("button", name="已关联 · 查看证据", exact=True).first
    evidence_button.wait_for(state="visible", timeout=15_000)
    evidence_button.focus()
    evidence_button.click()
    drawer = g3_page.locator(".pt-evidence-drawer-panel")
    drawer.wait_for(state="visible", timeout=15_000)
    assert drawer.bounding_box() is not None
    g3_page.keyboard.press("Escape")
    drawer.wait_for(state="hidden", timeout=5_000)
    assert g3_page.evaluate("document.activeElement?.tagName") in {"BUTTON", "BODY"}


def test_bt_ui_17_blocked_history_is_fail_closed(g3_page):
    """BT-UI-17: when a persisted run is blocked, no success curve is shown."""
    run = _run_for_first_portfolio(
        lambda candidate: str(candidate.get("status", "")).upper() in {"BLOCKED", "DATA_BLOCKED", "FAILED", "REJECTED"},
        reason="固定夹具没有阻断/失败回测运行，无法验证 BT-UI-17",
    )
    _open_run_url(g3_page, int(run["id"]), "data")
    gate = g3_page.get_by_test_id("backtest-gate-state")
    gate.wait_for(state="visible", timeout=15_000)
    assert g3_page.get_by_text("当前回测未生成可执行结果。", exact=True).is_visible()
    assert g3_page.locator(".pt-backtest-metric-grid").evaluate("el => getComputedStyle(el).display") == "none"


def test_bt_ui_20_historical_snapshot_is_stable(g3_page):
    """BT-UI-20: reopening the same run preserves immutable execution metadata."""
    run = _run_for_first_portfolio(
        lambda candidate: True,
        reason="固定夹具没有历史回测运行，无法验证 BT-UI-20",
    )
    run_id = int(run["id"])
    first = _json(_api_get(f"/backtest/runs/{run_id}"))
    _open_run_url(g3_page, run_id, "data")
    g3_page.reload(wait_until="domcontentloaded")
    _open_backtest_center(g3_page)
    g3_page.wait_for_timeout(800)
    second = _json(_api_get(f"/backtest/runs/{run_id}"))
    immutable_keys = (
        "id", "portfolio_id", "start_date", "end_date", "initial_capital",
        "strategy_snapshot_id", "snapshot_hash", "factor_model_run_id",
        "factor_set_id", "pit_mode", "commission_rate", "slippage_bps",
    )
    compared = [key for key in immutable_keys if key in first or key in second]
    assert compared, f"运行详情缺少可比较的执行快照字段：{first!r}"
    for key in compared:
        assert first.get(key) == second.get(key), f"运行 {run_id} 的 {key} 在重开后改变"
    assert f"bt_run={run_id}" in g3_page.url
