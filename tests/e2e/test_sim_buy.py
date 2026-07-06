"""E2E 测试 - 模拟买入流程（P2-1，从根目录 test_sim_buy.py 迁移）。

验证模拟交易买入流程：切换 Tab → 搜索标的 → 填写数量/价格 → 点击买入。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_navigate_to_trading_tab(page):
    """【P2-1 E2E】可切换到「目前观察池 → 模拟交易」子 Tab。"""
    # 切换到目前观察池
    portfolio_tab = page.locator("button.view-tab:has-text('目前观察池')").first
    if portfolio_tab.is_visible():
        portfolio_tab.click()
        page.wait_for_timeout(1000)

    # 切换到模拟交易子 Tab
    trading_tab = page.locator("button.sub-tab:has-text('模拟交易')").first
    if trading_tab.is_visible():
        trading_tab.click()
        page.wait_for_timeout(2000)

    # 验证订单面板可见（#tradingDetailTitle 或 #simBuyButton）
    detail_title = page.locator("#tradingDetailTitle").first
    buy_btn = page.locator("#simBuyButton").first
    assert detail_title.is_visible() or buy_btn.is_visible(), (
        "模拟交易子 Tab 切换后未看到订单面板"
    )


def test_sim_buy_form_visible(page):
    """【P2-1 E2E】模拟交易表单含数量/价格输入框和买入按钮。"""
    # 确保在模拟交易 Tab
    portfolio_tab = page.locator("button.view-tab:has-text('目前观察池')").first
    if portfolio_tab.is_visible():
        portfolio_tab.click()
        page.wait_for_timeout(500)
    trading_tab = page.locator("button.sub-tab:has-text('模拟交易')").first
    if trading_tab.is_visible():
        trading_tab.click()
        page.wait_for_timeout(1500)

    qty_input = page.locator("#simQuantityInput").first
    price_input = page.locator("#simPriceInput").first
    buy_btn = page.locator("#simBuyButton").first

    assert qty_input.is_visible(), "未找到数量输入框 #simQuantityInput"
    assert price_input.is_visible(), "未找到价格输入框 #simPriceInput"
    assert buy_btn.is_visible(), "未找到买入按钮 #simBuyButton"


def test_sim_buy_submit_does_not_throw_page_error(page):
    """【P2-1 E2E】填写表单并点击买入按钮不抛 page error。

    注意：本测试不验证买入成功（依赖持仓数据），仅验证无 JS 异常。
    """
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    # 切换到模拟交易
    portfolio_tab = page.locator("button.view-tab:has-text('目前观察池')").first
    if portfolio_tab.is_visible():
        portfolio_tab.click()
        page.wait_for_timeout(500)
    trading_tab = page.locator("button.sub-tab:has-text('模拟交易')").first
    if trading_tab.is_visible():
        trading_tab.click()
        page.wait_for_timeout(1500)

    # 填写数量和价格（如为空）
    qty_input = page.locator("#simQuantityInput").first
    price_input = page.locator("#simPriceInput").first
    if qty_input.is_visible() and not qty_input.input_value():
        qty_input.fill("100")
    if price_input.is_visible() and not price_input.input_value():
        price_input.fill("10.00")
    page.wait_for_timeout(500)

    # 关闭可能拦截点击的 Modal
    modal_close = page.locator(".ant-modal-close, button[aria-label='Close']").first
    if modal_close.is_visible():
        modal_close.click()
        page.wait_for_timeout(500)

    # 点击买入
    buy_btn = page.locator("#simBuyButton").first
    if buy_btn.is_visible():
        buy_btn.click()
        page.wait_for_timeout(3000)

    # 验证无 page error
    assert len(page_errors) == 0, f"模拟买入流程出现 page error: {page_errors}"
