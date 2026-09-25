# -*- coding: utf-8 -*-
"""补测 ops2c（修正断言版）：草稿内联反馈 + 经看板弹窗的「重选→成员批量删除」全链。"""
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
        u = r.url
        if "/factor-mining/drafts" in u or "/members" in u:
            API.append((r.request.method, u.split("/api/v1")[-1][:70], r.status))

    p.on("response", on_resp)
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(9000)

    # ══ B. 草稿：内联 [data-mining-draft-note] ══
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "进化参数" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(2000)
    save_btn = p.locator("button:has-text('保存草稿')")
    save_btn.first.click()
    note = ""
    for _ in range(30):
        p.wait_for_timeout(200)
        if p.locator("[data-mining-draft-note]").count():
            note = p.locator("[data-mining-draft-note]").first.inner_text()[:120]
            break
    log("B1 保存草稿→内联反馈条可见(role=status)", bool(note),
        f"note='{note}' API={[a for a in API if 'drafts' in a[1]][:2]}")
    p.screenshot(path=str(SHOTS / "ops2c_draft_note.png"), full_page=True)

    # ══ A. 生成物料 → 看板弹窗 → 重选 → 删除成员 ══
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "候选股票池" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(1500)
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
        p.wait_for_selector("[data-pool-lock-banner]", timeout=240000)
        log("A1 生成物料→锁定", True)
    except Exception:
        log("A1 生成物料→锁定", False, "240s 超时")
        b.close()
        raise SystemExit(0)

    # 锁定态再点「生成挖掘物料」→ 打开看板弹窗（onGenerateClick locked 分支）
    p.locator("[data-pool-generate]").first.click()
    try:
        p.wait_for_selector("[data-pool-analysis-modal]", timeout=15000)
        modal_open = True
    except Exception:
        modal_open = p.locator(".ant-modal").count() > 0
    log("A2 锁定态打开候选池分析看板弹窗", modal_open,
        f"modal={p.locator('.ant-modal').count()} analysis_modal={p.locator('[data-pool-analysis-modal]').count()}")
    p.screenshot(path=str(SHOTS / "ops2c_modal.png"), full_page=True)

    # 弹窗 footer 的「重新选择」
    rs = p.locator("[data-pool-reselect]")
    if rs.count():
        rs.first.click()
        p.wait_for_timeout(900)
        conf = p.locator(".ant-modal .ant-btn-dangerous, .ant-modal .ant-btn-primary, .ant-popover .ant-btn-primary")
        had_confirm = conf.count() > 0
        if had_confirm:
            try:
                conf.first.click(timeout=5000)
            except Exception:  # noqa: BLE001
                pass
        p.wait_for_timeout(2500)
        # 关弹窗（若还可见；resel 后可能已自动关闭，残留 DOM 不可见则跳过）
        close = p.locator(".ant-modal-close")
        if close.count() and close.first.is_visible():
            try:
                close.first.click(timeout=4000)
            except Exception:  # noqa: BLE001
                pass
            p.wait_for_timeout(1000)
        unlocked = p.locator("[data-pool-lock-banner]").count() == 0
        log("A3 看板内「重新选择」+二次确认→退编辑态", unlocked, f"确认弹窗={had_confirm}")
    else:
        unlocked = False
        log("A3 看板内「重新选择」+二次确认→退编辑态", False, "无 reselect")

    if unlocked:
        p.wait_for_timeout(2000)
        checks = p.locator("[data-pool-member-table] input[type='checkbox']")
        n = checks.count()
        log("A4 编辑态成员表加载", n >= 3, f"checkbox={n}")
        if n >= 3:
            checks.nth(1).click()
            checks.nth(2).click()
            cnt_el = p.locator("[data-pool-member-count]")
            cnt_before = cnt_el.first.inner_text() if cnt_el.count() else "?"
            bulk = p.locator("[data-pool-bulk-delete]")
            if bulk.count() and bulk.first.is_enabled():
                bulk.first.click()
                p.wait_for_timeout(900)
                ok_btn = p.locator(".ant-modal .ant-btn-dangerous, .ant-modal .ant-btn-primary, .ant-popconfirm .ant-btn-primary")
                confirmed = False
                for i in range(ok_btn.count()):
                    if ok_btn.nth(i).is_visible():
                        ok_btn.nth(i).click(timeout=5000)
                        confirmed = True
                        break
                if confirmed:
                    for _ in range(20):
                        p.wait_for_timeout(500)
                        if any(a[0] in ("DELETE", "POST") and "members" in a[1] for a in API):
                            break
                cnt_after = cnt_el.first.inner_text() if cnt_el.count() else "?"
                del_api = [a for a in API if "members" in a[1]]
                log("A5 勾选2成员→批量删除（二次确认+API）",
                    confirmed and bool(del_api) and any(s < 300 for _, _, s in del_api),
                    f"count:{cnt_before}→{cnt_after} API={del_api[-2:]}")
                p.screenshot(path=str(SHOTS / "ops2c_after_delete.png"), full_page=True)
            else:
                log("A5 勾选2成员→批量删除（二次确认+API）", False, "批量删除按钮缺失/禁用")
    b.close()

Path(__file__).with_name("bb24_ops2c.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
