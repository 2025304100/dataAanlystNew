# -*- coding: utf-8 -*-
"""设置→因子挖掘 · 浏览器用户视角黑盒（2026-09-24，Playwright）。

以真实用户路径走 5 步向导 + 4 子页签，重点取证：
- U2/U3 Step1 预览统计是否实时渲染（walkthrough/blackbox 脚本稳定失败点）
- 全程 console 报错与 /preview 网络请求（状态码/耗时）
- Step5 中断/继续按钮的真实用户提示文案
截图与结果：.codex-run/shots-bb24/ 、.codex-run/bb24-ui-report.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:5173/?tab=settings"
SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
SHOTS.mkdir(exist_ok=True)
RESULTS: list[dict] = []
CONSOLE_ERR: list[str] = []
NET_LOG: list[dict] = []


def check(case: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"case": case, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:180]}", flush=True)


def shot(page, name: str) -> str:
    p = SHOTS / f"{name}.png"
    try:
        page.screenshot(path=str(p), full_page=False)
    except Exception:  # noqa: BLE001
        return ""
    return str(p)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN")
    page = ctx.new_page()
    page.on("console", lambda m: CONSOLE_ERR.append(m.text) if m.type == "error" else None)

    def on_resp(r):
        if "/api/v1/factor-mining" in r.url:
            entry = {"url": r.url.split("/api/v1")[-1][:70], "status": r.status}
            try:
                entry["ms"] = round(r.header_values("x-process-time-ms") and 0
                                    or (time.time() - r.request.timing.get("requestStart", 0) / 1000) * 1000)
            except Exception:  # noqa: BLE001
                pass
            NET_LOG.append(entry)

    page.on("response", on_resp)

    # ── U0 进入因子挖掘 ──
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
    page.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(6000)
    shell = page.locator("[data-mining-shell]").count() > 0
    check("U0 进入 设置→因子挖掘（4子页签壳）", shell, "向导/批次列表/经验库/模板配置")
    shot(page, "u0_init")

    # ── U1 Step1 渲染（先处理可能存在的锁定态：重新选池解锁） ──
    if page.locator("[data-pool-lock-banner]").count():
        reselect = page.locator("[data-pool-reselect]")
        if reselect.count():
            reselect.first.click()
            page.wait_for_timeout(800)
            # antd Modal 确认
            for sel in (".ant-modal .ant-btn-primary", "button:has-text('确认')"):
                btn = page.locator(sel)
                if btn.count():
                    btn.first.click()
                    break
            page.wait_for_timeout(2000)
        check("U1a 锁定态可通过「重新选择」退回编辑态",
              page.locator("[data-pool-lock-banner]").count() == 0)
    preset_btns = page.locator("[data-filter-preset]")
    # 慢接口等待（冷启动 presets 可达 60s）：轮询至多 90s，记录首屏文案证据
    waited_ms, first_text = 0, ""
    while preset_btns.count() == 0 and waited_ms < 90000:
        if page.locator("[data-filter-no-preset]").count() and not first_text:
            first_text = page.locator("[data-filter-no-preset]").first.inner_text()[:60]
        page.wait_for_timeout(3000)
        waited_ms += 3000
    n_preset = preset_btns.count()
    if waited_ms > 5000:
        check(f"U1c presets 首屏 {waited_ms // 1000}s 才就绪（冷启动性能）", False,
              f"首屏占位文案：'{first_text}'（应显示加载态而非「暂无可用预设」）")
    filter_panel = page.locator("[data-pool-filter-input]").count()
    check("U1 Step1 左栏预设+右栏筛选渲染", n_preset > 0 and filter_panel >= 0,
          f"预设按钮={n_preset} 区间输入={filter_panel}")

    # ── U2 【关键】应用预设 → 预览统计实时出现 ──
    applyable = page.locator("[data-filter-preset]:not(:disabled)").first
    if applyable.count():
        applyable.click()
    NET_LOG.clear()
    appeared = ""
    for waited in (1, 2, 3, 5, 8, 12, 20):
        page.wait_for_timeout(1000 if waited == 1 else (waited - (waited - 1)) * 1000)
        stats = page.locator("[data-pool-preview-stats]").count()
        blocked = page.locator("[data-pool-blocked]").count()
        if stats or blocked:
            appeared = f"stats={stats} blocked={blocked} at ~{waited}s"
            break
    check("U2 应用预设后预览统计/阻断在 20s 内渲染（§3.2 实时反馈）", bool(appeared),
          appeared or "20s 内 [data-pool-preview-stats] 与 [data-pool-blocked] 均未出现")
    preview_reqs = [e for e in NET_LOG if "preview" in e["url"]]
    check("U2b preview 请求已发出且有响应", bool(preview_reqs), str(preview_reqs[:4]))
    shot(page, "u2_after_preset")

    # ── U3 数值输入 → 预览更新（先展开估值 Accordion） ──
    hdr = page.locator(".ant-collapse-header", has_text="估值").first
    if hdr.count():
        try:
            hdr.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
    inp = page.locator("[data-pool-filter-input] input")
    if inp.count():
        NET_LOG.clear()
        inp.first.fill("800")
        page.wait_for_timeout(6000)
        ok3 = page.locator("[data-pool-preview-stats]").count() > 0
        check("U3 输入市值下限后预览统计更新", ok3,
              f"stats={ok3} preview请求={str([e for e in NET_LOG if 'preview' in e['url']])[:120]}")
        shot(page, "u3_input")
    else:
        check("U3 输入市值下限后预览统计更新", False, "未找到区间输入框")

    # ── U4 生成挖掘物料 → 锁定横幅 → 看板 ──
    gen = page.locator("[data-pool-generate]")
    if gen.count() and gen.first.is_enabled():
        gen.first.click()
        locked = page.wait_for_selector("[data-pool-lock-banner]", timeout=90000)
        check("U4 生成物料后出现锁定横幅", locked is not None)
        # 锁定态再点「生成挖掘物料」（按钮态机：文案已改为查看看板类）
        board = None
        for sel in ("text=查看候选池看板", "text=候选池分析", "text=看板"):
            loc = page.locator(sel)
            if loc.count():
                board = loc.first
                break
        if board is not None:
            board.click()
            page.wait_for_timeout(2500)
            modal = (page.locator("[data-pool-analysis-modal]").count()
                     + page.locator(".ant-modal").count())
            check("U5 候选池分析看板弹窗可开", modal > 0, f"modal={modal}")
            shot(page, "u5_board")
            page.keyboard.press("Escape")
        else:
            check("U5 候选池分析看板弹窗可开", False, "无入口按钮")
    else:
        check("U4 生成物料后出现锁定横幅", False, "生成按钮不可用（预览未出数/命中不足）")

    # 下一步
    nxt = page.locator("button:has-text('下一步')")
    if nxt.count():
        nxt.first.click()
        page.wait_for_timeout(1500)

    # ── U6 Step2 日期/频率/切分预算 ──
    in_step2 = page.locator("text=切分预算").count() > 0 or page.locator("[data-step='2']").count() > 0
    check("U6 进入 Step2 且切分预算区渲染", in_step2)
    if in_step2:
        shot(page, "u6_step2")

    # ── U7-U11 子页签巡检 ──
    for tab_name, case in [("批次列表", "U7 批次列表子页签渲染"),
                           ("经验库", "U8 经验库子页签渲染"),
                           ("模板配置", "U9 模板配置子页签渲染")]:
        t = page.locator(f"text={tab_name}").first
        if t.count():
            t.click()
            page.wait_for_timeout(2000)
            body_len = len(page.locator("body").inner_text())
            check(case, body_len > 100, f"内容长度={body_len}")
            shot(page, f"u{case[1:3]}_{tab_name}")
        else:
            check(case, False, "找不到子页签入口")

    # 模板计数专查
    page.wait_for_timeout(1000)
    tpl_txt = page.locator("body").inner_text()
    import re
    m = re.findall(r"(趋势|反转|波动率|估值|质量|量价)", tpl_txt)
    check("U10 模板页按类别分组展示（命中≥4类标签）", len(set(m)) >= 4, f"类别标签={sorted(set(m))}")

    # ── U11 返回向导看 Step5 状态（若批次在跑可看中断/继续文案） ──
    t = page.locator("text=向导").first
    if t.count():
        t.click()
        page.wait_for_timeout(1500)

    browser.close()

print("\n===== console 错误（去重前10） =====")
for e in sorted(set(CONSOLE_ERR))[:10]:
    print(" *", e[:200])
n_pass = sum(1 for r in RESULTS if r["ok"])
print(f"\n===== UI 黑盒：{n_pass}/{len(RESULTS)} PASS =====")
Path(__file__).with_name("bb24_ui_report.json").write_text(
    json.dumps({"results": RESULTS, "console_errors": sorted(set(CONSOLE_ERR))[:30],
                "mining_requests": NET_LOG[-40:]}, ensure_ascii=False, indent=2), encoding="utf-8")
