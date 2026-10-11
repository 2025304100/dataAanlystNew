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

import sys

import pytest

from tests._e2e_settle import settle

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
            settle(page, 2500)
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

    # 「有 <tr>」不等于「有成员行」：空态时组件渲染的是
    # <tr><td colSpan={10}>暂无持仓数据</td></tr>（PortfolioMembersTable.tsx L735-740）。
    # 若按 `table tbody tr` 计数做前置，占位行会把它恒满足，于是后面的断言只能报出
    # 「按钮 []」这种误导症状，把「环境没数据」说成「UI 回归」。
    # 真实成员行带 data-symbol-id（同文件 L747），占位行没有 —— 用它做前置既精准
    # 又与文案无关。注意不要用 `td[colspan]` 判空态：本页第二张表（候选标的池）
    # 也有 colspan 空态行（L950-953），页面级选择器会把本地正常的用例误杀。
    member_rows = page.locator("table tbody tr[data-symbol-id]")
    assert member_rows.count() >= 1, (
        "持仓成员表没有真实成员行（只有空态占位行或整表为空），无法验证手动交易入口。"
        "本用例的前置条件是「环境里已有持仓」：CI 用 init_db 新建空库，实测 "
        "symbols=0 / portfolios=0 / portfolio_members=0，而 symbol_id 是外键，"
        "没有行情底座就无法通过 UI「加入持仓」造出成员 —— 这是 e2e 数据前置缺失，"
        "不是 UI 缺陷，也不要用放宽断言来转绿。"
        "CI 上数据由「预置 e2e 最小数据」步骤提供（scripts/ci_seed_e2e_fixture.py）："
        "先确认那一步是否执行成功、以及 workflow 的 CI_DB_NAME 是否还在它的库名白名单里，"
        "再来看这里。"
        "（若持仓被刻意清空，请连同本用例一起改写，不要直接删断言）"
    )

    # 只在真实成员行里找动作（Playwright 对多元素 locator 取后代并集），
    # 这样既覆盖所有成员行，也不会把候选池表的按钮算进来。
    actions = member_rows.locator("td:last-child button")
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
    blocked_by_modal: list[str] = []
    for label in PORTFOLIO_SUBNAV_LABELS:
        item = page.locator(
            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
        ).first
        if item.count() > 0 and item.is_visible():
            # 进「回测中心」会首进自动预检；只要 blocking_reasons>0，产品就主动弹出
            # 阻断模态（既有设计，前端单测钉着它）。模态遮罩会盖住下一个子导航的
            # 点击 —— CI 实测就是稳定红在切向「治理」的那一步。
            # 这里不当“没看见”：先记下它确实弹了，并硬要求
            #   ① 有标题（用户能判断来历）；② 能关掉（否则子导航被永久阻塞）。
            # 关掉后继续导航，本用例守的仍是“切子导航不产生 JS 异常”。
            # “预检阻断该不该用模态盖住整个视图”属产品语义，已单独汇总待拍板。
            wrap = page.locator("div.ant-modal-wrap")
            if wrap.count() > 0 and wrap.first.is_visible():
                title = wrap.first.locator(
                    ".ant-modal-title, .ant-modal-confirm-title"
                ).first
                heading = title.inner_text().strip() if title.count() > 0 else ""
                assert heading, (
                    f"预检/告警模态弹出却不说明来历（无标题），拦在切向「{label}」之前："
                    f"{wrap.first.inner_text()[:200]!r}"
                )
                blocked_by_modal.append(f"{label} <- {heading}")
                # 可观测性：Actions 的 job 日志对匿名请求是 403，光靠“用例绿了”分不出
                # 到底是“模态没弹”还是“弹了并被关掉”。发一条 notice，让每轮 CI 自己
                # 报告这个阻塞导航的产品行为有没有发生（annotations 接口能读到）。
                # 必须写 sys.__stdout__ 并 flush：pytest 会捕获普通 print，
                # 被捕获的 workflow command 到不了 runner，annotation 根本不会生成。
                sys.__stdout__.write(
                    "::notice title=E2E 预检模态阻塞导航已被关闭::"
                    + f"拦在切向「{label}」前，标题={heading!r}"
                      f"（本轮共 {len(blocked_by_modal)} 次）\n"
                )
                sys.__stdout__.flush()
                closer = wrap.first.locator(
                    ".ant-modal-close, button:has-text('关闭'), button:has-text('取消')"
                ).first
                if closer.count() > 0:
                    closer.click(timeout=5000)
                else:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(600)
                assert not (wrap.count() > 0 and wrap.first.is_visible()), (
                    f"预检模态关不掉，组合交易子导航被永久阻塞：title={heading!r}"
                )
            item.click()
            page.wait_for_timeout(1800)
            clicked += 1

    assert clicked >= 3, f"子导航切换不足，只点到 {clicked} 项，本用例未真正走通"
    assert len(page_errors) == 0, (
        f"组合交易视图出现 page error：{page_errors}"
        f"（本轮曾被以下模态拦住导航：{blocked_by_modal}）"
    )
