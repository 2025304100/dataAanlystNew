# -*- coding: utf-8 -*-
"""用户视角补测：Step5 运行看板真实按钮操作（pause→resume→stop）。

前置：API 提交一个中等时长 run（60×5 代，足够窗口操作），
UI 经 `?run=<id>` 直达 Step5，依次点击：
  [data-run-pause] → 观察 paused；
  [data-run-resume] → 记录用户实际看到的提示（验证 DEF-3 观感）；
  [data-run-stop] → [data-run-stop-ok] 确认 → 观察状态。
每步截图 + 抓取 antd message/notification 文案。
"""
import json
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000/api/v1"
SHOTS = Path(__file__).parent / ".codex-run" / "shots-bb24"
OUT = {"steps": []}


def log(step, ok, detail=""):
    OUT["steps"].append({"step": step, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {step}  -- {str(detail)[:200]}", flush=True)


# 1) 找可用快照（若无则用北交所池现建）
pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 100}, timeout=30).json().get("items", [])
snap_id = None
for p in pools:
    pid = p.get("id") or p.get("pool_id")
    r = requests.get(f"{BASE}/factor-mining/candidate-pools/{pid}/snapshot/latest", timeout=30)
    if r.status_code == 200 and r.json().get("snapshot_id"):
        snap_id = r.json()["snapshot_id"]
        break
if not snap_id:
    log("pre 无可用快照", False, "跳过")
    raise SystemExit(1)

lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
if (lk.get("miningDomain") or {}).get("busy"):
    log("pre 锁被占用", False, str(lk.get("miningDomain"))[:150])
    raise SystemExit(1)

r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap_id,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 60, "max_generations": 5},
    "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
}, timeout=120)
run_id = r.json().get("run_id")
log("pre 提交 60×5 代 run", r.status_code == 201 and run_id, f"run={run_id}")

for _ in range(40):
    st = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
    if st == "running":
        break
    time.sleep(3)
log("pre run 进入 running", st == "running", f"status={st}")

msgs = []
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    p = b.new_context(viewport={"width": 1600, "height": 950}, locale="zh-CN").new_page()
    p.goto(f"http://localhost:5173/?tab=settings&run={run_id}",
           wait_until="domcontentloaded", timeout=60000)
    p.evaluate("localStorage.setItem('settings_active_section','factor-mining')")
    p.reload(wait_until="domcontentloaded")
    # 等 Step5 看板出现
    try:
        p.wait_for_selector("[data-run-pause],[data-run-resume]", timeout=30000)
        log("U5.1 直达 Step5 运行看板", True)
    except Exception:
        log("U5.1 直达 Step5 运行看板", False, "30s 无 pause/resume 按钮")
    p.screenshot(path=str(SHOTS / "ops_step5_running.png"), full_page=True)
    body = p.locator("body").inner_text()
    for kw in ("资源占用", "每代", "第", "代"):
        pass
    log("U5.2 看板含进度/代数信息", ("代" in body or "进度" in body), body[200:340].replace("\n", "|"))

    # 2) 点「中断」
    if p.locator("[data-run-pause]").count():
        p.locator("[data-run-pause]").first.click()
        p.wait_for_timeout(3000)
        paused = p.locator("[data-run-resume]").count() > 0
        log("U5.3 UI 点中断→出现继续按钮", paused)
        p.screenshot(path=str(SHOTS / "ops_step5_paused.png"), full_page=True)
    else:
        log("U5.3 UI 点中断→出现继续按钮", False, "无 pause 按钮")
        paused = False

    # 3) 点「继续」（DEF-3 观感取证：用户看到什么？）
    def grab_toast():
        texts = []
        for sel in (".ant-message", ".ant-notification", "[data-run-error]",
                    ".ant-modal-body", "[class*='notice']"):
            loc = p.locator(sel)
            for i in range(loc.count()):
                t = loc.nth(i).inner_text().strip()
                if t:
                    texts.append(t[:160])
        return texts

    if paused:
        p.locator("[data-run-resume]").first.click()
        p.wait_for_timeout(2500)
        toast = grab_toast()
        st_now = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
        log("U5.4 UI 点继续→用户可见反馈", True,
            f"run状态={st_now} 提示={json.dumps(toast, ensure_ascii=False)[:280]}")
        p.screenshot(path=str(SHOTS / "ops_step5_resume_toast.png"), full_page=True)
        msgs.append(toast)
        # 再等 30s 重试一次继续（观察锁释放窗口内用户反复点击的体验）
        p.wait_for_timeout(30000)
        if p.locator("[data-run-resume]").count():
            p.locator("[data-run-resume]").first.click()
            p.wait_for_timeout(2500)
            toast2 = grab_toast()
            st2 = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
            log("U5.5 t+30s 再点继续", True,
                f"run状态={st2} 提示={json.dumps(toast2, ensure_ascii=False)[:280]}")
            p.screenshot(path=str(SHOTS / "ops_step5_resume_retry.png"), full_page=True)

    # 4) 点「停止」→ 确认
    if p.locator("[data-run-stop]").count():
        p.locator("[data-run-stop]").first.click()
        p.wait_for_timeout(800)
        confirm = p.locator("[data-run-stop-confirm]").count() > 0
        log("U5.6 UI 停止有二次确认弹窗", confirm)
        if confirm:
            p.locator("[data-run-stop-ok]").first.click()
            p.wait_for_timeout(3000)
            st3 = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
            log("U5.7 确认停止后状态收敛", st3 in ("converged", "cancelled", "failed", "validating"),
                f"status={st3}")
        p.screenshot(path=str(SHOTS / "ops_step5_stopped.png"), full_page=True)
    else:
        log("U5.6 UI 停止有二次确认弹窗", False, "无 stop 按钮")
    b.close()

# 收尾：确保 run 终止、锁释放
requests.post(f"{BASE}/factor-mining/runs/{run_id}/cancel", timeout=30)
for _ in range(30):
    time.sleep(5)
    lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
    if not (lk.get("miningDomain") or {}).get("busy"):
        break
log("post 收尾锁释放", not (lk.get("miningDomain") or {}).get("busy"))
Path(__file__).with_name("bb24_ops_step5.json").write_text(
    json.dumps(OUT, ensure_ascii=False, indent=2), encoding="utf-8")
print("done run_id=", run_id)
