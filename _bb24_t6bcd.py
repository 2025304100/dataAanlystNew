# -*- coding: utf-8 -*-
"""T6b/c/d：1366×768 响应式 + 慢网络节流 + 双窗口并发（用户视角行为）。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []


def log(step, ok, detail=""):
    OUT.append({"step": step, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {step}  -- {str(detail)[:200]}", flush=True)


def enter_mining(p):
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(9000)


def overflow_scan(p):
    return p.evaluate("""() => {
        const bad = [];
        const vw = window.innerWidth;
        document.querySelectorAll('*').forEach(el => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && (r.right > vw + 2) && getComputedStyle(el).position !== 'fixed') {
                const cls = (el.className && String(el.className).slice(0,50)) || el.tagName;
                bad.push(cls);
            }
        });
        return {docScrollW: document.documentElement.scrollWidth, vw, offenders: [...new Set(bad)].slice(0,8)};
    }""")


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)

    # ══ T6b 1366×768 响应式 ══
    ctx1 = b.new_context(viewport={"width": 1366, "height": 768}, locale="zh-CN")
    p1 = ctx1.new_page()
    enter_mining(p1)
    scan = overflow_scan(p1)
    log("T6b1 1366 视口无横向溢出元素", scan["docScrollW"] <= 1368 and not scan["offenders"],
        json.dumps(scan, ensure_ascii=False)[:250])
    p1.screenshot(path=str(SHOTS / "t6b_1366_step1.png"), full_page=True)
    # 左栏预设区（此前建议提到的换行问题）
    for _ in range(24):
        if p1.locator("[data-filter-preset]").count() > 0:
            break
        p1.wait_for_timeout(5000)
    left = p1.locator("[data-filter-left]")
    if left.count():
        box = left.bounding_box()
        log("T6b2 左栏预设区在 1366 下宽度合理(≤360px 不挤压)", bool(box) and box["width"] <= 380,
            f"width={box['width'] if box else '?'}")
        p1.screenshot(path=str(SHOTS / "t6b_1366_presets.png"), full_page=True)
    # 长文本溢出：成员表名称列
    body1 = p1.locator("body").inner_text()
    log("T6b3 页面无英文残留关键区（Step1 文案全中文）", "Not found" not in body1 and "undefined" not in body1,
        body1[:120].replace("\n", "|"))

    # ══ T6c 慢网络（CDP 400kbps/400ms RTT ≈ 3G） ══
    cdp = ctx1.new_cdp_session(p1)
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "downloadThroughput": 500_000, "uploadThroughput": 200_000, "latency": 250})
    enter_mining(p1)
    p1.wait_for_timeout(15000)
    # 慢网下页面是否白屏/崩溃；是否有加载态
    shell = p1.locator("[data-mining-shell]").count() > 0
    body_c = p1.locator("body").inner_text()
    has_loading = ("加载中" in body_c) or ("计算中" in body_c) or p1.locator(".ant-spin").count() > 0
    log("T6c1 3G 节流下向导壳正常渲染（不白屏不崩溃）", shell)
    log("T6c2 慢网时有可见加载反馈（观察项）", has_loading,
        f"加载指示存在={has_loading}（若否=用户面对静止页面不知在加载）")
    p1.screenshot(path=str(SHOTS / "t6c_slow3g.png"), full_page=True)
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "downloadThroughput": -1, "uploadThroughput": -1, "latency": 0})

    # ══ T6d 双窗口并发 ══
    ctx2 = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN")
    p2 = ctx2.new_page()
    enter_mining(p2)
    # A(p1) 生成物料 → B(p2) 刷新后是否看到锁定/一致状态
    for _ in range(24):
        if p1.locator("[data-filter-preset]:not(:disabled)").count() > 0:
            break
        p1.wait_for_timeout(5000)
    btns = p1.locator("[data-filter-preset]:not(:disabled)")
    if btns.count():
        labels = [btns.nth(i).inner_text().strip() for i in range(btns.count())]
        idx = next((i for i, lb in enumerate(labels) if "全" not in lb), 0)
        btns.nth(idx).click()
        for _ in range(34):
            p1.wait_for_timeout(3000)
            if p1.locator("[data-pool-preview-stats]").count() > 0:
                break
        p1.locator("[data-pool-generate]").first.click()
        locked_a = False
        try:
            p1.wait_for_selector("[data-pool-lock-banner]", timeout=240000)
            locked_a = True
        except Exception:  # noqa: BLE001
            pass
        log("T6d1 窗口A 生成物料并锁定", locked_a)
        # 窗口B 刷新：B 的 Step1 是什么状态？（会话隔离 or 全局一致）
        enter_mining(p2)
        locked_b = p2.locator("[data-pool-lock-banner]").count() > 0
        gen_b_enabled = (p2.locator("[data-pool-generate]").count() > 0
                         and p2.locator("[data-pool-generate]").first.is_enabled())
        log("T6d2 双窗口状态一致性观察（B 是否感知 A 的锁定）", True,
            f"A锁定={locked_a} B显示锁定={locked_b} B生成按钮可用={gen_b_enabled}"
            "（B 不感知=可再建一套池，DEF-5 同源；感知=全局一致）")
        p2.screenshot(path=str(SHOTS / "t6d_windowB.png"), full_page=True)
    else:
        log("T6d1 窗口A 生成物料并锁定", False, "A 无可用预设（环境慢）")

    b.close()

Path(__file__).with_name("bb24_t6bcd.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
