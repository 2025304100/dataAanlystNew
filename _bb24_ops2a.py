# -*- coding: utf-8 -*-
"""补测 ops2a：草稿保存/恢复 UI + 锁定态成员表保护。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []


def log(step, ok, detail=""):
    OUT.append({"step": step, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {step}  -- {str(detail)[:200]}", flush=True)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(8000)

    # ── A. 锁定态成员表保护（§3.7.3：锁定后不得可改） ──
    locked = p.locator("[data-pool-lock-banner]").count() > 0
    log("A1 Step1 恢复锁定态（存在已锁快照的池时）", locked,
        "banner=" + str(locked))
    if locked:
        checks = p.locator("[data-pool-member-table] input[type='checkbox']")
        n = checks.count()
        disabled_all = all(checks.nth(i).is_disabled() for i in range(min(n, 5))) if n else None
        bulk = p.locator("[data-pool-bulk-delete]")
        bulk_off = (bulk.count() == 0) or bulk.first.is_disabled()
        log("A2 锁定态成员复选框全部禁用", bool(n) and disabled_all,
            f"checkbox数={n} 全禁用={disabled_all}")
        log("A3 锁定态批量删除不可用", bulk_off,
            f"按钮存在={bulk.count()} 禁用={None if bulk.count()==0 else bulk.first.is_disabled()}")
        p.screenshot(path=str(SHOTS / "ops2a_locked_members.png"), full_page=True)

    # ── B. 跳转到 Step4 保存草稿 ──
    jumps = p.locator("[data-mining-step-jump]")
    jumped = False
    for i in range(jumps.count()):
        label = jumps.nth(i).inner_text()
        if "进化参数" in label:
            jumps.nth(i).click()
            jumped = True
            break
    p.wait_for_timeout(2500)
    log("B1 步骤条跳转到 Step4", jumped)
    save_btn = p.locator("button:has-text('保存草稿')")
    if save_btn.count() == 0:
        # Step4 面板可能要求先完成上游；记录实际文案
        body4 = p.locator("[data-mining-step-panel]").first.inner_text()[:150] if p.locator("[data-mining-step-panel]").count() else ""
        log("B2 Step4 有保存草稿按钮", False, f"面板文案={body4}")
    else:
        save_btn.first.click()
        p.wait_for_timeout(3000)
        toasts = []
        for sel in (".ant-message", ".ant-notification"):
            loc = p.locator(sel)
            for i in range(loc.count()):
                t = loc.nth(i).inner_text().strip()
                if t:
                    toasts.append(t[:120])
        saved = any("草稿" in t or "已保存" in t or "draft" in t.lower() for t in toasts)
        log("B2 点保存草稿→有成功反馈", saved, f"toast={json.dumps(toasts, ensure_ascii=False)[:220]}")
        p.screenshot(path=str(SHOTS / "ops2a_draft_saved.png"), full_page=True)

        # 刷新后是否可恢复（草稿列表/提示）
        p.reload(wait_until="domcontentloaded")
        p.wait_for_timeout(8000)
        body = p.locator("body").inner_text()
        restored = ("草稿" in body) or ("恢复" in body)
        log("B3 刷新后页面有草稿/恢复痕迹", restored, body[400:560].replace("\n", "|"))

    b.close()

Path(__file__).with_name("bb24_ops2a.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
