# -*- coding: utf-8 -*-
"""T4 续：强杀重启后的锁自愈与旧 run 终态核查（手动完成脚本剩余断言）。"""
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"


def log(case, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=10).json()
md = lk.get("miningDomain") or {}
log("T4.2 重启后锁查询可用", bool(lk), f"busy={md.get('busy')} runId={md.get('runId')}")

# 崩溃时刻距今已过 ~15min；观察 boot 清理是否已回收
released = not md.get("busy")
log("T4.3 崩溃残留锁已被自愈（boot 清理 _reap_stale_mining_runs / patrol）", released,
    f"busy={md.get('busy')} acquiredAt={md.get('acquiredAt')} heartbeatAt={md.get('heartbeatAt')}")

st_old = requests.get(f"{BASE}/factor-mining/runs/31ee6f20ee4b4c4aab3079bcd131ba53", timeout=30).json().get("status")
log("T4.5 崩溃时 running 的旧 run 已转终态", st_old in ("failed", "cancelled", "stalled"),
    f"status={st_old}")

# T4.4 新提交（若锁已放行）
if released:
    pools = requests.get(f"{BASE}/factor-mining/candidate-pools", params={"page_size": 100}, timeout=30).json().get("items", [])
    snap_id = None
    for p in pools:
        x = requests.get(f"{BASE}/factor-mining/candidate-pools/{p.get('id') or p.get('pool_id')}/snapshot/latest", timeout=30)
        if x.status_code == 200 and x.json().get("snapshot_id"):
            snap_id = x.json()["snapshot_id"]
            break
    r2 = requests.post(f"{BASE}/factor-mining/runs", json={
        "candidate_pool_snapshot_id": snap_id,
        "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
        "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
        "target_horizon": 5, "random_seed": 42,
        "evolution_params": {"population_size": 10, "max_generations": 1},
        "filter_config": {"selected_fields": ["close"]},
    }, timeout=120)
    log("T4.4 自愈后新 run 提交成功", r2.status_code == 201, f"status={r2.status_code} {r2.text[:120]}")
    if r2.status_code == 201:
        new_id = r2.json().get("run_id")
        # 观察 1 代小 run 是否自然走完并触发 finalize（DEF-9 复测样本）
        for _ in range(60):
            time.sleep(10)
            s = requests.get(f"{BASE}/factor-mining/runs/{new_id}", timeout=30).json()
            if s.get("status") in ("succeeded", "failed", "cancelled"):
                break
        c = requests.get(f"{BASE}/factor-mining/runs/{new_id}/candidates", params={"page_size": 50}, timeout=30).json()
        items = c.get("items", [])
        graded = [i for i in items if i.get("factor_version_id") or i.get("grade")]
        log("T4.6 小 run 终态后候选 finalize 回填（DEF-9 复验）", bool(graded),
            f"final_status={s.get('status')} 候选={len(items)} 有version/grade={len(graded)}")
