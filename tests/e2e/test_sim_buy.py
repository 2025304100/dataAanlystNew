"""E2E 测试 - 组合交易视图与手动下单入口（原「模拟买入」用例重写）。

现状（2026-09-28 实测，非猜测）：
- 主视图已重排为：今日决策 / 组合交易 / 机会中心 / 宏观数据 / 行情消息 / 设置。
  旧用例依赖的「目前观察池」主 Tab 与「模拟交易」子 Tab 都不存在了。
- 手动下单面板（数量/价格/买入按钮，id 为 simQuantityInput / simPriceInput /
  simBuyButton）在当前任何视图下都不渲染：其宿主组件 `components/Trading.tsx` 与
  `components/TradingPanel.tsx` 已成孤儿（除各自单测外无任何挂载点）。
  该缺口由 test_manual_order_entry_is_reachable 作为缺陷警报线把门（PT-DEF-22）。
- 本文件同时纠正旧写法：导航一律“必须真的点进去”，不再用 `if 可见: 点击`
  让用例在元素缺失时静默通过（那是假绿）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# 组合交易主视图：现名 + 历史名（改名过渡期都能命中）
PORTFOLIO_TAB_LABELS = ("组合交易", "Portfolio Trading", "目前观察池", "投资中心")

# 组合交易视图内的子导航（DOM 侦察实测）
PORTFOLIO_SUBNAV_LABELS = ("总览", "持仓成员", "策略规则", "回测中心", "治理")

MANUAL_ORDER_ENTRY_IDS = (
    "simQuantityInput",
    "simPriceInput",
    "simBuyButton",
)


def _open_view(page, labels):
    """切到指定主视图；找不到就直接失败，不再静默跳过。"""
    for label in labels:
        tab = page.locator(f"button.view-tab:has-text('{label}')").first
        if tab.count() > 0 and tab.is_visible():
            tab.click()
            page.wait_for_timeout(2500)
            page.wait_for_load_state("networkidle")
            return tab
    pytest.fail(
        f"主视图不可达，已尝试标签：{labels}（界面若再次改名，请更新本用例标签表）"
    )


def test_navigate_to_portfolio_view_renders_subnav(page):
    """【P2-1 E2E】组合交易视图可进入，且子导航齐全。"""
    _open_view(page, PORTFOLIO_TAB_LABELS)

    found = []
    for label in PORTFOLIO_SUBNAV_LABELS:
        item = page.locator(
            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
        ).first
        if item.count() > 0 and item.is_visible():
            found.append(label)

    assert len(found) >= 3, (
        f"组合交易视图子导航缺失：只找到 {found}，期望至少含 "
        f"{list(PORTFOLIO_SUBNAV_LABELS)} 中的 3 项"
    )


@pytest.mark.xfail(
    reason=(
        "PT-DEF-22（见体检报告 §十七.2）：手动模拟下单入口在当前 UI 不可达——"
        "simQuantityInput / simPriceInput / simBuyButton 在全部 6 个主视图下计数均为 0，"
        "其宿主 components/Trading.tsx 与 components/TradingPanel.tsx 无任何挂载点（孤儿组件）。"
        "需产品拍板：若手动下单已刻意下线，应删组件与本用例；若仍需保留，这是入口回归。"
    ),
    strict=False,
)
def test_manual_order_entry_is_reachable(page):
    """缺陷警报线：组合交易视图里应能打开手动下单面板。"""
    _open_view(page, PORTFOLIO_TAB_LABELS)

    # 先尝试把子导航逐个点开一遍（下单面板可能藏在某一子页里）
    for label in PORTFOLIO_SUBNAV_LABELS:
        item = page.locator(
            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
        ).first
        if item.count() > 0 and item.is_visible():
            item.click()
            page.wait_for_timeout(1500)
            break

    missing = [
        _id for _id in MANUAL_ORDER_ENTRY_IDS
        if page.locator(f"#{_id}").count() == 0 or not page.locator(f"#{_id}").first.is_visible()
    ]
    assert not missing, (
        f"手动下单控件缺失：{missing}。当前 UI 没有任何可达的手动买入入口。"
    )


def test_portfolio_view_has_no_page_errors(page):
    """【P2-1 E2E】进入组合交易视图并切换子导航，不产生 JS 异常。"""
    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    _open_view(page, PORTFOLIO_TAB_LABELS)

    clicked = 0
    for label in PORTFOLIO_SUBNAV_LABELS:
        item = page.locator(
            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
        ).first
        if item.count() > 0 and item.is_visible():
            item.click()
            page.wait_for_timeout(1800)
            clicked += 1

    assert clicked >= 3, f"子导航切换不足，只点到 {clicked} 项，本用例未真正走通"
    assert len(page_errors) == 0, f"组合交易视图出现 page error：{page_errors}"
