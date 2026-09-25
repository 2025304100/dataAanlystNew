# -*- coding: utf-8 -*-
"""诊断3：presets 是迟到可恢复还是永久失败。"""
from playwright.sync_api import sync_playwright

res = []
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()

    def on_resp(r):
        if "filter-presets" in r.url:
            res.append((r.status, r.url.split("/api/v1")[-1][:60]))

    p.on("response", on_resp)
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    el = 0
    for step in (10, 20, 30, 30):
        p.wait_for_timeout(step * 1000)
        el += step
        n = p.locator("[data-filter-preset]").count()
        no = p.locator("[data-filter-no-preset]").count()
        print(f"~{el}s presets_btn={n} no_preset_placeholder={no}", flush=True)
        if n > 0:
            break
    print("presets responses:", res)
    p.screenshot(path=".codex-run/shots-bb24/diag_presets_late.png", full_page=True)
    b.close()
