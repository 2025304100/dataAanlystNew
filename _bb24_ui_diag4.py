# -*- coding: utf-8 -*-
"""诊断4：预设/输入后 preview 的 REQ 是否立即发出、响应多久、stats 何时渲染。"""
from playwright.sync_api import sync_playwright

EV = []
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    t0 = {}

    def on_req(r):
        if "candidate-pools/preview" in r.url:
            t0["sent"] = r.timing.get("startTime") if hasattr(r, "timing") else 0
            EV.append(("REQ-preview", r.method))

    def on_resp(r):
        if "candidate-pools/preview" in r.url:
            EV.append(("RES-preview", r.status))

    p.on("request", on_req)
    p.on("response", on_resp)
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    # 等 presets 就绪（至多 90s）
    for _ in range(30):
        p.wait_for_timeout(3000)
        if p.locator("[data-filter-preset]").count() > 0:
            break
    EV.clear()
    # 点一个非「全」的市值预设
    btns = p.locator("[data-filter-preset]:not(:disabled)")
    labels = [btns.nth(i).inner_text().strip() for i in range(btns.count())]
    print("preset labels:", labels)
    target = None
    for i, lb in enumerate(labels):
        if "全" not in lb:
            target = i
            break
    if target is None:
        target = 0
    btns.nth(target).click()
    # 观察 90s：每 5s 记录 stats 是否渲染
    for sec in range(1, 19):
        p.wait_for_timeout(5000)
        stats = p.locator("[data-pool-preview-stats]").count()
        snapshot = list(EV)
        if stats or sec % 6 == 0:
            print(f"t+{sec*5}s stats={stats} events={snapshot}", flush=True)
        if stats:
            txt = p.locator("[data-pool-preview-stats]").first.inner_text()[:120]
            print("stats text:", txt.replace("\n", " | "))
            break
    b.close()
