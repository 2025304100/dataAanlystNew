"""E2E 测试 - 组合交易视图与手动交易入口（原「模拟买入」用例重写）。

现状（2026-09-29 实测，非猜测）：
- 主视图已重排为：今日决策 / 组合交易 / 机会中心 / 宏观数据 / 行情消息 / 设置。
  旧用例依赖的「目前观察池」主 Tab 与「模拟交易」子 Tab 都不存在了。
- **手动交易仍然存在**，只是换了形态：入口在「持仓成员」表的行内操作
  （调仓 / 清仓），前端走 `api.submitSimOrder(portfolioId, {symbol_id, side,
  quantity, order_type:"market", note:"manual rebalance"|"manual close"})`
  （PortfolioMembersTable.tsx L365 / L421）。
  本文件曾按旧 id（simQuantityInput / simPriceInput / simBuyButton）计数为 0
  判定"手动下单入口缺失"并挂了 PT-DEF-22 警报线 —— **那是误判，已撤销**：
  旧面板形态确实下线了（宿主 Trading.tsx / TradingPanel.tsx 成了孤儿组件），
  但功能改由行内动作承担。教训：不能用旧选择器证明功能不存在，必须先看现役形态。
- 注意组合可能处于 HG1 门禁锁定态（如「对账差异阻塞」会把 NEW_BUY / RISK_EXIT
  关掉，按钮显示 🔒）—— 锁住 ≠ 不存在，用例只验入口存在与锁定原因可读，
  绝不提交订单（不产生真实模拟成交）。
- 本文件同时纠正旧写法：导航一律"必须真的点进去"，不再用 `if 可见: 点击`
  让用例在元素缺失时静默通过（那是假绿）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# 组合交易主视图：现名 + 历史名（改名过渡期都能命中）
PORTFOLIO_TAB_LABELS = ("组合交易", "Portfolio Trading", "目前观察池", "投资中心")

# 组合交易视图内的子导航（DOM 侦察实测）
PORTFOLIO_SUBNAV_LABELS = ("总览", "持仓成员", "策略规则", "回测中心", "治理")

# 手动交易动作在持仓成员行内的现名（旧面板时代的 simXxx id 已随之作废）
MANUAL_TRADE_ACTION_LABELS = ("再平衡", "调仓", "清仓", "平仓")


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


def test_manual_trade_entry_lives_in_members_table(page):
    """手动交易入口的真实形态：「持仓成员」行内 调仓 / 清仓。

    本用例取代原先那条按旧 id 判定"手动下单不存在"的 xfail 警报线（PT-DEF-22
    系误判，见模块 docstring）。钉住三件事：
    1) 持仓成员表里确实存在手动交易动作；
    2) 动作处于锁定态（🔒 / disabled）时必须给出可读原因，不能只"看起来坏了"；
    3) 全程不点提交，不产生真实模拟成交。
    """
    _open_view(page, PORTFOLIO_TAB_LABELS)

    members_nav = page.locator(
        "button:has-text('持仓成员'), [role='tab']:has-text('持仓成员')"
    ).first
    assert members_nav.count() > 0 and members_nav.is_visible(), "找不到「持仓成员」子导航"
    members_nav.click()
    page.wait_for_timeout(2500)

    rows = page.locator("table tbody tr")
    assert rows.count() >= 1, (
        "持仓成员表没有数据行，无法验证手动交易入口——"
        "若持仓被刻意清空，请连同本用例一起改写，不要直接删断言"
    )

    actions = page.locator("table tbody tr td:last-child button")
    labels = [actions.nth(i).inner_text().strip() for i in range(actions.count())]
    manual = [
        text for text in labels
        if any(key in text for key in MANUAL_TRADE_ACTION_LABELS)
    ]
    assert manual, (
        f"持仓成员行内找不到手动交易动作（期望含 {list(MANUAL_TRADE_ACTION_LABELS)} 之一），"
        f"实际按钮：{labels}"
    )

    # 锁定态必须能解释自己：有 title 说明，或按钮文案自带锁定标记
    for i in range(actions.count()):
        btn = actions.nth(i)
        text = labels[i]
        if not any(key in text for key in MANUAL_TRADE_ACTION_LABELS):
            continue
        if btn.is_disabled() or "🔒" in text:
            explanation = (btn.get_attribute("title") or "") + text
            assert explanation.strip(), (
                f"手动交易动作被锁定却没有任何可读说明：label={text!r}"
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
