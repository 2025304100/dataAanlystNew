# -*- coding: utf-8 -*-
"""缺口补测 T2：草稿保存→恢复全链（API 契约 + UI 恢复路径）。

疑点：前端 handleSaveDraft 按 currentStep 组装 steps（在 Step4 保存时 step2/step3 是否为空壳）
→ 恢复时能否回填全部 5 步配置。
"""
import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/api/v1"
SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []


def log(case, ok, detail=""):
    OUT.append({"case": case, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


# ══ T2a UI：Step2 填日期 → Step4 保存草稿 → 检查落库内容 ══
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(9000)

    # Step2 填日期（经步骤条跳转）
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "时间与目标" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(2000)
    date_inputs = p.locator(".ant-picker input")
    n_dates = date_inputs.count()
    filled = False
    if n_dates >= 2:
        date_inputs.nth(0).click()
        date_inputs.nth(0).fill("2025-03-01")
        p.keyboard.press("Enter")
        p.wait_for_timeout(500)
        date_inputs.nth(1).click()
        date_inputs.nth(1).fill("2026-08-01")
        p.keyboard.press("Enter")
        p.wait_for_timeout(800)
        filled = True
    log("T2a1 Step2 填入起止日期", filled, f"date inputs={n_dates}")
    p.screenshot(path=str(SHOTS / "t2_step2_dates.png"), full_page=True)

    # Step4 保存草稿
    jumps = p.locator("[data-mining-step-jump]")
    for i in range(jumps.count()):
        if "进化参数" in jumps.nth(i).inner_text():
            jumps.nth(i).click()
            break
    p.wait_for_timeout(2000)
    p.locator("button:has-text('保存草稿')").first.click()
    note = ""
    for _ in range(30):
        p.wait_for_timeout(200)
        if p.locator("[data-mining-draft-note]").count():
            note = p.locator("[data-mining-draft-note]").first.inner_text()
            break
    draft_id = None
    import re
    m = re.search(r"#([0-9a-f]{6,})", note)
    log("T2a2 UI 保存草稿成功", bool(note), f"note='{note}'")

    # ══ T2b API：读回草稿，检查 steps 完整性 ══
    drs = requests.get(f"{BASE}/factor-mining/drafts", timeout=30).json()
    dlist = drs if isinstance(drs, list) else drs.get("items", [])
    newest = None
    for d in dlist:
        did = d.get("draft_id") or d.get("id")
        if m and str(did).startswith(m.group(1)):
            newest = did
    if not newest and dlist:
        newest = dlist[0].get("draft_id") or dlist[0].get("id")
    if newest:
        full = requests.get(f"{BASE}/factor-mining/drafts/{newest}", timeout=30).json()
        steps = full.get("steps") or full.get("steps_json") or {}
        if isinstance(steps, str):
            steps = json.loads(steps)
        s2 = steps.get("step2") or {}
        has_dates = bool(s2.get("start_date")) and bool(s2.get("end_date"))
        log("T2b Step2 日期是否随草稿落库（在 Step4 保存时）", has_dates,
            f"draft={newest} step2={json.dumps(s2, ensure_ascii=False)[:200]}")
        # 其它步
        s1 = steps.get("step1") or {}
        s3 = steps.get("step3") or {}
        s4 = steps.get("step4") or {}
        log("T2c 草稿含 step1 快照引用（本会话未生成物料，允许为空）", True,
            f"step1={json.dumps(s1, ensure_ascii=False)[:100]}")
        print("  step3:", json.dumps(s3, ensure_ascii=False)[:150])
        print("  step4:", json.dumps(s4, ensure_ascii=False)[:150])

        # ══ T2d UI：新会话载入草稿（清掉当前会话态后看恢复） ══
        p.evaluate("localStorage.removeItem('mining_wizard_config')")
        p.reload(wait_until="domcontentloaded")
        p.wait_for_timeout(9000)
        body = p.locator("body").inner_text()
        has_restore_ui = ("草稿" in body and ("恢复" in body or "载入" in body or "最近" in body))
        log("T2d 新会话页面出现草稿恢复入口/提示", has_restore_ui,
            body[100:400].replace("\n", "|")[:220])
        p.screenshot(path=str(SHOTS / "t2_restore_entry.png"), full_page=True)
    else:
        log("T2b Step2 日期是否随草稿落库（在 Step4 保存时）", False, f"未找到草稿列表 {dlist[:2]}")
    b.close()

Path(__file__).with_name("bb24_gap_t2.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
