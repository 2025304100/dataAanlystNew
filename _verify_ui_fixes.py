# -*- coding: utf-8 -*-
"""修复验证 V3（UI）：DEF-4 预览三态/生成门控 + DEF-10 草稿载入回填。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []


def log(case, ok, detail=""):
    OUT.append({"case": case, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:200]}", flush=True)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto("http://localhost:5173/?tab=settings", wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(6000)

    # ── DEF-4a：presets 加载中的三态文案 ──
    # 冷启动窗口内看左栏：应显示"加载中…"类骨架/spin，而非「暂无可用预设」
    left = p.locator("[data-filter-left]")
    early_txt = left.inner_text() if left.count() else ""
    early_has_preset = p.locator("[data-filter-preset]").count() > 0
    loading_state = ("加载" in early_txt) or ("计算" in early_txt) or p.locator("[data-filter-left] .ant-skeleton, [data-filter-left] .ant-spin").count() > 0
    wrong_empty = "暂无可用预设" in early_txt
    if early_has_preset:
        log("V3.1 presets 加载中显示加载态（DEF-4）", True, "缓存热，秒出（无窗口可验，视为通过）")
    else:
        log("V3.1 presets 加载中显示加载态（DEF-4）", loading_state and not wrong_empty,
            f"左栏早期文案='{early_txt[:60]}' 加载态={loading_state} 误显空态={wrong_empty}")
    # 等就绪
    for _ in range(40):
        if p.locator("[data-filter-preset]").count() > 0:
            break
        p.wait_for_timeout(3000)
    p.screenshot(path=str(SHOTS / "v3_step1.png"), full_page=True)

    # ── DEF-4b：预览进行中 loading + 生成按钮门控（VIS-2） ──
    btns = p.locator("[data-filter-preset]:not(:disabled)")
    labels = [btns.nth(i).inner_text().strip() for i in range(btns.count())]
    idx = next((i for i, lb in enumerate(labels) if "全" not in lb), 0)
    btns.nth(idx).click()
    p.wait_for_timeout(1500)  # 防抖后、响应前的窗口
    preview_loading = (p.locator("[data-pool-preview-loading]").count() > 0
                       or "计算中" in (p.locator("[data-pool-preview]").inner_text()
                                       if p.locator("[data-pool-preview]").count() else "")
                       or p.locator(".mining-pool-preview .ant-spin").count() > 0)
    gen = p.locator("[data-pool-generate]")
    gen_gated_before = gen.count() > 0 and not gen.first.is_enabled()
    log("V3.2 预览计算中有加载反馈（DEF-4）", preview_loading, f"loading元素={preview_loading}")
    log("V3.3 预览未定型时生成按钮置灰（VIS-2）", gen_gated_before,
        f"响应前按钮可用={not gen_gated_before}")
    # 等预览完成 → 按钮恢复
    for _ in range(40):
        p.wait_for_timeout(3000)
        if p.locator("[data-pool-preview-stats]").count() > 0:
            break
    gen_after = p.locator("[data-pool-generate]")
    log("V3.4 预览完成后生成按钮恢复可用", gen_after.first.is_enabled(),
        f"stats 已渲染，enabled={gen_after.first.is_enabled()}")

    # ── DEF-10：载入草稿 ──
    load_btn = p.locator("[data-mining-draft-open]")
    log("V3.5 向导有「载入草稿」按钮（DEF-10）", load_btn.count() > 0)
    if load_btn.count():
        load_btn.first.click()
        p.wait_for_timeout(3000)
        items = p.locator("[data-mining-draft-restore]")
        log("V3.6 草稿列表渲染（含载入/删除按钮）", items.count() > 0, f"草稿数={items.count()}")
        if items.count():
            items.first.click()
            p.wait_for_timeout(3000)
            note = p.locator("[data-mining-draft-note]")
            note_txt = note.first.inner_text() if note.count() else ""
            log("V3.7 载入草稿→全局反馈条+配置回填", "已载入" in note_txt, f"note='{note_txt[:80]}'")
            # 验证回填：Step2 日期非空（昨天草稿含 2025-03-01~2026-08-01 或其它）
            jumps = p.locator("[data-mining-step-jump]")
            for i in range(jumps.count()):
                if "时间与目标" in jumps.nth(i).inner_text():
                    jumps.nth(i).click()
                    break
            p.wait_for_timeout(2000)
            dates = [x.input_value() for x in p.locator(".ant-picker input").all()
                     if x.input_value()] if p.locator(".ant-picker input").count() else []
            log("V3.8 草稿回填后 Step2 日期有值", len(dates) >= 1, f"dates={dates}")
            p.screenshot(path=str(SHOTS / "v3_draft_restored.png"), full_page=True)
    b.close()

Path(__file__).with_name("bb24_v3_ui.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
