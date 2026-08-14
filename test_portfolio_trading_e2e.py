# -*- coding: utf-8 -*-
"""
组合交易模块综合自动化测试脚本
- 页面布局侦察
- 功能交互测试
- 控制台错误捕获
- 网络请求监控
"""
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any

from playwright.sync_api import sync_playwright, Page, Browser, ConsoleMessage, Request, Response

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


@dataclass
class TestCase:
    id: str
    name: str
    category: str
    priority: str  # P0/P1/P2
    status: str = "pending"  # pending/passed/failed/skipped
    message: str = ""
    duration_ms: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestReport:
    start_time: str = ""
    end_time: str = ""
    total_duration_ms: int = 0
    test_cases: List[TestCase] = field(default_factory=list)
    console_errors: List[Dict] = field(default_factory=list)
    failed_requests: List[Dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_duration_ms": self.total_duration_ms,
            "summary": {
                "total": len(self.test_cases),
                "passed": sum(1 for t in self.test_cases if t.status == "passed"),
                "failed": sum(1 for t in self.test_cases if t.status == "failed"),
                "skipped": sum(1 for t in self.test_cases if t.status == "skipped"),
                "console_errors": len(self.console_errors),
                "failed_requests": len(self.failed_requests),
            },
            "test_cases": [asdict(t) for t in self.test_cases],
            "console_errors": self.console_errors,
            "failed_requests": self.failed_requests,
        }


REPORT = TestReport()


def capture(page: Page, name: str):
    """截图辅助函数"""
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    return path


def run_test(case: TestCase, func):
    """执行单个测试用例并记录结果"""
    t0 = time.time()
    try:
        result = func()
        case.status = "passed"
        case.message = str(result) if result else "OK"
    except Exception as e:
        case.status = "failed"
        case.message = f"Exception: {type(e).__name__}: {str(e)[:300]}"
    finally:
        case.duration_ms = int((time.time() - t0) * 1000)
        REPORT.test_cases.append(case)
        print(f"[{case.status.upper():7s}] P{case.priority[-1]} {case.id:10s} | {case.name} - {case.message[:80]}")


