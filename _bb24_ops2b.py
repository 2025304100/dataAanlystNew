# -*- coding: utf-8 -*-
"""补测 ops2b：草稿保存（实时 toast 捕获）+ 锁定→重选→成员批量删除全链。

用户路径：
  B) Step4 点「保存草稿」→ 100ms 轮询 toast + 监听 POST /drafts 响应
  A) Step1 点「生成挖掘物料」→ 锁定态 → 成员表 checkbox 应禁用（§3.7.3）
     → 点「重新选择」→ 编辑态 → 勾 2 个成员 → 批量删除 → 二次确认 → 计数减少
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []
API = []


def log(step, ok, detail=""):
    OUT.append({"step": step, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {step}  -- {str(detail)[:200]}", flush=True)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()

    def on_resp(r):
        if "/factor-mining/drafts" in r.url or "/members" in r.url:
            API.append((r.request.method, r.url.split("/api/v1")[-1][:60], r.status))

    p.on("response", on_resp)
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(9000)

    # ════ B. 草稿保存（toast 实时捕获）════
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "进化参数" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(2000)
    save_btn = p.locator("button:has-text('保存草稿')")
    toasts = []
    if save_btn.count():
        save_btn.first.click()
        deadline = time.time() + 6
        while time.time() < deadline and not toasts:
            for sel in (".ant-message", ".ant-notification"):
                loc = p.locator(sel)
                for i in range(loc.count()):
                    t = loc.nth(i).inner_text().strip()
                    if t:
                        toasts.append(t[:140])
            p.wait_for_timeout(100)
        log("B1 保存草稿→toast 反馈", bool(toasts),
            f"toast={json.dumps(toasts, ensure_ascii=False)[:200]} API={API}")
    else:
        log("B1 保存草稿→toast 反馈", False, "Step4 无保存草稿按钮")
    draft_apis = [a for a in API if "drafts" in a[1]]
    log("B2 保存草稿发出 POST /drafts 且 2xx", any(m == "POST" and s < 300 for m, _, s in draft_apis),
        str(draft_apis[:3]))
    p.screenshot(path=str(SHOTS / "ops2b_draft.png"), full_page=True)

    # ════ A. 生成物料 → 锁定 → 成员保护 → 重选 → 删除 ════
    # 回 Step1
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "候选股票池" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(1500)
    # 等 presets 就绪后应用一个市值预设（非「全市场」）确保命中≥50
    for _ in range(30):
        if p.locator("[data-filter-preset]:not(:disabled)").count() > 0:
            break
        p.wait_for_timeout(3000)
    btns = p.locator("[data-filter-preset]:not(:disabled)")
    labels = [btns.nth(i).inner_text().strip() for i in range(btns.count())]
    idx = next((i for i, lb in enumerate(labels) if "全" not in lb), 0)
    btns.nth(idx).click()
    # 等 preview 返回（慢接口，轮询至多 100s）
    for _ in range(34):
        p.wait_for_timeout(3000)
        if p.locator("[data-pool-preview-stats]").count() > 0:
            break
    log("A1 应用预设「{}」后预览统计渲染".format(labels[idx] if labels else "?"),
        p.locator("[data-pool-preview-stats]").count() > 0)

    gen = p.locator("[data-pool-generate]")
    if gen.count() and gen.first.is_enabled():
        gen.first.click()
        try:
            p.wait_for_selector("[data-pool-lock-banner]", timeout=240000)
            log("A2 生成物料→锁定横幅（≤4min）", True)
        except Exception:
            log("A2 生成物料→锁定横幅（≤4min）", False, "240s 未锁定")
    else:
        log("A2 生成物料→锁定横幅（≤4min）", False, "按钮不可用")
        b.close()
        raise SystemExit(0)

    # 成员表 checkbox 禁用（锁定态）
    p.wait_for_timeout(3000)
    checks = p.locator("[data-pool-member-table] input[type='checkbox']")
    n = checks.count()
    all_disabled = n > 0 and all(checks.nth(i).is_disabled() for i in range(min(n, 8)))
    log("A3 锁定态成员 checkbox 禁用（§3.7.3）", all_disabled, f"checkbox={n}")
    p.screenshot(path=str(SHOTS / "ops2b_locked.png"), full_page=True)

    # 重新选择 → 编辑态
    rs = p.locator("[data-pool-reselect]")
    if rs.count():
        rs.first.click()
        p.wait_for_timeout(800)
        for sel in (".ant-modal .ant-btn-primary", "button:has-text('确认')"):
            btn = p.locator(sel)
            if btn.count():
                btn.first.click()
                break
        p.wait_for_timeout(2500)
        unlocked = p.locator("[data-pool-lock-banner]").count() == 0
        log("A4 重新选择→退回编辑态", unlocked)
    else:
        log("A4 重新选择→退回编辑态", False, "无 reselect 按钮")
        unlocked = False

    if unlocked:
        checks = p.locator("[data-pool-member-table] input[type='checkbox']")
        n = checks.count()
        if n >= 3:
            checks.nth(1).click()
            checks.nth(2).click()
            cnt_before = p.locator("[data-pool-member-count]").inner_text() if p.locator("[data-pool-member-count]").count() else "?"
            bulk = p.locator("[data-pool-bulk-delete]")
            if bulk.count() and bulk.first.is_enabled():
                bulk.first.click()
                p.wait_for_timeout(900)
                ok_btn = p.locator(".ant-modal .ant-btn-dangerous, .ant-modal .ant-btn-primary")
                confirmed = ok_btn.count() > 0
                if confirmed:
                    ok_btn.first.click()
                    p.wait_for_timeout(3000)
                cnt_after = p.locator("[data-pool-member-count]").inner_text() if p.locator("[data-pool-member-count]").count() else "?"
                log("A5 编辑态勾选2成员→批量删除（含二次确认）",
                    confirmed and str(cnt_before) != str(cnt_after),
                    f"count: {cnt_before} → {cnt_after} 确认弹窗={confirmed} API={API[-3:]}")
                p.screenshot(path=str(SHOTS / "ops2b_after_delete.png"), full_page=True)
            else:
                log("A5 编辑态勾选2成员→批量删除（含二次确认）", False, "批量删除按钮缺失/禁用")
        else:
            log("A5 编辑态勾选2成员→批量删除（含二次确认）", False, f"成员 checkbox 数={n}（成员未加载？）")
    b.close()

Path(__file__).with_name("bb24_ops2b.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
