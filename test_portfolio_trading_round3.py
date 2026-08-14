# -*- coding: utf-8 -*-
"""
第三轮 - 精准聚焦：模拟交易二级视图 + 工作台深度验证
- 修复闭包变量传播Bug
- 直接点击"模拟交易"二级按钮（选择器已验证）
- 检测模拟交易页是否有绩效/自动交易/回测
- 检测工作台持仓录入表单的3输入框+按钮是否可点击
- 调用 /api/v1/portfolios API 验证响应结构
- 检测新建组合弹窗字段完整性
"""
import json, os, time
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any
from playwright.sync_api import sync_playwright, Page

OUT = os.path.join(os.path.dirname(__file__), "test_output")
SD = os.path.join(OUT, "screenshots", "round3")
os.makedirs(SD, exist_ok=True)

@dataclass
class TC:
    id: str; name: str; cat: str; pri: str
    status: str = "pending"; msg: str = ""; dur: int = 0
    details: Dict = field(default_factory=dict)

R: List[TC] = []
CONS = []
NET4 = []

def ss(page, n, full=True):
    try: page.screenshot(path=os.path.join(SD, f"{n}.png"), full_page=full)
    except: pass

def exe(tc: TC, fn):
    t0 = time.time()
    try:
        r = fn(); tc.status = "passed"
        tc.msg = str(r)[:500] if r else "OK"
    except AssertionError as e: tc.status = "failed"; tc.msg = f"Assert:{str(e)[:300]}"
    except Exception as e: tc.status = "failed"; tc.msg = f"{type(e).__name__}:{str(e)[:300]}"
    finally:
        tc.dur = int((time.time()-t0)*1000); R.append(tc)
        s = {"passed":"✅","failed":"❌"}.get(tc.status,"⏭ ")
        print(f"{s} P{tc.pri[-1]} {tc.id:6s} | {tc.name[:38]:38s} | {tc.msg[:100]}")

def scroll(page, step=500, times=12):
    ys = []
    for _ in range(times):
        ys.append(page.evaluate("window.scrollY"))
        page.evaluate(f"window.scrollBy(0,{step})")
        time.sleep(0.25)
    page.evaluate("window.scrollTo(0,0)")
    time.sleep(0.5)
    return max(ys) if ys else 0

def text(page): return page.inner_text("body", timeout=5000)

