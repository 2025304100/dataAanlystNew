# -*- coding: utf-8 -*-
"""修复验证 V4：DEF-9（小 run 自然收官→finalize 回填 grade）+ DEF-3（首代评估中 pause→resume）。"""
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"


def log(case, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
assert not (lk.get("miningDomain") or {}).get("busy"), "锁忙，稍后再试"

pools = requests.get(f"{BASE}/factor-mining/candidate-pools", params={"page_size": 100}, timeout=30).json().get("items", [])
snap = None
# 优先小池（北交所/BB23）：全市场 perf 对比池的 finalize 逐候选 test 段评估极慢
ordered = sorted(pools, key=lambda p: 0 if any(k in str(p.get("name", "")) for k in ("北交所", "BB23", "微盘")) else 1)
for p in ordered:
    x = requests.get(f"{BASE}/factor-mining/candidate-pools/{p.get('id') or p.get('pool_id')}/snapshot/latest", timeout=30)
    if x.status_code == 200 and x.json().get("snapshot_id"):
        snap = x.json()["snapshot_id"]
        print("use pool:", p.get("name"), flush=True)
        break
assert snap, "无快照"

# ══ V4a: DEF-9 —— 不传 finalize_top_k 的小 run，终态后候选应有 version/grade ══
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 10, "max_generations": 1},
    "filter_config": {"selected_fields": ["close"]},
}, timeout=120)
run_a = r.json().get("run_id")
log("V4a.0 提交 10×1 小 run（不传 finalize_top_k）", r.status_code == 201, f"run={run_a}")
st = ""
for _ in range(90):  # ≤15min
    time.sleep(10)
    d = requests.get(f"{BASE}/factor-mining/runs/{run_a}", timeout=30).json()
    st = d.get("status")
    if st in ("succeeded", "failed", "cancelled", "converged"):
        break
c = requests.get(f"{BASE}/factor-mining/runs/{run_a}/candidates", params={"page_size": 100}, timeout=30).json()
items = c.get("items", [])
fv = [x for x in items if x.get("factor_version_id")]
gr = [x for x in items if x.get("grade")]
log("V4a.1 DEF-9：run 终态后候选 finalize 回填（version/grade 非空）",
    st == "succeeded" and bool(fv or gr),
    f"status={st} 候选={len(items)} 有version={len(fv)} 有grade={len(gr)}"
    + (f" 样例grade={gr[0]['grade']}" if gr else ""))

# ══ V4b: DEF-3 —— 首代评估中 pause → resume 分钟级成功 ══
lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
if (lk.get("miningDomain") or {}).get("busy"):
    for _ in range(24):
        time.sleep(5)
        lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
        if not (lk.get("miningDomain") or {}).get("busy"):
            break
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 100, "max_generations": 20},
    "filter_config": {"selected_fields": ["close", "volume"]},
}, timeout=120)
run_b = r.json().get("run_id")
for _ in range(40):
    time.sleep(3)
    if requests.get(f"{BASE}/factor-mining/runs/{run_b}", timeout=30).json().get("status") == "running":
        break
time.sleep(30)  # 让评估深入首代中段
pr = requests.post(f"{BASE}/factor-mining/runs/{run_b}/pause", timeout=30)
log("V4b.1 评估中 pause 成功", pr.status_code == 200 and pr.json().get("status") == "paused",
    f"{pr.status_code} {pr.text[:80]}")
t0 = time.time()
resume_ok = None
for k in range(20):  # ≤5min
    time.sleep(15)
    rr = requests.post(f"{BASE}/factor-mining/runs/{run_b}/resume", timeout=60)
    if rr.status_code == 200:
        resume_ok = int(time.time() - t0)
        break
log("V4b.2 DEF-3：pause 后 resume ≤5min 成功", resume_ok is not None,
    f"pause 后 {resume_ok}s resume=200" if resume_ok is not None else f"20 次重试失败，最后={rr.status_code} {rr.text[:100]}")
# resume 后 run 应回到 queued/running
time.sleep(10)
st_b = requests.get(f"{BASE}/factor-mining/runs/{run_b}", timeout=30).json().get("status")
log("V4b.3 resume 后状态回到运行链", st_b in ("queued", "running"), f"status={st_b}")
requests.post(f"{BASE}/factor-mining/runs/{run_b}/cancel", timeout=30)
for _ in range(24):
    time.sleep(5)
    lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
    if not (lk.get("miningDomain") or {}).get("busy"):
        break
log("V4b.4 收尾 cancel 后锁释放", not (lk.get("miningDomain") or {}).get("busy"))
