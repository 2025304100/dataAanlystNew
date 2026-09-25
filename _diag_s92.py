# -*- coding: utf-8 -*-
"""诊断 S9.2：资源确认弹窗的锁区文本与确认按钮状态。"""
import time

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/api/v1"
lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
print("lock:", (lk.get("miningDomain") or {}).get("busy"))

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(8000)
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "进化参数" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(2500)
    p.locator("[data-evo-submit]").first.click()
    p.wait_for_selector("[data-resource-modal]", timeout=30000)
    p.wait_for_timeout(2500)  # 等锁状态查询返回
    modal = p.locator("[data-resource-modal]").first
    txt = modal.inner_text()
    print("modal text:", txt[:400].replace("\n", " | "))
    btn = p.locator("[data-evo-confirm-submit]")
    print("confirm btn count:", btn.count(),
          "disabled:", btn.first.is_disabled() if btn.count() else "N/A")
    p.screenshot(path=".codex-run/shots-bb24/diag_s92_modal.png", full_page=True)
    b.close()
