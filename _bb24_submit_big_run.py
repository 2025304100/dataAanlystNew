# -*- coding: utf-8 -*-
"""T5：提交 100×20 大 run（长稳定性观察样本，finalize_top_k=5）。"""
import requests

BASE = "http://127.0.0.1:8000/api/v1"
pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 100}, timeout=30).json().get("items", [])
snap = None
for p in pools:
    x = requests.get(f"{BASE}/factor-mining/candidate-pools/"
                     f"{p.get('id') or p.get('pool_id')}/snapshot/latest", timeout=30)
    if x.status_code == 200 and x.json().get("snapshot_id"):
        snap = x.json()["snapshot_id"]
        break
assert snap, "无快照"
lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
assert not (lk.get("miningDomain") or {}).get("busy"), "锁忙"
r = requests.post(f"{BASE}/factor-mining/runs", json={
    "candidate_pool_snapshot_id": snap,
    "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
    "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
    "target_horizon": 5, "random_seed": 42,
    "evolution_params": {"population_size": 100, "max_generations": 20},
    "finalize_top_k": 5,
    "filter_config": {"selected_fields": ["close", "open", "high", "low", "volume", "amount"]},
}, timeout=120)
print(r.status_code, r.text[:200])
