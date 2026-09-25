# -*- coding: utf-8 -*-
"""补测第一步：提交可完整跑完的小 run（20×2 代，finalize_top_k=3）。"""
import requests

BASE = "http://127.0.0.1:8000/api/v1"
pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 100}, timeout=30).json().get("items", [])
snap_id = None
for p in pools:
    pid = p.get("id") or p.get("pool_id")
    r = requests.get(f"{BASE}/factor-mining/candidate-pools/{pid}/snapshot/latest", timeout=30)
    if r.status_code == 200 and r.json().get("snapshot_id"):
        snap_id = r.json()["snapshot_id"]
        print("use pool:", p.get("name"), "snap:", snap_id)
        break
assert snap_id, "no snapshot"
lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
assert not (lk.get("miningDomain") or {}).get("busy"), "lock busy"
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap_id,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 20, "max_generations": 2},
    "finalize_top_k": 3,
    "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
}, timeout=120)
print(r.status_code, r.text[:200])
