# -*- coding: utf-8 -*-
"""
第二轮 - 组合交易模块深度功能测试
- 针对二级子视图（工作台/模拟交易）分别探索
- 全量滚动捕获所有模块
- 后端API路由自动探测（/api/v1 /api 等前缀）
- 交互测试（新建组合弹窗、录入表单按钮等）
"""
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any

from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PwTimeoutError

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots", "round2")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


@dataclass
class TestCase:
    id: str
    name: str
    category: str
    priority: str
    status: str = "pending"
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
    network_4xx: List[Dict] = field(default_factory=list)
    api_probe_results: Dict = field(default_factory=dict)

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
                "console_errors_warnings": len(self.console_errors),
                "network_4xx_5xx": len(self.network_4xx),
            },
            "test_cases": [asdict(t) for t in self.test_cases],
            "console_errors": self.console_errors,
            "network_4xx": self.network_4xx,
            "api_probe": self.api_probe_results,
        }


RPT = TestReport()


def ss(page: Page, name: str, full=True):
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    try:
        page.screenshot(path=path, full_page=full)
    except Exception:
        pass
    return path


def run(case: TestCase, fn):
    t0 = time.time()
    try:
        r = fn()
        case.status = "passed"
        case.message = str(r)[:400] if r else "OK"
    except AssertionError as e:
        case.status = "failed"
        case.message = f"Assert: {str(e)[:300]}"
    except Exception as e:
        case.status = "failed"
        case.message = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        case.duration_ms = int((time.time() - t0) * 1000)
        RPT.test_cases.append(case)
        sym = {"passed": "✅", "failed": "❌", "skipped": "⏭ "}.get(case.status, "?")
        print(f"{sym} P{case.priority[-1]} {case.id:8s} | {case.name[:40]:40s} | {case.message[:80]}")


def scroll_all(page: Page, step=600):
    """逐步滚动到页面底部并返回，确保懒加载触发"""
    positions = []
    for i in range(15):
        positions.append(page.evaluate("window.scrollY"))
        page.evaluate(f"window.scrollBy(0, {step})")
        time.sleep(0.3)
    # 回到顶部
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)
    return max(positions) if positions else 0


