# -*- coding: utf-8 -*-
"""ops2e：结果页 + 证据抽屉 + 人工调级（用户视角全链）。"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

RUN = "a9a1cd1a33c64f2d9a6d5053d2bd0f61"
SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = []
API = []


def log(step, ok, detail=""):
    OUT.append({"step": step, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {step}  -- {str(detail)[:200]}", flush=True)


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.on("response", lambda r: API.append((r.request.method, r.url.split("/api/v1")[-1][:70], r.status))
         if "/grade" in r.url else None)
    p.goto(f"http://localhost:5173/?tab=settings&run={RUN}",
           wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    p.wait_for_timeout(12000)

    body = p.locator("body").inner_text()
    log("E1 结果页渲染：研究声明置顶",
        ("研究" in body and "声明" in body) or "不构成投资" in body or "回测" in body,
        body[300:520].replace("\n", "|")[:200])
    p.screenshot(path=str(SHOTS / "ops2e_result.png"), full_page=True)

    # 候选列表与等级 chip
    chips = p.locator("[data-grade-chip]")
    log("E2 候选列表渲染（含等级标签）", chips.count() > 0, f"grade-chip数={chips.count()}")
    if chips.count() == 0:
        # 可能需要滚到候选表；再等等
        p.wait_for_timeout(6000)
        chips = p.locator("[data-grade-chip]")
        log("E2b 重试后等级标签", chips.count() > 0, f"数={chips.count()}")

    # 打开证据抽屉
    if chips.count():
        chips.first.click()
        try:
            p.wait_for_selector("[data-evidence-drawer]", timeout=15000)
            drawer_ok = True
        except Exception:  # noqa: BLE001
            drawer_ok = p.locator(".ant-drawer").count() > 0
        log("E3 点等级标签→证据抽屉打开", drawer_ok)
        if drawer_ok:
            dtxt = p.locator("[data-evidence-drawer]").first.inner_text() if p.locator("[data-evidence-drawer]").count() else p.locator(".ant-drawer").first.inner_text()
            has_dims = p.locator("[data-evidence-dimension]").count() > 0 or "维度" in dtxt
            has_walk = "walk" in dtxt.lower() or "样本外" in dtxt or "逐期" in dtxt
            log("E4 抽屉含评级依据/维度明细", has_dims,
                f"维度元素={p.locator('[data-evidence-dimension]').count()} walk_forward={has_walk}")
            p.screenshot(path=str(SHOTS / "ops2e_drawer.png"), full_page=True)

            # 人工调级：先短理由（应被拦截），再长理由（应成功）
            adj = p.locator("[data-evidence-adjust]")
            if adj.count():
                adj.first.click()
                p.wait_for_timeout(1200)
                reason = p.locator("[data-evidence-adjust-reason] textarea, [data-evidence-adjust-reason] input")
                grade_btn = p.locator("[data-evidence-adjust-grade]")
                if grade_btn.count():
                    grade_btn.first.click()
                    p.wait_for_timeout(500)
                    opts = p.locator(".ant-select-item-option, [role='option']")
                    if opts.count():
                        opts.nth(min(1, opts.count() - 1)).click()
                short = "理由太短"
                if reason.count():
                    reason.first.fill(short)
                    p.locator("[data-evidence-adjust-submit]").first.click()
                    p.wait_for_timeout(1500)
                    blocked_ui = p.locator(".ant-form-item-explain, .ant-message").count() > 0
                    short_api = [a for a in API if "manual" in a[1]]
                    log("E5 短理由(<10字)被拦截", (not short_api) or any(s >= 400 for _, _, s in short_api),
                        f"UI提示={blocked_ui} API={short_api[-1] if short_api else '未发请求'}")
                    # 长理由
                    reason.first.fill("黑盒补测：该因子换手率适中且样本外IC稳定，人工判断上调。")
                    p.locator("[data-evidence-adjust-submit]").first.click()
                    for _ in range(15):
                        p.wait_for_timeout(700)
                        if any("manual" in a[1] for a in API):
                            break
                    manual = [a for a in API if "manual" in a[1]]
                    ok2xx = any(s < 300 for _, _, s in manual)
                    log("E6 合法理由人工调级成功", ok2xx, f"API={manual[-2:]}")
                    p.wait_for_timeout(1500)
                    tip = p.locator("[data-evidence-manual-tip]").count()
                    p.screenshot(path=str(SHOTS / "ops2e_manual.png"), full_page=True)
                    log("E7 调级后显示人工提示+季度重评说明", tip > 0, f"manual-tip={tip}")
            else:
                log("E5 短理由(<10字)被拦截", False, "抽屉内无调级入口")
            # 关闭
            cl = p.locator("[data-evidence-close]")
            if cl.count():
                cl.first.click()
                p.wait_for_timeout(800)
    else:
        log("E3 点等级标签→证据抽屉打开", False, "无 grade-chip 可点")

    # 经验库联动（F1 沉淀）
    t = p.locator("text=经验库").first
    if t.count():
        t.click()
        p.wait_for_timeout(2500)
        ex = p.locator("body").inner_text()
        has = any(k in ex for k in ("cs_zscore", "highest", "公式"))
        log("E8 经验库有本 run 沉淀条目", has, ex[200:400].replace("\n", "|")[:180])
        p.screenshot(path=str(SHOTS / "ops2e_experience.png"), full_page=True)
    b.close()

Path(__file__).with_name("bb24_ops2e.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