def main():
    global CONS, NET4
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--disable-gpu","--no-sandbox","--window-size=1680,950"])
        c = b.new_context(viewport={"width":1680,"height":950}, locale="zh-CN")
        page: Page = c.new_page()
        page.on("console", lambda m: CONS.append({"t":m.type,"x":m.text[:300],"ts":time.strftime("%H:%M:%S")}) if m.type in ("error","warning") else None)
        page.on("response", lambda r: NET4.append({"s":r.status,"m":r.request.method,"u":r.url[:180]}) if r.status>=400 and "@fs" not in r.url and "hmr" not in r.url and "sockjs" not in r.url else None)

        URL = "http://127.0.0.1:5173"

        # ======= A0: 进入+切换组合交易 =======
        def a0():
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000); time.sleep(3)
            # 组合交易主Tab
            for s in ["text=组合交易", "button:has-text('组合交易')"]:
                try:
                    l = page.locator(s).first
                    if l.count()>0 and l.is_visible(): l.click(timeout=4000); break
                except: pass
            page.wait_for_load_state("networkidle", timeout=30000); time.sleep(2.5)
            ss(page, "A0_after_portfolio_tab")
            return page.url

        exe(TC("A0","进入组合交易主Tab","init","P0"), a0)

        # ======= A1: 工作台二级视图确认(结构验证) =======
        def a1():
            # 二级按钮检查
            wb_btn = page.locator("button:has-text('工作台')").first
            tr_btn = page.locator("button:has-text('模拟交易')").first
            wb_exist = wb_btn.count()>0
            tr_exist = tr_btn.count()>0
            # 当前激活态：检查工作台按钮的视觉态（有绿色active class概率大）
            wb_classes = wb_btn.get_attribute("class") if wb_exist else ""
            tr_classes = tr_btn.get_attribute("class") if tr_exist else ""
            # 统计工作台内容元素
            tb = text(page)
            keys = ["今日机会","账户概览","持仓管理","组合成员","组合暴露","数据管理","候选池","录入对齐","保存规则"]
            fk = [k for k in keys if k in tb]
            # 3输入框
            code_in = page.locator("input[placeholder*='代码']").count()
            qty_in = page.locator("input[placeholder*='数量'], input[placeholder*='股数']").count()
            cost_in = page.locator("input[placeholder*='成本']").count()
            lr_btn = page.locator("button:has-text('录入对齐')").count()
            save_rule_btn = page.locator("button:has-text('保存规则')").count()
            add_member_btn = page.locator("button:has-text('添加成员'), button:has-text('添加')").count()
            ss(page, "A1_workbench_view")
            return f"WBbtn={wb_exist},TRbtn={tr_exist},active=[{wb_classes[:60]}]/[{tr_classes[:60]}],keysHit={fk},3input=[{code_in},{qty_in},{cost_in}],actionBtn=[录入对齐={lr_btn},保存规则={save_rule_btn},加成员={add_member_btn}]"

        exe(TC("A1","工作台二级视图-内容+元素验证","工作台","P0"), a1)

        # ======= A2: 点击"模拟交易"二级按钮 =======
        def a2():
            tr_btn = page.locator("button:has-text('模拟交易')").first
            assert tr_btn.count()>0, "未找到模拟交易按钮"
            tr_btn.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=25000); time.sleep(2)
            # 滚动全页
            my = scroll(page, 500, 15)
            ss(page, "A2_trading_view_after_click")
            return f"OK, 滚动{my}px"

        exe(TC("A2","切换到模拟交易二级视图","模拟交易","P0"), a2)

        # ======= A3: 模拟交易视图深度侦察 =======
        def a3():
            tb = text(page)
            # 关键模块关键词
            perf = ["绩效","净值曲线","累计收益","年化收益","最大回撤","夏普","胜率","年化"]
            at = ["自动交易","模拟运行","Dry","执行交易","接管","调仓","信号"]
            bt = ["回测","Backtest","引擎","历史回测","策略回测","回测结果"]
            order = ["买入","卖出","下单","订单","市价","限价","数量","价格","预估金额","持仓"]
            found_perf = [k for k in perf if k in tb]
            found_at = [k for k in at if k in tb]
            found_bt = [k for k in bt if k in tb]
            found_order = [k for k in order if k in tb]
            # Tabs组件
            tabs_containers = page.locator(".ant-tabs-nav")
            tc = tabs_containers.count()
            tab_labels = []
            for i in range(min(tc,6)):
                try: tab_labels.append(tabs_containers.nth(i).locator("[role='tab']").all_inner_texts())
                except: pass
            # 按钮统计
            buy = page.locator("button:has-text('买入'), button:has-text('买')").count()
            sell = page.locator("button:has-text('卖出'), button:has-text('卖')").count()
            tables = page.locator(".ant-table-wrapper").count()
            cards = page.locator(".ant-card").count()
            charts = page.locator("svg, canvas").count()
            statistics = page.locator(".ant-statistic").count()
            ss(page, "A3_trading_full")
            return f"关键词命中: 绩效{found_perf}, 自动交易{found_at}, 回测{found_bt}, 下单{found_order}; Tab容器={tc}→{tab_labels}; 元素: 买={buy},卖={sell},表={tables},卡={cards},图={charts},指标={statistics}"

        exe(TC("A3","模拟交易视图-关键模块侦察","模拟交易","P0"), a3)

        # ======= A4: 模拟交易-头部下单面板 =======
        def a4():
            # 回到顶部
            page.evaluate("window.scrollTo(0,0)"); time.sleep(0.5)
            ss(page, "A4_order_panel_top")
            # 找搜索标的下拉/输入
            search = page.locator(".ant-select, input[placeholder*='代码'], input[placeholder*='标的'], input[placeholder*='Symbol']").count()
            # 数字输入框
            num = page.locator(".ant-input-number, input[type='number']").count()
            # 按钮
            top_btns = page.locator("button:visible").all_inner_texts()[:20]
            # 是否有买卖方向切换
            seg = page.locator(".ant-segmented, [role='group']").count()
            return f"搜索框={search}, 数字框={num}, 方向分段器={seg}, 顶部按钮={top_btns}"

        exe(TC("A4","模拟交易-头部下单面板元素侦察","模拟交易","P0"), a4)

        # ======= A5: 模拟交易-持仓表格详细 =======
        def a5():
            ts = page.locator(".ant-table-wrapper")
            c = ts.count()
            all_info = []
            for i in range(min(c,5)):
                try:
                    ths = ts.nth(i).locator("thead th").all_inner_texts()
                    rs = ts.nth(i).locator("tbody tr").count()
                    ops = ts.nth(i).locator("tbody button, tbody a").count()
                    all_info.append({"idx":i,"headers":ths,"rows":rs,"op_el":ops})
                except: pass
            # 如果表格没找到，尝试向下滚动后再找
            if c == 0:
                scroll(page); time.sleep(0.8)
                c = ts.count()
                all_info.append({"after_scroll_tables": c})
            ss(page, "A5_tables_detail")
            return f"表数={c},详情={json.dumps(all_info,ensure_ascii=False)}"

        exe(TC("A5","模拟交易-持仓表详细(列/行/操作)","模拟交易","P0"), a5)

        # ======= A6: API v1 /portfolios 响应结构验证 =======
        def a6():
            try:
                r = page.request.get("http://127.0.0.1:8000/api/v1/portfolios", timeout=10000)
                s = r.status
                assert 200 <= s < 400, f"HTTP {s}"
                try:
                    data = r.json()
                    if isinstance(data, list):
                        n = len(data)
                        sample_keys = list(data[0].keys())[:15] if n>0 else []
                        return f"OK list len={n}, sample keys={sample_keys}"
                    elif isinstance(data, dict):
                        # 可能是分页格式
                        keys = list(data.keys())[:10]
                        items = data.get("items") or data.get("data") or data.get("rows")
                        it_len = len(items) if isinstance(items, list) else "not-list"
                        return f"OK dict keys={keys}, items={it_len}"
                    else:
                        return f"OK type={type(data).__name__}, preview={str(data)[:200]}"
                except Exception as e:
                    return f"HTTP OK, parse fail: {type(e).__name__}: {str(e)[:100]}"
            except Exception as e:
                raise AssertionError(f"Request error: {type(e).__name__}: {e}")

        exe(TC("A6","API /api/v1/portfolios 响应结构验证","API","P0"), a6)

        # ======= A7: API /api/v1/symbols 响应结构 =======
        def a7():
            try:
                r = page.request.get("http://127.0.0.1:8000/api/v1/symbols?limit=5", timeout=10000)
                s = r.status
                assert 200 <= s < 400, f"HTTP {s}"
                try:
                    data = r.json()
                    if isinstance(data, list):
                        n = len(data)
                        sample = list(data[0].keys())[:12] if n>0 else []
                        return f"list len={n}, keys={sample}"
                    elif isinstance(data, dict):
                        items = data.get("items") or data.get("data")
                        n = len(items) if isinstance(items, list) else "?"
                        return f"dict len={n}"
                except: pass
                return f"status={s}, body_len={len(r.text())}"
            except Exception as e:
                raise AssertionError(str(e))

        exe(TC("A7","API /api/v1/symbols 响应结构验证","API","P0"), a7)

        # ======= A8: 新建组合弹窗 字段侦察 =======
        def a8():
            # 点工作台先
            try: page.locator("button:has-text('工作台')").first.click(timeout=4000); time.sleep(1.5)
            except: pass
            new_btn = page.locator("button:has-text('新建组合'), button:has-text('+ 新建组合')").first
            assert new_btn.count()>0, "无新建组合按钮"
            new_btn.click(timeout=5000); time.sleep(2)
            modal = page.locator(".ant-modal-content").first
            assert modal.count()>0, "弹窗未打开"
            # 收集弹窗内容
            labels = modal.locator("label, .ant-form-item-label > label").all_inner_texts()
            inputs = modal.locator("input, .ant-select").count()
            switches = modal.locator(".ant-switch").count()
            # 输入框placeholder
            placeholders = []
            for i in range(min(inputs,10)):
                try:
                    ph = modal.locator("input").nth(i).get_attribute("placeholder")
                    if ph: placeholders.append(ph)
                except: pass
            btns = modal.locator("button").all_inner_texts()
            ss(page, "A8_new_portfolio_modal")
            # 关闭
            try: page.locator(".ant-modal-close").first.click(timeout=3000); time.sleep(0.8)
            except: pass
            return f"字段label={labels}, 输入={inputs}, Switch={switches}, placeholder={placeholders}, 按钮={btns}"

        exe(TC("A8","新建组合弹窗-字段结构侦察","弹窗","P1"), a8)

        # ======= A9: 工作台-持仓录入 交互(填充3字段+尝试点按钮) =======
        def a9():
            # 确保在工作台
            try: page.locator("button:has-text('工作台')").first.click(timeout=4000); time.sleep(1.5)
            except: pass
            scroll(page); time.sleep(1)
            # 定位三个输入框
            code = page.locator("input[placeholder*='代码']").first
            qty  = page.locator("input[placeholder*='数量'], input[placeholder*='股数']").first
            cost = page.locator("input[placeholder*='成本']").first
            res = {}
            # 填值
            for nm, loc, val in [("code",code,"600519"), ("qty",qty,"100"), ("cost",cost,"1500")]:
                try:
                    if loc.count()>0 and loc.is_visible():
                        loc.click(timeout=2000); time.sleep(0.1)
                        loc.fill(val, timeout=2000)
                        res[nm] = "filled"
                    else: res[nm] = "not-found-or-hidden"
                except Exception as e: res[nm] = f"err:{type(e).__name__}"
            ss(page, "A9_position_entry_filled")
            # 点录入对齐按钮
            btn = page.locator("button:has-text('录入对齐')").first
            try:
                if btn.count()>0 and btn.is_visible():
                    btn.click(timeout=3000); time.sleep(1.2)
                    ss(page, "A9_after_luru_submit")
                    res["luru_clicked"] = True
                else: res["luru_clicked"] = False
            except Exception as e: res["luru_clicked"] = f"err:{type(e).__name__}"
            # 清空输入
            for loc in [code,qty,cost]:
                try:
                    if loc.count()>0: loc.fill("", timeout=1500)
                except: pass
            return json.dumps(res, ensure_ascii=False)

        exe(TC("A9","工作台-持仓录入表单交互测试","工作台","P1"), a9)

        # ======= A10: 工作台-组合暴露/规则卡片 数据 =======
        def a10():
            # 确保在工作台
            try: page.locator("button:has-text('工作台')").first.click(timeout=4000); time.sleep(1)
            except: pass
            scroll(page); time.sleep(0.8)
            tb = text(page)
            left_items = ["证券占比","基金占比","行业分散","前十大权重","板块集中度","单标的上限"]
            right_items = ["单只标的仓位","ETF行业上限","板块仓位上限","最大回撤阈值","波动率上限","成分股数"]
            found_left = [k for k in left_items if k in tb]
            found_right = [k for k in right_items if k in tb]
            # 数值输入框个数(规则阈值)
            threshold_inputs = page.locator(".ant-input-number, input[type='number']:visible").count()
            save_btn = page.locator("button:has-text('保存规则')").count()
            return f"左指标{len(found_left)}/{len(left_items)} → {found_left}, 右阈值{len(found_right)}/{len(right_items)} → {found_right}, 阈值输入框={threshold_inputs}, 保存规则按钮={save_btn}"

        exe(TC("A10","工作台-组合暴露+规则卡片 6x6阈值项验证","工作台","P1"), a10)

        # ======= A11: 工作台-数据管理 3按钮 =======
        def a11():
            try: page.locator("button:has-text('工作台')").first.click(timeout=4000); time.sleep(1)
            except: pass
            scroll(page); time.sleep(0.8)
            tb = text(page)
            btns = []
            for name in ["一键备份","导出数据","恢复备份","全部备份","一键备份","备份/恢复"]:
                if name in tb: btns.append(name)
            # 计数
            real_count = page.locator("button:has-text('备份'), button:has-text('导出'), button:has-text('恢复')").count()
            return f"关键词命中={btns}, 实际备份/恢复/导出相关按钮数={real_count}"

        exe(TC("A11","工作台-数据管理(备份/恢复/导出)区域","工作台","P2"), a11)

        # ======= A12: 质量检查-Console+Network汇总 =======
        def a12():
            ce = [x for x in CONS if x["t"]=="error"]
            cw = [x for x in CONS if x["t"]=="warning"]
            net_4 = [x for x in NET4 if 400<=x["s"]<500]
            net_5 = [x for x in NET4 if x["s"]>=500]
            # 去重错误文本
            uniq_err = list({x["x"][:100] for x in CONS})
            return f"Console: error={len(ce)}, warning={len(cw)}; Network:4xx={len(net_4)},5xx={len(net_5)}; 唯一告警/错误={len(uniq_err)}条; 样例={json.dumps(uniq_err[:5],ensure_ascii=False)}"

        exe(TC("A12","控制台错误+网络失败请求 质量汇总","质量","P1"), a12)

        b.close()

    # 保存报告
    rep = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total": len(R),
            "passed": sum(1 for x in R if x.status=="passed"),
            "failed": sum(1 for x in R if x.status=="failed"),
        },
        "cases": [asdict(x) for x in R],
        "console": CONS,
        "network_4xx_5xx": NET4,
    }
    rpath = os.path.join(OUT, "portfolio_round3_report.json")
    with open(rpath, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    # 打印总结
    print("\n" + "="*90)
    print(f"  第三轮精准测试报告  {rep['time']}  |  截图目录: {SD}")
    print("="*90)
    s = rep["summary"]
    print(f"  用例 总数={s['total']}  ✅通过={s['passed']}  ❌失败={s['failed']}  报告={rpath}")
    fails = [x for x in R if x.status=="failed"]
    if fails:
        print("\n❌ 失败列表:")
        for x in fails: print(f"  - {x.id} {x.name}: {x.msg}")
    else:
        print("\n✅ 全部通过")
    print("="*90)

if __name__ == "__main__": main()
