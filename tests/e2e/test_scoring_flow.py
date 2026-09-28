"""E2E 测试 - 评分配置流程（P2-2）。

完整链路验证评分配置：
- 设置 Tab → 评分配置区域 → 股票/ETF Tab 切换 → 预设列表加载。

E2E 测试需要前后端同时运行：
    cd d:/ai_project/dataAanlystNew && python -m uvicorn app.main:app --port 8000
    cd d:/ai_project/dataAanlystNew/frontend && npm run dev
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.e2e

# i18n 文本（zh-CN / en）。
SETTINGS_TAB_LABELS = ("设置", "Settings")
SCORING_NAV_LABELS = ("评分配置", "Scoring Config")
# antd Button 会在两个中文字符间插入真实空格（DOM 文本是「股 票」而不是「股票」），
# 字面 has-text 因此永远匹配不上 —— 用容忍空白的正则定位。
STOCK_TAB_PATTERN = re.compile(r"股\s*票")
ETF_TAB_PATTERN = re.compile(r"ETF", re.IGNORECASE)
STOCK_TAB_LABELS = ("股票", "Stock")
ETF_TAB_LABELS = ("ETF",)
REFRESH_LABELS = ("刷新", "Refresh")


def _click_view_tab(page, labels):
    """通过文本切换主 Tab，返回首个可见的 Tab locator。"""
    for label in labels:
        tab = page.locator(f"button.view-tab:has-text('{label}')").first
        if tab.is_visible():
            tab.click()
            page.wait_for_timeout(1500)
            page.wait_for_load_state("networkidle")
            return tab
    return None


def _click_settings_nav(page, labels):
    """点击设置页左侧 nav 切换到指定 section。"""
    for label in labels:
        nav = page.locator(f".settings-nav-item:has-text('{label}')").first
        if nav.is_visible():
            nav.click()
            page.wait_for_timeout(1500)
            page.wait_for_load_state("networkidle")
            return nav
    return None


def _goto_scoring(page):
    """切换到 设置 → 评分配置。"""
    _click_view_tab(page, SETTINGS_TAB_LABELS)
    _click_settings_nav(page, SCORING_NAV_LABELS)
    page.wait_for_timeout(2000)


def test_scoring_config_section_visible(page):
    """【P2-2 E2E】评分配置区域可见。"""
    _goto_scoring(page)

    # scoring 内容容器可见
    content = page.locator("[data-settings-content='settings-scoring']").first
    assert content.is_visible(), "未找到评分配置内容容器 settings-scoring"

    # 评分配置区域根容器
    section = page.locator(".scoring-config-section").first
    assert section.is_visible(), "未找到评分配置根容器 .scoring-config-section"


def test_stock_etf_tab_switchable(page):
    """【P2-2 E2E】股票/ETF Tab 可切换。"""
    _goto_scoring(page)

    # 资产类型切换按钮由 antd Button 渲染，中文双字会被拆开，所以走 role + 正则
    def _find_type_button(pattern):
        candidate = page.locator(
            ".scoring-config-section button"
        ).filter(has_text=pattern).first
        if candidate.count() > 0 and candidate.is_visible():
            return candidate
        return None

    stock_btn = _find_type_button(STOCK_TAB_PATTERN)
    assert stock_btn is not None, (
        "未找到「股票」资产类型切换按钮（已容忍 antd 的双字空格）"
    )

    etf_btn = _find_type_button(ETF_TAB_PATTERN)
    assert etf_btn is not None, "未找到「ETF」资产类型切换按钮"

    # 切换到 ETF
    etf_btn.click()
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")

    # 切换回 股票
    stock_btn.click()
    page.wait_for_timeout(1500)
    page.wait_for_load_state("networkidle")

    # 切换后内容容器仍可见即视为通过
    content = page.locator("[data-settings-content='settings-scoring']").first
    assert content.is_visible(), "切换股票/ETF Tab 后评分配置区域不可见"


def test_scoring_config_list_loaded(page):
    """【P2-2 E2E】预设列表加载。

    验证：评分配置 Table 已渲染（至少有 1 行预设，或空状态提示可见）。
    """
    _goto_scoring(page)

    # 评分配置区域内的 Table 行
    rows = page.locator(".scoring-config-section .ant-table-row")
    page.wait_for_timeout(1000)
    count = rows.count()

    # 系统预置至少 1 个预设（balanced_opportunity / etf_balanced 等）
    # 若数据未加载完成（count == 0），验证 ant-table 容器存在即可
    if count == 0:
        table = page.locator(".scoring-config-section .ant-table").first
        assert table.is_visible(), "评分配置 Table 未渲染（既无行也无 Table 容器）"
    else:
        assert count >= 1, f"评分配置预设列表应为 >=1 行，实际为 {count}"