def body_text(page: Page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def count_elements(page: Page, *selectors: str) -> Dict[str, int]:
    result = {}
    for s in selectors:
        try:
            result[s] = page.locator(s).count()
        except Exception:
            result[s] = -1
    return result


def main():
    RPT.start_time = time.strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--no-sandbox", "--window-size=1680,950"],
        )
        ctx = browser.new_context(viewport={"width": 1680, "height": 950}, locale="zh-CN")
        page: Page = ctx.new_page()

        def on_console(msg):
            if msg.type in ("error", "warning"):
                RPT.console_errors.append({
                    "type": msg.type,
                    "text": msg.text[:400],
                    "ts": time.strftime("%H:%M:%S"),
                })

        def on_response(resp):
            s = resp.status
            if s >= 400 and "sockjs" not in resp.url and "hmr" not in resp.url and "@fs" not in resp.url:
                RPT.network_4xx.append({
                    "status": s,
                    "method": resp.request.method,
                    "url": resp.url[:200],
                })

        page.on("console", on_console)
        page.on("response", on_response)

        URL = "http://127.0.0.1:5173"

        # ---------------- T0: 加载并切换组合交易 ----------------
        def t0():
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            time.sleep(3)
            # 点组合交易
            for sel in ["text=组合交易", "role=tab[name='组合交易']", "button:has-text('组合交易')"]:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click(timeout=5000)
                        break
                except Exception:
                    pass
            page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(3)
            scroll_all(page)
            time.sleep(1)
            ss(page, "T0_pt_main_fulloffset")
            return f"URL={page.url}"

        run(TestCase("T0", "进入组合交易主视图+全滚动捕获", "组合交易", "P0"), t0)

        # ---------------- T1: 侦察二级子视图（工作台/模拟交易）入口 ----------------
        sub_views = {"workbench": None, "trading": None}

        def t1():
            global sub_views
            # 找二级按钮
            candidates = {
                "workbench": ["text=工作台", "button:has-text('工作台')", "role=tab[name='工作台']"],
                "trading": ["text=模拟交易", "button:has-text('模拟交易')", "role=tab[name='模拟交易']", "text=资产详情", "button:has-text('资产详情')"],
            }
            found = {}
            for key, sels in candidates.items():
                for sel in sels:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0 and loc.is_visible():
                            found[key] = sel
                            sub_views[key] = sel
                            break
                    except Exception:
                        continue
            ss(page, "T1_subview_buttons")
            return json.dumps(found, ensure_ascii=False)

        run(TestCase("T1", "发现二级子视图入口(工作台/模拟交易)", "结构", "P0"), t1)

        # ---------------- T2: 工作台视图(Workbench)深度侦察 ----------------
        def t2():
            if not sub_views.get("workbench"):
                return "SKIP: 未找到工作台入口"
            page.locator(sub_views["workbench"]).first.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=20000)
            time.sleep(2)
            max_y = scroll_all(page)
            time.sleep(1.5)
            bt = body_text(page)
            # 统计关键元素
            key_parts = ["暴露分析", "组合规则", "持仓录入", "备份", "恢复", "候选", "组合成员", "评分", "资产", "今日机会"]
            found = [k for k in key_parts if k in bt]
            elems = count_elements(page,
                "input[placeholder*='代码'], input[placeholder*='symbol'], input[placeholder*='标的']",
                "input[placeholder*='数量'], input[placeholder*='股数']",
                "input[placeholder*='成本']",
                "button:has-text('录入对齐'), button:has-text('保存'), button:has-text('加入')",
                ".ant-card",
                ".ant-table-wrapper",
                ".ant-switch",
            )
            ss(page, "T2_workbench_full")
            return f"滚动={max_y}px, 关键词{found}, 元素计数={json.dumps(elems, ensure_ascii=False)}"

        run(TestCase("T2", "工作台视图-功能模块全面侦察", "工作台", "P0"), t2)

        # ---------------- T3: 模拟交易视图(Trading)深度侦察 ----------------
        def t3():
            if not sub_views.get("trading"):
                return "SKIP: 未找到模拟交易入口"
            page.locator(sub_views["trading"]).first.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=20000)
            time.sleep(2)
            max_y = scroll_all(page)
            time.sleep(1.5)
            bt = body_text(page)
            key_parts = ["绩效", "净值", "回撤", "夏普", "胜率", "自动交易", "回测", "买入", "卖出", "下单", "持仓", "订单", "市价"]
            found = [k for k in key_parts if k in bt]
            elems = count_elements(page,
                "button:has-text('买入'), button:has-text('买')",
                "button:has-text('卖出'), button:has-text('卖')",
                "input[type='number'], .ant-input-number",
                ".ant-tabs-nav, [role='tablist']",
                ".ant-statistic",
                ".ant-card",
                ".ant-table-wrapper",
                "svg, canvas",
            )
            ss(page, "T3_trading_full")
            return f"滚动={max_y}px, 关键词{found}, 元素计数={json.dumps(elems, ensure_ascii=False)}"

        run(TestCase("T3", "模拟交易视图-下单/绩效/自动交易/回测侦察", "模拟交易", "P0"), t3)

        # ---------------- T4: Header区域侦察(组合选择器/新建组合/刷新) ----------------
        def t4():
            bt = body_text(page)
            has_new_portfolio = "+ 新建组合" in bt or "新建组合" in bt
            has_settings = "⚙" in bt or "设置" in bt
            has_refresh = "刷 新" in bt or "刷新" in bt
            # 组合下拉
            select_count = page.locator(".ant-select").count()
            # 点击新建组合尝试打开弹窗
            modal_opened = False
            try:
                new_btn = page.locator("button:has-text('新建组合'), button:has-text('+ 新建')").first
                if new_btn.count() > 0 and new_btn.is_visible():
                    new_btn.click(timeout=5000)
                    time.sleep(1.5)
                    modal_opened = page.locator(".ant-modal-content").count() > 0
                    ss(page, "T4_new_portfolio_modal")
                    # 关闭
                    if modal_opened:
                        close_btn = page.locator(".ant-modal-close").first
                        if close_btn.count() > 0:
                            close_btn.click(timeout=3000)
                            time.sleep(0.8)
            except Exception as e:
                pass
            return f"新建按钮={has_new_portfolio}, 设置={has_settings}, 刷新={has_refresh}, 下拉数={select_count}, 弹窗打开={modal_opened}"

        run(TestCase("T4", "Header操作区+新建组合弹窗交互", "Header", "P0"), t4)

        # ---------------- T5: 工作台视图-持仓录入表单交互 ----------------
        def t5():
            if not sub_views.get("workbench"):
                return "SKIP"
            page.locator(sub_views["workbench"]).first.click(timeout=5000)
            time.sleep(2)
            scroll_all(page)
            time.sleep(1)
            # 找输入框
            inputs = {
                "code": page.locator("input[placeholder*='代码'], input[placeholder*='标的']").first,
                "qty": page.locator("input[placeholder*='数量'], input[placeholder*='股数']").first,
                "cost": page.locator("input[placeholder*='成本']").first,
            }
            ok = {}
            for k, loc in inputs.items():
                try:
                    exists = loc.count() > 0
                    ok[k] = exists
                    if exists and loc.is_visible():
                        loc.click(timeout=2000)
                        loc.fill("1", timeout=2000)
                        ok[k + "_filled"] = True
                        time.sleep(0.2)
                except Exception as e:
                    ok[k + "_err"] = str(e)[:80]
            ss(page, "T5_position_entry_test")
            # 清空
            for k, loc in inputs.items():
                try:
                    if loc.count() > 0:
                        loc.fill("", timeout=1500)
                except Exception:
                    pass
            return json.dumps(ok, ensure_ascii=False)

        run(TestCase("T5", "工作台-手动录入持仓表单交互测试", "工作台", "P1"), t5)

        # ---------------- T6: 后端API路由探测 ----------------
        def t6():
            # 尝试多个前缀
            candidates = [
                ("health_root", "http://127.0.0.1:8000/health"),
                ("health_v1", "http://127.0.0.1:8000/api/v1/health"),
                ("health_api", "http://127.0.0.1:8000/api/health"),
                ("docs", "http://127.0.0.1:8000/docs"),
                ("portfolios_v1", "http://127.0.0.1:8000/api/v1/portfolios"),
                ("portfolios_api", "http://127.0.0.1:8000/api/portfolios"),
                ("portfolios_root", "http://127.0.0.1:8000/portfolios"),
                ("symbols_v1", "http://127.0.0.1:8000/api/v1/symbols?limit=5"),
                ("openapi_json", "http://127.0.0.1:8000/openapi.json"),
            ]
            results = {}
            for name, url in candidates:
                try:
                    r = page.request.get(url, timeout=8000)
                    results[name] = {"status": r.status, "ok": 200 <= r.status < 400}
                    if 200 <= r.status < 400 and name == "openapi_json":
                        # 探测路由前缀
                        try:
                            body = r.json()
                            paths = list(body.get("paths", {}).keys())[:20]
                            RPT.api_probe_results["prefix_paths_sample"] = paths
                            # 判断前缀
                            has_v1 = any("/api/v1/" in p for p in paths)
                            has_api = any(p.startswith("/api/") for p in paths)
                            results["api_prefix_detected"] = {
                                "has_v1": has_v1, "has_api": has_api,
                                "first_5": paths[:5],
                            }
                        except Exception:
                            pass
                except Exception as e:
                    results[name] = {"status": -1, "err": str(e)[:80]}
            RPT.api_probe_results.update(results)
            return json.dumps(results, ensure_ascii=False)

        run(TestCase("T6", "后端API路由前缀探测+接口可用性", "API", "P0"), t6)

        # ---------------- T7: 模拟交易视图-绩效/自动交易/回测Tab是否存在（检查Tabs组件） ----------------
        def t7():
            if not sub_views.get("trading"):
                return "SKIP"
            page.locator(sub_views["trading"]).first.click(timeout=5000)
            time.sleep(2)
            scroll_all(page)
            time.sleep(1)
            # 找所有ant-tabs
            tabs_containers = page.locator(".ant-tabs-nav")
            tab_count = tabs_containers.count()
            tab_labels = []
            for i in range(min(tab_count, 5)):
                try:
                    labels = tabs_containers.nth(i).locator("[role='tab']").all_inner_texts()
                    tab_labels.append(labels)
                except Exception:
                    pass
            ss(page, "T7_tabs_inspect")
            return f"Tab容器数={tab_count}, 各容器Tab标签={json.dumps(tab_labels, ensure_ascii=False)}"

        run(TestCase("T7", "模拟交易-Tabs结构侦察(绩效/自动交易/回测)", "模拟交易", "P0"), t7)

        # ---------------- T8: 持仓表详细侦察(列+操作按钮) ----------------
        def t8():
            if not sub_views.get("trading"):
                return "SKIP"
            page.locator(sub_views["trading"]).first.click(timeout=5000)
            time.sleep(2)
            # 找所有表格
            tables = page.locator(".ant-table-wrapper")
            count = tables.count()
            details = []
            for i in range(min(count, 3)):
                try:
                    headers = tables.nth(i).locator("thead th").all_inner_texts()
                    row_count = tables.nth(i).locator("tbody tr").count()
                    op_btns = tables.nth(i).locator("tbody button, tbody a").count()
                    details.append({"idx": i, "headers": headers, "rows": row_count, "op_btns": op_btns})
                except Exception as e:
                    details.append({"idx": i, "err": str(e)[:80]})
            ss(page, "T8_tables_detail")
            return f"表格数={count}, 详情={json.dumps(details, ensure_ascii=False)}"

        run(TestCase("T8", "持仓表结构详细侦察(列/行/操作按钮)", "模拟交易", "P0"), t8)

        # ---------------- T9: 控制台错误聚合 ----------------
        def t9():
            err_types = {}
            for e in RPT.console_errors:
                t = e["type"]
                err_types[t] = err_types.get(t, 0) + 1
            # 过滤重复告警（ECharts alignTicks同一类型多次）
            unique_texts = list({e["text"][:80] for e in RPT.console_errors})
            return f"类型计数={json.dumps(err_types)}, 唯一告警/错误={len(unique_texts)}条, 样例={json.dumps(unique_texts[:5], ensure_ascii=False)}"

        run(TestCase("T9", "控制台错误/警告分析", "质量", "P1"), t9)

        # ---------------- T10: 网络4xx请求分析 ----------------
        def t10():
            if not RPT.network_4xx:
                return "无失败请求"
            by_status = {}
            for r in RPT.network_4xx:
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            return f"失败请求={len(RPT.network_4xx)}个, 状态分布={json.dumps(by_status)}, 样例URL={json.dumps([r['url'][:120] for r in RPT.network_4xx[:5]], ensure_ascii=False)}"

        run(TestCase("T10", "网络失败请求分析(4xx/5xx)", "网络", "P1"), t10)

        RPT.end_time = time.strftime("%Y-%m-%d %H:%M:%S")
        browser.close()

    # 保存完整报告
    report_path = os.path.join(OUTPUT_DIR, "portfolio_round2_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(RPT.to_dict(), f, ensure_ascii=False, indent=2)

    # 终端总结
    s = RPT.to_dict()["summary"]
    print("\n" + "=" * 80)
    print(f"  第二轮深度测试总结 | {RPT.start_time} → {RPT.end_time}")
    print("=" * 80)
    print(f"  用例: 总数={s['total']}  ✅通过={s['passed']}  ❌失败={s['failed']}  ⏭跳过={s['skipped']}")
    print(f"  质量: Console告警/错误={s['console_errors_warnings']}条  网络4xx/5xx={s['network_4xx_5xx']}次")
    if RPT.api_probe_results:
        print(f"  API探测: 已写入{len(RPT.api_probe_results)}个路由结果")
    print(f"  截图: {SCREENSHOT_DIR}")
    print(f"  报告: {report_path}")
    print("=" * 80)
    fails = [t for t in RPT.test_cases if t.status == "failed"]
    if fails:
        print("\n❌ 失败清单:")
        for t in fails:
            print(f"  - [{t.priority}] {t.id} {t.name}: {t.message}")
    else:
        print("\n✅ 本轮无失败用例")


if __name__ == "__main__":
    main()
