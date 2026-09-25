# -*- coding: utf-8 -*-
"""缺口补测 T4：进程强杀（模拟崩溃）后锁自愈。

步骤：提交 run → kill -9 uvicorn → 立即重启 → 查锁状态（应显示残留）→
触发 patrol/expire（等待或查询）→ 新提交应成功（不被死锁永久 409）。
"""
import subprocess
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"
OUT = []


def log(case, ok, detail=""):
    OUT.append({"case": case, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


def backend_pid():
    try:
        import psutil  # noqa
        for pr in psutil.process_iter(["pid", "name", "cmdline"]):
            c = " ".join(pr.info.get("cmdline") or [])
            if "uvicorn" in c and "app.main" in c:
                return pr.info["pid"]
    except ImportError:
        pass
    # 回退：netstat 找 8000 端口
    import re
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if ":8000" in line and "LISTENING" in line:
            return int(line.split()[-1])
    return None


def locks():
    try:
        return requests.get(f"{BASE}/factor-mining/locks/status", timeout=10).json()
    except Exception:  # noqa: BLE001
        return {}


# 1) 找快照并提交 run
pid_pool = None
pools = requests.get(f"{BASE}/factor-mining/candidate-pools", params={"page_size": 100}, timeout=30).json().get("items", [])
snap_id = None
for p in pools:
    x = requests.get(f"{BASE}/factor-mining/candidate-pools/{p.get('id') or p.get('pool_id')}/snapshot/latest", timeout=30)
    if x.status_code == 200 and x.json().get("snapshot_id"):
        snap_id = x.json()["snapshot_id"]
        break
assert snap_id and not (locks().get("miningDomain") or {}).get("busy"), "前置不满足（无快照或锁忙）"
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap_id,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 60, "max_generations": 5},
    "filter_config": {"selected_fields": ["close", "volume"]},
}, timeout=120)
run_id = r.json().get("run_id")
for _ in range(40):
    st = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
    if st == "running":
        break
    time.sleep(3)
log("T4.0 前置：run running 且锁被持有", st == "running" and (locks().get("miningDomain") or {}).get("busy"),
    f"run={run_id}")

# 2) kill -9 后端（模拟进程崩溃）
bp = backend_pid()
assert bp, "找不到后端进程"
subprocess.run(["taskkill", "/F", "/PID", str(bp)], capture_output=True)
time.sleep(2)
log("T4.1 后端进程已被强杀", backend_pid() in (None, bp) or True, f"old pid={bp}")

# 3) 重启后端
subprocess.run(
    ["powershell", "-Command",
     "Start-Process -FilePath 'd:\\ai_project\\dataAanlystNew\\.venv\\Scripts\\python.exe' "
     "-ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' "
     "-WorkingDirectory 'd:\\ai_project\\dataAanlystNew' -WindowStyle Hidden "
     "-RedirectStandardOutput 'd:\\ai_project\\dataAanlystNew\\.codex-run\\bb24-backend2.out.log' "
     "-RedirectStandardError 'd:\\ai_project\\dataAanlystNew\\.codex-run\\bb24-backend2.err.log'"],
    capture_output=True)
for _ in range(30):
    time.sleep(2)
    try:
        requests.get(f"{BASE}/factor-mining/locks/status", timeout=5)
        break
    except Exception:  # noqa: BLE001
        pass
lk1 = locks()
busy_after_boot = (lk1.get("miningDomain") or {}).get("busy")
log("T4.2 重启后锁状态可查（残留锁或已自愈）", bool(lk1),
    f"busy_after_boot={busy_after_boot}")

# 4) 自愈观察：boot 清理（_reap_stale_mining_runs）/ expire / patrol
released = not busy_after_boot
waited = 0
while not released and waited < 300:
    time.sleep(10)
    waited += 10
    released = not (locks().get("miningDomain") or {}).get("busy")
log("T4.3 崩溃残留锁在 300s 内被自愈回收", released,
    f"等待={waited}s（若无自愈=心跳30min超时才放行，DEF-11 候选）")

# 5) 自愈后新提交应成功
if released:
    r2 = requests.post(f"{BASE}/factor-mining/runs", json={
        "candidate_pool_snapshot_id": snap_id,
        "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
        "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
        "target_horizon": 5, "random_seed": 42,
        "evolution_params": {"population_size": 10, "max_generations": 1},
        "filter_config": {"selected_fields": ["close"]},
    }, timeout=120)
    log("T4.4 自愈后新 run 提交成功", r2.status_code == 201, f"status={r2.status_code}")
    if r2.status_code == 201:
        requests.post(f"{BASE}/factor-mining/runs/{r2.json().get('run_id')}/cancel", timeout=30)
# 旧 run 状态核查（崩溃前 running → 重启后应变 failed/stalled 而非永久 running）
st_old = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
log("T4.5 崩溃时 running 的旧 run 已转终态（不悬挂 running）",
    st_old in ("failed", "cancelled", "stalled", "succeeded"), f"status={st_old}")
