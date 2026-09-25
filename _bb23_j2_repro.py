# -*- coding: utf-8 -*-
"""J2 专项复现：pause 后 mining_domain 锁释放耗时 & resume 可用窗口定量。

背景：第四轮黑盒 J2 中 pause 后立即 resume 得 409（MINING_DOMAIN_BUSY），
以 5s 间隔重试 150s 仍未成功；而同 run cancel 后锁约 45s 内释放（J4 PASS）。
本脚本定量：pause→锁释放秒数、期间 resume 每次的返回码、cancel 对照。
"""
import json
import time
from datetime import datetime, timezone

import requests

BASE = "http://127.0.0.1:8000/api/v1"


def locks_busy() -> bool:
    r = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
    return bool((r.get("miningDomain") or {}).get("busy"))


# 1) 找 BB23 测试池的最新快照
pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 50}, timeout=30).json()
pool = next((p for p in pools.get("items", []) if p.get("name", "").startswith("BB23")), None)
assert pool, "未找到 BB23 测试池，先跑 _bb23_api_mining.py"
pid = pool.get("id") or pool.get("pool_id")
snap = requests.get(f"{BASE}/factor-mining/candidate-pools/{pid}/snapshot/latest", timeout=30)
assert snap.status_code == 200, f"latest 快照 404：{snap.status_code}"
snap_id = snap.json().get("snapshot_id") or snap.json().get("id")
print(f"pool={pid} snap={snap_id} busy_before={locks_busy()}")

# 2) 提交中等时长 run（种群 60×3 代，保证 pause 时仍在跑）
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap_id,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 60, "max_generations": 3},
    "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
}, timeout=120)
assert r.status_code == 201, r.text[:200]
run_id = r.json()["run_id"]
print("run:", run_id)

# 3) 等进入 running
for _ in range(60):
    st = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
    if st == "running":
        break
    time.sleep(3)
print("status@start:", st)

# 4) pause 并计时锁释放 + resume 重试
t0 = time.time()
rp = requests.post(f"{BASE}/factor-mining/runs/{run_id}/pause", timeout=30)
print(f"pause -> {rp.status_code} {rp.json().get('status')}")
release_s, resume_log = None, []
for i in range(90):  # 最多 180s
    time.sleep(2)
    busy = locks_busy()
    rr = requests.post(f"{BASE}/factor-mining/runs/{run_id}/resume", timeout=30)
    code = rr.status_code
    if busy is False and release_s is None:
        release_s = time.time() - t0
    if code == 200:
        ok = rr.json().get("status")
        resume_log.append((round(time.time() - t0, 1), code, busy))
        print(f"resume SUCCESS at {time.time()-t0:.1f}s status={ok} lock_busy={busy}")
        break
    if i % 5 == 0:
        resume_log.append((round(time.time() - t0, 1), code, busy))
        print(f"t+{time.time()-t0:5.1f}s lock_busy={busy} resume={code}")
print(f"锁释放耗时={release_s and round(release_s,1)}s resume尝试序列={resume_log}")

# 5) 收尾：取消 run 并确认锁回收
requests.post(f"{BASE}/factor-mining/runs/{run_id}/cancel", timeout=30)
for _ in range(40):
    time.sleep(5)
    if not locks_busy():
        break
print("收尾 busy_after_cancel:", locks_busy())
st = requests.get(f"{BASE}/factor-mining/runs/{run_id}", timeout=30).json().get("status")
print("final run status:", st)
