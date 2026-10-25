"""E2E 测试 - 接口管理流程（P2-2）。

完整链路验证接口管理：
- 设置 Tab → 接口管理区域 → 接口列表（与注册表数量一致）→ 单接口探测 → 批量探测。

E2E 测试需要前后端同时运行：
    cd d:/ai_project/dataAanlystNew && python -m uvicorn app.main:app --port 8000
    cd d:/ai_project/dataAanlystNew/frontend && npm run dev
"""
from __future__ import annotations

import pytest
import httpx
import os

from tests._e2e_settle import settle
from tests._e2e_navigation import click_named
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# i18n 文本（zh-CN / en）。
SETTINGS_TAB_LABELS = ("设置", "Settings")
API_MGMT_NAV_LABELS = ("接口管理", "API Management")
API_MGMT_SECTION_TITLE_LABELS = ("第三方接口状态", "Third-party API Status")
REFRESH_LABELS = ("刷新", "Refresh")
PROBE_ALL_LABELS = ("批量探测", "Probe All")
PROBE_LABELS = ("探测", "Probe")

# The registry is intentionally extensible.  The UI contract is that it
# renders every API returned by the live registry, rather than a stale count.
BACKEND_URL = os.getenv("E2E_BACKEND_URL", "http://localhost:8000").rstrip("/")


def _click_view_tab(page, labels):
    return click_named(page.locator("nav.view-tabs"), labels)


def _click_settings_nav(page, labels):
    return click_named(page.locator(".settings-sidebar"), labels)


def _goto_api_mgmt(page):
    """切换到 设置 → 接口管理。"""
    _click_view_tab(page, SETTINGS_TAB_LABELS)
    _click_settings_nav(page, API_MGMT_NAV_LABELS)
    expect(page.locator("[data-settings-content='settings-api-mgmt']")).to_be_visible(timeout=20000)
    expect(page.locator("[data-settings-content='settings-api-mgmt'] .ant-table-row").first).to_be_visible(timeout=20000)


def test_api_mgmt_tab_visible(page):
    """【P2-2 E2E】切换到「设置」Tab，接口管理区域可见。"""
    _goto_api_mgmt(page)

    # api-mgmt 内容容器可见
    content = page.locator("[data-settings-content='settings-api-mgmt']").first
    assert content.is_visible(), "未找到接口管理内容容器 settings-api-mgmt"

    # Card 标题含「第三方接口状态」
    title = None
    for label in API_MGMT_SECTION_TITLE_LABELS:
        candidate = page.locator(f".ant-card-head-title:has-text('{label}')").first
        if candidate.is_visible():
            title = candidate
            break
    assert title is not None, "未找到接口管理 Card 标题「第三方接口状态」"


def test_api_list_loaded(page):
    """【P2-2 E2E】接口列表完整加载并与后端注册表一致。"""
    _goto_api_mgmt(page)

    # 等待表格行加载
    rows = page.locator("[data-settings-content='settings-api-mgmt'] .ant-table-row")
    # 给表格加载留出时间
    page.wait_for_timeout(1000)
    count = rows.count()
    response = httpx.get(
        f"{BACKEND_URL}/api/v1/external-data/apis",
        params={"locale": "zh-CN"},
        timeout=10.0,
        trust_env=False,
    )
    assert response.status_code == 200, (
        f"接口注册表请求失败：HTTP {response.status_code}: {response.text[:300]}"
    )
    registry = response.json()
    assert isinstance(registry, list) and registry, "接口注册表为空"
    assert count == len(registry), (
        f"接口列表应展示注册表全部 {len(registry)} 项，实际为 {count}"
    )


def test_single_probe_button_clickable(page):
    """【P2-2 E2E】单接口探测按钮可点击。

    每行操作列含「探测」按钮（apiMgmtProbe）。
    """
    _goto_api_mgmt(page)
    page.wait_for_timeout(1000)

    rows = page.locator("[data-settings-content='settings-api-mgmt'] .ant-table-row")
    if rows.count() == 0:
        pytest.skip("接口列表未加载，跳过单接口探测按钮验证")

    # 第一行内的「探测」按钮
    first_row = rows.first
    probe_btn = None
    for label in PROBE_LABELS:
        candidate = first_row.locator(f"button:has-text('{label}')").first
        if candidate.is_visible():
            probe_btn = candidate
            break
    assert probe_btn is not None, "未在第一行找到「探测」按钮"
    # 可点击（enabled）即视为通过；不实际触发，避免触发真实网络请求
    assert probe_btn.is_enabled(), "「探测」按钮不可点击"


def test_batch_probe_button_clickable(page):
    """【P2-2 E2E】批量探测按钮可点击。

    Card extra 区域含「批量探测」按钮（apiMgmtProbeAll）。
    """
    _goto_api_mgmt(page)

    # Card extra 内的「批量探测」按钮
    probe_all_btn = None
    for label in PROBE_ALL_LABELS:
        candidate = page.locator(
            f"[data-settings-content='settings-api-mgmt'] button:has-text('{label}')"
        ).first
        if candidate.is_visible():
            probe_all_btn = candidate
            break
    assert probe_all_btn is not None, "未找到「批量探测」按钮"
    assert probe_all_btn.is_enabled(), "「批量探测」按钮不可点击"
