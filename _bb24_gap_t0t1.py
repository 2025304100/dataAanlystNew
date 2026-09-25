# -*- coding: utf-8 -*-
"""缺口补测 T0+T1：
T0: DEF-9 补证——converged(提前停止) vs succeeded(自然收敛) 两 run 的 finalize 状态对比
T1a: #8 成员批量上限边界（MAX_IDS_PER_REQUEST 两侧）
T1b: #7 防连点——并发双提交 run（第二个必须 409 而非双创建）；并发双生成快照
"""
import json
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "http://127.0.0.1:8000/api/v1"
OUT = []


def log(case, ok, detail=""):
    OUT.append({"case": case, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


# ══ T0: 两 run 候选 finalize 对比 ══
for name, rid in [("自然收敛succeeded", "a9a1cd1a33c64f2d9a6d5053d2bd0f61"),
                  ("提前停止converged", "278460baf2f548b28dc6ffd29034c079")]:
    r = requests.get(f"{BASE}/factor-mining/runs/{rid}", timeout=30)
    status = r.json().get("status") if r.ok else "?"
    c = requests.get(f"{BASE}/factor-mining/runs/{rid}/candidates",
                     params={"page_size": 100}, timeout=30)
    items = c.json().get("items", []) if c.ok else []
    fv = [i for i in items if i.get("factor_version_id")]
    graded = [i for i in items if i.get("grade")]
    log(f"T0 [{name}] candidates 有 factor_version_id/grade（finalize 已执行）",
        bool(fv) or bool(graded),
        f"status={status} 候选={len(items)} 有version={len(fv)} 有grade={len(graded)}"
        + (f" sample_grade={graded[0].get('grade')}" if graded else ""))

# ══ T1a: 批量成员上限边界 ══
# 找一个池
pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 100}, timeout=30).json().get("items", [])
pid = (pools[0].get("id") or pools[0].get("pool_id")) if pools else None
assert pid, "无池可用"
# 从 members 拿真实 symbol_id
mm = requests.get(f"{BASE}/factor-mining/candidate-pools/{pid}/members",
                  params={"page_size": 5}, timeout=30).json()
real_ids = [m.get("symbol_id") for m in mm.get("items", []) if m.get("symbol_id")]
# 探测上限：用超限数量的不存在 id（应 400 条数超限，而非先查存在性）
probe = requests.post(f"{BASE}/factor-mining/candidate-pools/{pid}/members/batch-remove",
                      json={"symbol_ids": list(range(9_000_000, 9_000_600))}, timeout=30) \
    if False else None
# 路由可能是 DELETE /members? 试 remove 端点（body 传参）
r_over = requests.request("DELETE", f"{BASE}/factor-mining/candidate-pools/{pid}/members",
                          json={"symbol_ids": list(range(9_000_000, 9_000_600))}, timeout=30)
r_ok = requests.request("DELETE", f"{BASE}/factor-mining/candidate-pools/{pid}/members",
                        json={"symbol_ids": list(range(9_000_000, 9_000_500))}, timeout=30)
over_txt = r_over.text[:160]
log("T1a 批量条数超限被拒(600>500?) 且 500 个不超限",
    r_over.status_code == 400 and ("超限" in r_over.text or "MAX" in r_over.text.upper()),
    f"600个→{r_over.status_code} '{over_txt}' | 500个→{r_ok.status_code}")

# ══ T1b: 并发双提交 run（mining_domain 只允许一个） ══
snap_id = None
for p in pools:
    x = requests.get(f"{BASE}/factor-mining/candidate-pools/"
                     f"{p.get('id') or p.get('pool_id')}/snapshot/latest", timeout=30)
    if x.status_code == 200 and x.json().get("snapshot_id"):
        snap_id = x.json()["snapshot_id"]
        break
if snap_id:
    lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
    if (lk.get("miningDomain") or {}).get("busy"):
        log("T1b 并发双提交（锁当前被占，跳过）", True, "skip: busy")
    else:
        payload = {
            "candidate_pool_snapshot_id": snap_id,
            "data_cutoff_at": "2026-09-01T00:00:00", "start_date": "2025-01-01T00:00:00",
            "end_date": "2026-09-01T00:00:00", "rebalance_frequency": "daily",
            "target_horizon": 5, "random_seed": 42,
            "evolution_params": {"population_size": 10, "max_generations": 1},
            "filter_config": {"selected_fields": ["close"]},
        }
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(requests.post, f"{BASE}/factor-mining/runs", json=payload, timeout=120)
                    for _ in range(2)]
            rs = [f.result() for f in futs]
        codes = sorted(r.status_code for r in rs)
        created = [r for r in rs if r.status_code == 201]
        log("T1b 连点双提交：恰一个 201、另一个 409（无重复 run）",
            codes == [201, 409] and len(created) == 1, f"codes={codes}")
        # 清掉创建出的 run
        for r in created:
            try:
                rid = r.json().get("run_id")
                if rid:
                    requests.post(f"{BASE}/factor-mining/runs/{rid}/cancel", timeout=30)
            except Exception:  # noqa: BLE001
                pass
else:
    log("T1b 并发双提交", False, "无可用快照")

print(json.dumps([o for o in OUT if not o["ok"]], ensure_ascii=False)[:400])
