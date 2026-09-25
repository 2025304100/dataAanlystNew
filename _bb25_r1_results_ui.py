# -*- coding: utf-8 -*-
"""重测 R1：结果页 → 证据抽屉 → 人工调级 → 恢复自动（DEF-9 修复后首次全链）。

用已带等级的 run 360d64c1（10 候选，finalize 回填 D 级）经 ?run= 直达。
"""
import json
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

RUN = "360d64c12a854b2a8f81414de3658d5b"
SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []
API = []

# 前置：把首候选恢复为自动评定（保证 adjust 入口存在，脚本可重复跑）
_c = requests.get(f"http://127.0.0.1:8000/api/v1/factor-mining/runs/{RUN}/candidates",
                  params={"page_size": 5}, timeout=30).json()
for _x in _c.get("items", [])[:1]:
    requests.post(f"http://127.0.0.1:8000/api/v1/factor-mining/candidates/"
                  f"{_x['id']}/grade/restore-auto", timeout=30)


def log(case, ok, detail=""):
    OUT.append({"case": case, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.on("response", lambda r: API.append((r.request.method, r.url.split("/api/v1")[-1][:70], r.status))
         if "/grade" in r.url else None)
    p.goto(f"http://localhost:5173/?tab=settings&run={RUN}",
           wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(15000)

    # ── R1.1 结果页渲染：研究声明置顶 ──
    body = p.locator("body").inner_text()
    has_disclaimer = ("研究用途" in body) or ("不构成任何投资建议" in body) or ("人工复核" in body)
    log("R1.1 结果页研究声明置顶（§8.4/§6.9）", has_disclaimer,
        body[300:560].replace("\n", "|")[:220])
    p.screenshot(path=str(SHOTS / "r1_result_page.png"), full_page=True)

    # ── R1.2 候选列表含等级列 + 证据入口（DEF-15 接线后） ──
    chips = p.locator("[data-grade-chip]")
    log("R1.2 候选列表渲染等级标签", chips.count() > 0, f"grade-chip 数={chips.count()}")
    ev_btns = p.locator("[data-result-evidence]")
    log("R1.2b 结果行有「证据」入口按钮（DEF-15 接线）", ev_btns.count() > 0,
        f"按钮数={ev_btns.count()}")

    if ev_btns.count():
        # ── R1.3 证据抽屉 ──
        ev_btns.first.click()
        drawer_ok = False
        try:
            p.wait_for_selector("[data-evidence-drawer]", timeout=15000)
            drawer_ok = True
        except Exception:  # noqa: BLE001
            drawer_ok = p.locator(".ant-drawer-open").count() > 0
        log("R1.3 点等级标签→证据抽屉打开", drawer_ok)
        if drawer_ok:
            # 等抽屉内容就绪（占位 aside 与真抽屉同属性；GET evidence 可能慢）
            for _ in range(40):
                p.wait_for_timeout(700)
                if p.locator("[data-evidence-grade]").count() or \
                        p.locator("[data-evidence-adjust]").count() or \
                        p.locator("[data-evidence-manual-tip]").count():
                    break
            dtxt = p.locator("[data-evidence-drawer]").first.inner_text()
            dims = p.locator("[data-evidence-dimension]").count()
            log("R1.4 抽屉含评级维度明细", dims > 0 or "维度" in dtxt,
                f"dimension元素={dims} 文本含'维度'={'维度' in dtxt}")
            has_reason = "理由" in dtxt or "为什么" in dtxt or "等级" in dtxt
            log("R1.5 抽屉含定级理由区", has_reason, dtxt[:150].replace("\n", "|"))
            p.screenshot(path=str(SHOTS / "r1_drawer.png"), full_page=True)

            # ── R1.6 人工调级：短理由应被拦截 ──
            adj = p.locator("[data-evidence-adjust]")
            if adj.count():
                adj.first.click()
                p.wait_for_timeout(1500)
                reason = p.locator("[data-evidence-adjust-reason] textarea, textarea[data-evidence-adjust-reason], [data-evidence-adjust-reason] input")
                grade_sel = p.locator("[data-evidence-adjust-grade]")
                if grade_sel.count():
                    grade_sel.first.click()
                    p.wait_for_timeout(600)
                    opts = p.locator(".ant-select-item-option:visible, [role='option']:visible")
                    if opts.count():
                        opts.first.click()
                submit = p.locator("[data-evidence-adjust-submit]")
                if reason.count() and submit.count():
                    reason.first.fill("短理由")
                    p.wait_for_timeout(600)
                    if submit.first.is_disabled():
                        log("R1.6 短理由(<10字)被拦截（UI 或 API 4xx）", True,
                            "UI 层拦截：提交按钮 disabled（canSubmit<10字）")
                    else:
                        submit.first.click()
                        p.wait_for_timeout(2500)
                        short_apis = [a for a in API if "manual" in a[1]]
                        blocked = (not short_apis) or any(s >= 400 for _, _, s in short_apis)
                        log("R1.6 短理由(<10字)被拦截（UI 或 API 4xx）", blocked,
                            f"API={short_apis[-1] if short_apis else '未发请求'}")
                    # ── R1.7 合法理由调级成功 ──
                    reason.first.fill("重测：该因子样本外IC表现稳定，换手率适中，人工复核后维持并留痕。")
                    p.wait_for_timeout(1200)
                    submit.first.click(timeout=15000)
                    for _ in range(15):
                        p.wait_for_timeout(700)
                        if any("manual" in a[1] for a in API):
                            break
                    manual = [a for a in API if "manual" in a[1]]
                    ok2xx = any(s < 300 for _, _, s in manual)
                    log("R1.7 合法理由人工调级成功", ok2xx, f"API={manual[-2:]}")
                    p.wait_for_timeout(2000)
                    tip = p.locator("[data-evidence-manual-tip]").count()
                    log("R1.8 调级后显示人工提示+季度重评说明", tip > 0, f"manual-tip={tip}")
                    p.screenshot(path=str(SHOTS / "r1_manual.png"), full_page=True)
                    # ── R1.9 恢复自动 ──
                    restore = p.locator("[data-evidence-restore-auto]")
                    if restore.count():
                        restore.first.click()
                        for _ in range(10):
                            p.wait_for_timeout(700)
                            auto_apis = [a for a in API if "auto" in a[1] or "restore" in a[1]]
                            if auto_apis:
                                break
                        auto_apis = [a for a in API if "auto" in a[1] or "restore" in a[1]]
                        log("R1.9 恢复自动评定成功（状态还原）", any(s < 300 for _, _, s in auto_apis),
                            f"API={auto_apis[-1] if auto_apis else '无'}")
                    else:
                        log("R1.9 恢复自动评定成功（状态还原）", False, "无恢复按钮")
            else:
                log("R1.6 短理由(<10字)被拦截（UI 或 API 4xx）", False, "抽屉无调级入口")
        # 关闭抽屉
        cl = p.locator("[data-evidence-close]")
        if cl.count():
            cl.first.click()
            p.wait_for_timeout(800)

    # ── R1.10 经验库 F1 沉淀 ──
    t = p.locator("text=经验库").first
    if t.count():
        t.click()
        p.wait_for_timeout(3000)
        ex = p.locator("body").inner_text()
        # 该 run 候选公式特征（cs_zscore/ts_delta 等）是否出现在经验库
        has_entries = any(k in ex for k in ("ts_delta_bars", "cs_zscore", "close/mean", "D 级", "D级"))
        log("R1.10 经验库有成功 run 的沉淀条目", has_entries,
            ex[200:500].replace("\n", "|")[:220])
        p.screenshot(path=str(SHOTS / "r1_experience.png"), full_page=True)

    b.close()

Path(__file__).with_name("bb25_r1_results.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
