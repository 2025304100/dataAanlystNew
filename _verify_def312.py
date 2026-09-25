# -*- coding: utf-8 -*-
"""修复验证 V2：DEF-12（大 run 不被看门狗误杀 + touch 生效）& DEF-3（pause→resume 可用）。

流程：
1. 提交 100×20 大 run；
2. 每 60s 采样（≤14 分钟）：status 不得变 STALLED；run.updated_at 应持续刷新（touch 生效）；
3. 首代完成后（gen>=1）执行 pause → 轮询 resume 直至 200（修复后 worker 应在分钟级自止放锁）；
4. resume 成功后 cancel 收尾，验证锁释放与 worker 终止。
"""
import subprocess
import time
from datetime import datetime

import requests

BASE = "http://127.0.0.1:8000/api/v1"
fails = []


def log(case, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)
    if not ok:
        fails.append(case)


def backend_ws_mb():
    out = subprocess.run(
        ["powershell", "-Command",
         "Get-Process -Id (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess "
         "| Select-Object -ExpandProperty WS"],
        capture_output=True, text=True).stdout.strip()
    try:
        return round(int(out) / 1048576)
    except Exception:  # noqa: BLE001
        return -1


lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
assert not (lk.get("miningDomain") or {}).get("busy"), "锁忙，先等待"

pools = requests.get(f"{BASE}/factor-mining/candidate-pools", params={"page_size": 100}, timeout=30).json().get("items", [])
snap = None
for p in pools:
    x = requests.get(f"{BASE}/factor-mining/candidate-pools/{p.get('id') or p.get('pool_id')}/snapshot/latest", timeout=30)
    if x.status_code == 200 and x.json().get("snapshot_id"):
        snap = x.json()["snapshot_id"]
        break
assert snap, "无快照"

r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 100, "max_generations": 20},
    "finalize_top_k": 5,
    "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
}, timeout=120)
run_id = r.json().get("run_id")
log("V2.0 提交 100×20 大 run", r.status_code == 201 and run_id, f"run={run_id}")

t0 = time.time()
first_updated = None
touch_seen = False
gen_advanced = False
paused_done = False
resume_ok_at = None
for i in range(14):
    time.sleep(60)
    d = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json()
    st, gen, upd = d.get("status"), d.get("current_generation"), d.get("updated_at")
    ws = backend_ws_mb()
    el = int(time.time() - t0)
    print(f"[{el:4d}s] status={st} gen={gen} updated_at={upd} ws={ws}MB", flush=True)
    if st == "failed" and d.get("error_code") == "STALLED":
        log("V2.1 大 run 不被看门狗误杀（≥12min）", False, f"{el}s 时被标 STALLED")
        break
    if first_updated and upd and upd != first_updated:
        touch_seen = True
    if not first_updated:
        first_updated = upd
    if (gen or 0) >= 1:
        gen_advanced = True
    # 首代完成后做一次 pause→resume 验证
    if gen_advanced and not paused_done:
        pr = requests.post(f"{BASE}/factor-mining/runs/{run_id}/pause", timeout=30)
        print(f"  pause -> {pr.status_code}", flush=True)
        for k in range(20):  # resume 轮询 ≤10min
            time.sleep(30)
            rr = requests.post(f"{BASE}/factor-mining/runs/{run_id}/resume", timeout=60)
            if rr.status_code == 200:
                resume_ok_at = int(time.time() - t0)
                print(f"  resume OK at +{k*30}s after pause", flush=True)
                break
            print(f"  resume retry {k}: {rr.status_code}", flush=True)
        paused_done = True
        log("V2.2 pause 后 resume 在 10min 内成功（DEF-3）", resume_ok_at is not None,
            f"pause后 {resume_ok_at}s 成功" if resume_ok_at else "20 次重试全 409")
else:
    log("V2.1 大 run 不被看门狗误杀（≥12min）", True, f"14 分钟仍 running；gen={gen}")
log("V2.3 代内 updated_at 刷新（touch_run_progress 生效）", touch_seen,
    f"first={first_updated} last={upd if 'upd' in dir() else '?'}")

# 收尾
requests.post(f"{BASE}/factor-mining/runs/{run_id}/cancel", timeout=30)
released = False
for _ in range(24):
    time.sleep(5)
    lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
    if not (lk.get("miningDomain") or {}).get("busy"):
        released = True
        break
log("V2.4 cancel 后锁释放 ≤2min", released)
ws_end = backend_ws_mb()
print(f"收尾内存 ws={ws_end}MB（对照 cancel 前采样，若持续攀升=僵尸 worker 未停）")
print("V2 fails:", fails or "NONE")
