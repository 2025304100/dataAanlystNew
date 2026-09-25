# -*- coding: utf-8 -*-
"""R1.7 诊断：restore-auto 后走 UI 调级链，dump 全部网络请求与按钮状态。"""
import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/api/v1"
REQS = []

# restore auto（还原状态）
c = requests.get(f"{BASE}/factor-mining/runs/360d64c12a854b2a8f81414de3658d5b/candidates",
                 params={"page_size": 3}, timeout=30).json()
cid = c["items"][0]["id"]
r = requests.post(f"{BASE}/factor-mining/candidates/{cid}/grade/restore-auto", timeout=30)
print("restore-auto:", r.status_code, r.text[:120])

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.on("request", lambda rq: REQS.append(("REQ", rq.method, rq.url[-60:]))
         if "/grade" in rq.url else None)
    p.on("response", lambda rq: REQS.append(("RES", rq.status, rq.url[-60:]))
         if "/grade" in rq.url else None)
    p.on("console", lambda m: REQS.append(("CONSOLE", m.type, m.text[:150]))
         if m.type == "error" else None)
    p.on("pageerror", lambda e: REQS.append(("PAGEERROR", "", str(e)[:180])))
    p.goto(f"http://localhost:5173/?tab=settings&run=360d64c12a854b2a8f81414de3658d5b",
           wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(15000)
    ev = p.locator("[data-result-evidence]")
    print("evidence btns:", ev.count())
    ev.first.click()
    p.wait_for_selector("[data-evidence-drawer]", timeout=15000)
    adj = p.locator("[data-evidence-adjust]")
    print("adjust entry:", adj.count())
    adj.first.click()
    p.wait_for_timeout(1000)
    ta = p.locator("textarea[data-evidence-adjust-reason]")
    print("textarea count:", ta.count())
    ta.first.fill("诊断：该因子样本外IC表现稳定，人工复核后调整并留痕。")
    p.wait_for_timeout(800)
    sub = p.locator("[data-evidence-adjust-submit]")
    print("submit disabled after fill:", sub.first.is_disabled())
    sub.first.click(timeout=8000)
    p.wait_for_timeout(4000)
    print("=== grade requests / errors ===")
    for x in REQS:
        print(x)
    b.close()