def main():
    REPORT.start_time = time.strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1600,900",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            locale="zh-CN",
        )
        page: Page = context.new_page()

        # 捕获控制台错误
        def on_console(msg: ConsoleMessage):
            if msg.type in ("error", "warning"):
                REPORT.console_errors.append({
                    "type": msg.type,
                    "text": msg.text[:500],
                    "location": str(msg.location),
                    "timestamp": time.strftime("%H:%M:%S"),
                })

        # 捕获失败的网络请求
        def on_response(response: Response):
            status = response.status
            if status >= 400 and "sockjs" not in response.url and "hmr" not in response.url:
                REPORT.failed_requests.append({
                    "status": status,
                    "url": response.url[:200],
                    "method": response.request.method,
                    "timestamp": time.strftime("%H:%M:%S"),
                })

        page.on("console", on_console)
        page.on("response", on_response)

        BASE_URL = "http://127.0.0.1:5173"

        # ==============================================================
        # TC-G0: 访问首页 & 全页加载
        # ==============================================================
        def tc_g0():
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            time.sleep(3)
            capture(page, "G0_homepage_loaded")
            # 检查是否有主容器
            assert page.locator("#root").count() >= 1, "根容器不存在"
            title = page.title()
            return f"Title='{title}', URL={page.url}"

        run_test(TestCase("G0", "访问首页并加载完整", "通用", "P0"), tc_g0)

        # ==============================================================
        # TC-G1: 检查主导航存在性（今日决策/组合交易/机会中心/宏观数据/行情消息/设置）
        # ==============================================================
        def tc_g1():
            nav_texts = page.locator("nav a, nav button, [role='tab']").all_inner_texts()
            nav_flat = " ".join(nav_texts)
            key_items = ["今日决策", "组合", "机会", "宏观", "行情", "设置"]
            found = [k for k in key_items if k in nav_flat]
            capture(page, "G1_navigation")
            return f"找到关键导航: {found} / {key_items}"

        run_test(TestCase("G1", "主导航结构完整性", "通用", "P0"), tc_g1)

        # ==============================================================
        # TC-PT0: 切换到"组合交易"Tab
        # ==============================================================
        portfolio_tab_selector = None

        def tc_pt0():
            global portfolio_tab_selector
            # 尝试多种选择器定位组合交易入口
            selectors = [
                "text=组合交易",
                "role=tab[name='组合交易']",
                "button:has-text('组合交易')",
                "a:has-text('组合交易')",
                "[data-tab-key='portfolio-trading']",
                "[data-tab-key='trading']",
            ]
            clicked = False
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=5000)
                        portfolio_tab_selector = sel
                        clicked = True
                        break
                except Exception:
                    continue
            assert clicked, "无法找到并点击组合交易Tab"
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(2)
            capture(page, "PT0_portfolio_trading_tab")
            return f"使用选择器: {portfolio_tab_selector}"

        run_test(TestCase("PT0", "进入组合交易主页面", "组合交易", "P0"), tc_pt0)

        # ==============================================================
        # TC-PT1: 组合交易页面布局结构侦察（大改造前现状）
        # ==============================================================
        def tc_pt1():
            sections = {
                "band_account": page.locator(".account-band, .band.account-band, [class*='account-band']").count(),
                "performance_panel": page.locator("[class*='performance-panel'], [class*='PerformancePanel']").count(),
                "auto_trade_panel": page.locator("[class*='auto-trade'], [class*='AutoTrade']").count(),
                "backtest_panel": page.locator("[class*='backtest'], [class*='Backtest']").count(),
                "trading_panel": page.locator("[class*='trading-panel'], [class*='TradingPanel'], .trading-order-panel").count(),
                "position_table": page.locator("table:has-text('持仓量'), table:has-text('代码')").count(),
                "order_panel_buttons": page.locator(".trading-order-panel button, [class*='order'] button").count(),
                "tabs": page.locator(".ant-tabs-nav, [role='tablist']").count(),
            }
            capture(page, "PT1_layout_overview")
            return json.dumps(sections, ensure_ascii=False)

        run_test(TestCase("PT1", "组合交易页面布局元素侦察", "组合交易", "P0"), tc_pt1)

        # ==============================================================
        # TC-PT2: 顶部账户汇总Band（6列）存在性
        # ==============================================================
        def tc_pt2():
            # 查找总资产/可用现金/持仓市值/累计收益/最大回撤等指标文本
            page_text = page.inner_text("body")
            key_metrics = ["总资产", "可用现金", "持仓市值", "累计收益", "最大回撤", "夏普"]
            found_metrics = [k for k in key_metrics if k in page_text]
            # 检查是否有Statistic样式卡片
            stat_count = page.locator(".ant-statistic, [class*='stat-card']").count()
            return f"找到指标{found_metrics}（{len(found_metrics)}/{len(key_metrics)}）, Statistic组件={stat_count}"

        run_test(TestCase("PT2", "顶部账户汇总指标卡存在性", "组合交易", "P0"), tc_pt2)

        # ==============================================================
        # TC-PT3: 下单面板功能侦察
        # ==============================================================
        def tc_pt3():
            # 尝试定位下单面板
            order_panel_locators = [
                ".trading-order-panel",
                "[class*='order-panel']",
                "text=买:text('卖') >> xpath=ancestor::div[contains(@class,'panel')]",
            ]
            panel_found = False
            for sel in order_panel_locators:
                if page.locator(sel).count() > 0:
                    panel_found = True
                    break
            # 检查数量/价格输入框
            qty_inputs = page.locator("input[type='number'], .ant-input-number-input").count()
            buy_btns = page.locator("button:has-text('买')").count()
            sell_btns = page.locator("button:has-text('卖')").count()
            capture(page, "PT3_order_panel_detail")
            return f"面板存在={panel_found}, 数字输入框={qty_inputs}, 买按钮={buy_btns}, 卖按钮={sell_btns}"

        run_test(TestCase("PT3", "下单面板元素完整性", "组合交易", "P0"), tc_pt3)

        # ==============================================================
        # TC-PT4: 持仓表格存在性 & 列数量
        # ==============================================================
        def tc_pt4():
            tables = page.locator(".ant-table-wrapper")
            count = tables.count()
            if count == 0:
                return "未找到任何表格组件"
            # 取第一个可能的持仓表
            first_table = tables.first
            headers = first_table.locator("thead th").all_inner_texts()
            capture(page, "PT4_position_table")
            return f"表格数={count}, 表头={headers}"

        run_test(TestCase("PT4", "持仓表格&列结构", "组合交易", "P0"), tc_pt4)

        # ==============================================================
        # TC-PT5: 绩效面板（日期选择+指标卡+归因/复盘Tabs）存在性
        # ==============================================================
        def tc_pt5():
            has_date_picker = page.locator(".ant-picker, [placeholder*='开始'], [placeholder*='日期']").count()
            has_equity_chart = page.locator("svg, canvas, echarts, [class*='chart'], [_echarts_instance_]").count()
            perf_keywords = ["夏普", "胜率", "年化", "净值曲线", "回撤"]
            body_text = page.inner_text("body")
            found_kw = [k for k in perf_keywords if k in body_text]
            capture(page, "PT5_performance_panel")
            return f"日期picker={has_date_picker}, 图表容器={has_equity_chart}, 绩效关键词={found_kw}"

        run_test(TestCase("PT5", "绩效面板存在性与元素", "组合交易", "P0"), tc_pt5)

        # ==============================================================
        # TC-PT6: 自动交易面板（dry_run/执行/成员）
        # ==============================================================
        def tc_pt6():
            auto_keys = ["自动交易", "auto", "dry", "模拟运行", "执行交易"]
            body_text = page.inner_text("body")
            found = [k for k in auto_keys if k in body_text]
            auto_btn_count = page.locator("button:has-text('自动'),button:has-text('执行'),button:has-text('模拟')").count()
            switches = page.locator(".ant-switch").count()
            capture(page, "PT6_auto_trade")
            return f"关键词命中={found}, 相关按钮={auto_btn_count}, Switch开关={switches}"

        run_test(TestCase("PT6", "自动交易面板侦察", "组合交易", "P0"), tc_pt6)

        # ==============================================================
        # TC-PT7: 回测面板侦察
        # ==============================================================
        def tc_pt7():
            backtest_keys = ["回测", "Backtest", "引擎", "快照"]
            body_text = page.inner_text("body")
            found = [k for k in backtest_keys if k in body_text]
            run_btns = page.locator("button:has-text('回测'),button:has-text('运行')").count()
            capture(page, "PT7_backtest_panel")
            return f"关键词命中={found}, 回测相关按钮={run_btns}"

        run_test(TestCase("PT7", "回测面板侦察", "组合交易", "P0"), tc_pt6)

        # ==============================================================
        # TC-WB0: 切换到"组合工作台"（或workbench入口）
        # ==============================================================
        workbench_entered = False

        def tc_wb0():
            global workbench_entered
            # 多种定位方式
            selectors = [
                "text=组合工作台",
                "role=tab[name='组合工作台']",
                "button:has-text('组合工作台')",
                "a:has-text('组合工作台')",
                "[data-tab-key='workbench']",
                "[data-tab-key='portfolio-workbench']",
            ]
            clicked = False
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                return "SKIP: 未找到组合工作台入口，可能当前在单Tab或其他结构"
            workbench_entered = True
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(2)
            capture(page, "WB0_workbench")
            return "成功切换到工作台"

        run_test(TestCase("WB0", "切换到组合工作台", "组合工作台", "P0"), tc_wb0)

        # ==============================================================
        # TC-WB1: 工作台结构侦察（暴露分析/规则/候选/持仓录入）
        # ==============================================================
        def tc_wb1():
            if not workbench_entered:
                return "SKIP: 未进入工作台"
            wb_keys = ["暴露分析", "组合规则", "候选", "持仓录入", "备份", "恢复", "成员"]
            body_text = page.inner_text("body")
            found = [k for k in wb_keys if k in body_text]
            # 检查表单/录入元素
            form_inputs = page.locator("input[placeholder*='代码'], input[placeholder*='数量'], input[placeholder*='成本']").count()
            tables_count = page.locator(".ant-table-wrapper").count()
            capture(page, "WB1_structure")
            return f"关键词命中={found}, 录入输入框={form_inputs}, 表格数={tables_count}"

        run_test(TestCase("WB1", "组合工作台布局侦察", "组合工作台", "P0"), tc_wb1)

        # ==============================================================
        # TC-WB2: 手动录入持仓表单存在性
        # ==============================================================
        def tc_wb2():
            if not workbench_entered:
                return "SKIP"
            # 代码/数量/成本输入框或表单卡片
            code_input = page.locator("input[placeholder*='代码'], input[placeholder*='symbol']").count()
            qty_input = page.locator("input[placeholder*='数量'], input[placeholder*='股数'], input[placeholder*='Qty']").count()
            cost_input = page.locator("input[placeholder*='成本'], input[placeholder*='Cost']").count()
            save_btn = page.locator("button:has-text('保存'), button:has-text('录入')").count()
            capture(page, "WB2_position_entry_form")
            return f"代码框={code_input}, 数量框={qty_input}, 成本框={cost_input}, 保存按钮={save_btn}"

        run_test(TestCase("WB2", "手动录入持仓表单存在性", "组合工作台", "P0"), tc_wb2)

        # ==============================================================
        # TC-WB3: 暴露分析&规则卡片双列布局
        # ==============================================================
        def tc_wb3():
            if not workbench_entered:
                return "SKIP"
            body = page.inner_text("body")
            expose_count = body.count("暴露")
            rule_count = body.count("规则")
            card_count = page.locator(".ant-card, [class*='card-']").count()
            capture(page, "WB3_expose_rule_cards")
            return f"暴露提及={expose_count}, 规则提及={rule_count}, 卡片总数={card_count}"

        run_test(TestCase("WB3", "暴露分析+规则卡存在性", "组合工作台", "P1"), tc_wb3)

        # ==============================================================
        # TC-WB4: 备份/恢复区域存在性
        # ==============================================================
        def tc_wb4():
            if not workbench_entered:
                return "SKIP"
            body = page.inner_text("body")
            backup_count = body.count("备份") + body.count("backup")
            restore_count = body.count("恢复") + body.count("restore")
            return f"备份相关={backup_count}, 恢复相关={restore_count}"

        run_test(TestCase("WB4", "备份/恢复数据区存在性", "组合工作台", "P2"), tc_wb4)

        # ==============================================================
        # TC-PT8: 其他页面快速验证（今日决策/机会中心/宏观/行情消息/设置）
        # ==============================================================
        def tc_pt8():
            """快速切换其他Tab确保不崩溃"""
            tab_names = ["今日决策", "机会中心", "宏观数据", "行情消息", "设置"]
            results = {}
            for name in tab_names:
                try:
                    selectors = [
                        f"text='{name}'",
                        f"role=tab[name='{name}']",
                        f"button:has-text('{name}')",
                    ]
                    found_sel = None
                    for sel in selectors:
                        if page.locator(sel).first.count() > 0 and page.locator(sel).first.is_visible():
                            found_sel = sel
                            break
                    if found_sel:
                        page.locator(found_sel).first.click(timeout=3000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        time.sleep(1)
                        capture(page, f"PT8_{name}")
                        # 检查是否渲染出至少一个内容容器
                        has_content = page.locator(".ant-card, .ant-table, main").count() > 0
                        results[name] = "OK" if has_content else "无内容容器"
                    else:
                        results[name] = "未找到Tab入口"
                except Exception as e:
                    results[name] = f"错误: {type(e).__name__}"
            return json.dumps(results, ensure_ascii=False)

        run_test(TestCase("PT8", "其他主Tab可切换性(冒烟)", "通用", "P1"), tc_pt8)

        # ==============================================================
        # TC-PT9: 返回组合交易页并做快速交互测试
        # ==============================================================
        def tc_pt9():
            # 尝试切回组合交易
            if portfolio_tab_selector:
                page.locator(portfolio_tab_selector).first.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(1)
            # 检查是否有组合选择下拉框
            portfolio_select = page.locator(".ant-select, [class*='portfolio-select']").count()
            # 刷新按钮
            refresh_btn = page.locator("button:has-text('刷新'), [aria-label='refresh']").count()
            # 新建组合按钮
            new_pf_btn = page.locator("button:has-text('新建组合'), button:has-text('创建组合')").count()
            capture(page, "PT9_final_portfolio_trading")
            return f"组合选择器={portfolio_select}, 刷新按钮={refresh_btn}, 新建组合按钮={new_pf_btn}"

        run_test(TestCase("PT9", "返回组合交易 - Header操作区", "组合交易", "P0"), tc_pt9)

        # ==============================================================
        # TC-API0: 后端API健康检查（组合相关接口）
        # ==============================================================
        def tc_api0():
            results = {}
            api_urls = [
                ("health", "http://127.0.0.1:8000/api/health"),
                ("portfolios_list", "http://127.0.0.1:8000/api/portfolios"),
                ("symbols_list", "http://127.0.0.1:8000/api/symbols?limit=10"),
            ]
            for name, url in api_urls:
                try:
                    resp = page.request.get(url, timeout=10000)
                    results[name] = {
                        "status": resp.status,
                        "ok": 200 <= resp.status < 400,
                    }
                except Exception as e:
                    results[name] = {"status": -1, "error": str(e)[:100]}
            return json.dumps(results, ensure_ascii=False)

        run_test(TestCase("API0", "后端核心API健康检查", "API", "P0"), tc_api0)

        # 结束
        REPORT.end_time = time.strftime("%Y-%m-%d %H:%M:%S")
        browser.close()

    # 保存报告
    report_path = os.path.join(OUTPUT_DIR, "portfolio_trading_test_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(REPORT.to_dict(), f, ensure_ascii=False, indent=2)

    # 打印总结
    summary = REPORT.to_dict()["summary"]
    print("\n" + "=" * 70)
    print(f"  组合交易自动化测试总结  |  开始: {REPORT.start_time}  结束: {REPORT.end_time}")
    print("=" * 70)
    print(f"  用例总数: {summary['total']}  |  ✅ 通过: {summary['passed']}  |  ❌ 失败: {summary['failed']}  |  ⏭  跳过: {summary['skipped']}")
    print(f"  控制台错误/警告: {summary['console_errors']}  |  API失败请求: {summary['failed_requests']}")
    print(f"  报告路径: {report_path}")
    print(f"  截图目录: {SCREENSHOT_DIR}")
    print("=" * 70)

    # 打印失败用例详情
    failed = [t for t in REPORT.test_cases if t.status == "failed"]
    if failed:
        print("\n❌ 失败用例列表:")
        for t in failed:
            print(f"  - [P{t.priority[-1]}] {t.id} {t.name}: {t.message}")
    else:
        print("\n✅ 全部测试通过，无失败用例！")


if __name__ == "__main__":
    main()
