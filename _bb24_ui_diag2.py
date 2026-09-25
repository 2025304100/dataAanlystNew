# -*- coding: utf-8 -*-
"""诊断2：抓 Step1 前端发出的 factor-mining 请求与失败原因。"""
from playwright.sync_api import sync_playwright

REQS = []
CONSOLE = []

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.on("request", lambda r: REQS.append(("REQ", r.method, r.url[:110]))
         if "/api/v1" in r.url else None)
    p.on("response", lambda r: REQS.append(("RES", r.status, r.url[:110]))
         if "/api/v1" in r.url else None)
    p.on("console", lambda m: CONSOLE.append(f"{m.type}: {m.text[:160]}"))
    p.on("pageerror", lambda e: CONSOLE.append(f"pageerror: {str(e)[:200]}"))
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(15000)
    b.close()

print("=== factor-mining/api requests ===")
for kind, a, u in REQS:
    if "factor-mining" in u or "candidate-pools" in u or "fields" in u:
        print(kind, a, u)
print("=== console tail 25 ===")
for c in CONSOLE[-25:]:
    print(" *", c)
