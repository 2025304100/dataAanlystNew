# -*- coding: utf-8 -*-
"""ops2d 最小诊断：成员批量删除点击链，记录所有 /factor-mining 请求与弹窗 DOM。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
ALL_REQ = []

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.on("request", lambda r: ALL_REQ.append((r.method, r.url[:120]))
         if "/factor-mining" in r.url else None)
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(9000)

    # 等 presets 就绪→应用非「全」预设（小池更快）再生成物料
    for _ in range(30):
        if p.locator("[data-filter-preset]:not(:disabled)").count() > 0:
            break
        p.wait_for_timeout(3000)
    btns = p.locator("[data-filter-preset]:not(:disabled)")
    labels = [btns.nth(i).inner_text().strip() for i in range(btns.count())]
    idx = next((i for i, lb in enumerate(labels) if "全" not in lb), 0)
    btns.nth(idx).click()
    for _ in range(34):
        p.wait_for_timeout(3000)
        if p.locator("[data-pool-preview-stats]").count() > 0:
            break
    gen = p.locator("[data-pool-generate]")
    gen.first.click()
    try:
        p.wait_for_selector("[data-pool-lock-banner]", timeout=400000)
    except Exception:  # noqa: BLE001
        print("锁定超时，当前请求：", ALL_REQ[-6:])
        b.close()
        raise SystemExit(1)
    print("已锁定")

    # 重选解锁（看板弹窗内）
    p.locator("[data-pool-generate]").first.click()
    p.wait_for_selector("[data-pool-analysis-modal]", timeout=15000)
    p.locator("[data-pool-reselect]").first.click()
    p.wait_for_timeout(800)
    for i in range(p.locator(".ant-modal .ant-btn-dangerous, .ant-modal .ant-btn-primary").count()):
        btn = p.locator(".ant-modal .ant-btn-dangerous, .ant-modal .ant-btn-primary").nth(i)
        if btn.is_visible():
            btn.click(timeout=5000)
            break
    p.wait_for_timeout(2000)
    if p.locator(".ant-modal-close").count() and p.locator(".ant-modal-close").first.is_visible():
        p.locator(".ant-modal-close").first.click(timeout=4000)
    p.wait_for_timeout(2000)
    print("锁定横幅还在吗:", p.locator("[data-pool-lock-banner]").count())

    # 等成员表加载
    for _ in range(20):
        if p.locator("[data-pool-member-table] input[type='checkbox']").count() > 2:
            break
        p.wait_for_timeout(1500)
    checks = p.locator("[data-pool-member-table] input[type='checkbox']")
    n = checks.count()
    print("成员checkbox:", n)
    ALL_REQ.clear()
    checks.nth(1).click()
    checks.nth(2).click()
    p.wait_for_timeout(500)
    bulk = p.locator("[data-pool-bulk-delete]")
    print("bulk存在:", bulk.count(), "enabled:", bulk.first.is_enabled() if bulk.count() else None)
    bulk.first.click()
    p.wait_for_timeout(1200)
    # dump 弹窗结构
    modals = p.locator(".ant-modal")
    print("ant-modal数:", modals.count())
    for i in range(modals.count()):
        m = modals.nth(i)
        if m.is_visible():
            btns = m.locator("button")
            print("  modal", i, "buttons:", [(btns.nth(j).inner_text().strip(),
                                              btns.nth(j).get_attribute("class") or "") for j in range(btns.count())])
            p.screenshot(path=str(SHOTS / "ops2d_confirm_modal.png"), full_page=True)
            # 点「确认/删除」字样的按钮
            clicked = False
            for j in range(btns.count()):
                t = btns.nth(j).inner_text()
                if ("确认" in t or "删除" in t) and btns.nth(j).is_visible():
                    btns.nth(j).click()
                    clicked = True
                    break
            print("  点击确认按钮:", clicked)
    p.wait_for_timeout(4000)
    print("删除后请求:", [r for r in ALL_REQ if "members" in r[1]])
    print("全部新请求:", ALL_REQ[-8:])
    b.close()
