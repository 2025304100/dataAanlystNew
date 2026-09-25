# -*- coding: utf-8 -*-
"""诊断：Step1 实际渲染的 DOM 探针与文案。"""
import re
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(9000)
    txt = p.locator("body").inner_text()
    print("=== BODY 800 ===")
    print(txt[:800].replace("\n", " | "))
    attrs = set(re.findall(r"data-(?:pool|filter|mining)-[a-z-]+", p.content()))
    print("=== probes ===")
    print(sorted(attrs))
    p.screenshot(path=".codex-run/shots-bb24/diag_step1.png", full_page=True)
    b.close()
