# -*- coding: utf-8 -*-
"""诊断：载入 BB23-草稿 后 Step2 的 DatePicker 实际值与 wizardConfig 痕迹。"""
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(8000)
    p.locator("[data-mining-draft-open]").first.click()
    p.wait_for_timeout(3000)
    # 找 BB23-草稿 那条（有名字的）
    items = p.locator("[data-mining-draft-restore]")
    n = items.count()
    print("draft rows:", n)
    # 第一行就是 6d132ff1（列表序）
    items.first.click()
    p.wait_for_timeout(4000)
    cur = p.locator("[data-mining-step-state]:visible")
    body = p.locator("body").inner_text()
    print("current step visible? 时间与目标 highlighted:", "已完成" in body or "进行中" in body)
    vals = []
    pk = p.locator(".ant-picker input")
    for i in range(pk.count()):
        vals.append((i, pk.nth(i).input_value(), pk.nth(i).is_visible()))
    print("picker inputs:", vals)
    p.screenshot(path=".codex-run/shots-bb24/diag_restore_step2.png", full_page=True)
    b.close()
