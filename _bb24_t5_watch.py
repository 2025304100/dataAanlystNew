# -*- coding: utf-8 -*-
"""T5 观察器：100×20 大 run 长稳定性采样（进度/落库/内存/心跳/MySQL 断连迹象）。"""
import subprocess
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"
RUN = "c61cb96a867b4a809bc8199114677344"


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


t0 = time.time()
last_gen = -1
stall_seconds = 0
for i in range(13):  # 13 × 300s = 65min
    r = requests.get(f"{BASE}/factor-mining/runs/{RUN}", timeout=30).json()
    st, gen = r.get("status"), r.get("current_generation")
    g = requests.get(f"{BASE}/factor-mining/runs/{RUN}/generations", timeout=30).json()
    items = g if isinstance(g, list) else g.get("items", [])
    lk = requests.get(f"{BASE}/factor-mining/locks/status", timeout=30).json()
    hb = (lk.get("miningDomain") or {}).get("heartbeatAt")
    ws = backend_ws_mb()
    print(f"[{int(time.time()-t0)//60:3d}min] status={st} gen={gen} persisted_gens={len(items)} "
          f"ws={ws}MB heartbeat={hb}", flush=True)
    if gen == last_gen:
        stall_seconds += 300
    else:
        stall_seconds = 0
        last_gen = gen
    if st in ("succeeded", "failed", "cancelled", "converged"):
        # 终态：检查 finalize 与结果
        c = requests.get(f"{BASE}/factor-mining/runs/{RUN}/candidates",
                         params={"page_size": 200}, timeout=30).json()
        ci = c.get("items", [])
        fv = [x for x in ci if x.get("factor_version_id")]
        gr = [x for x in ci if x.get("grade")]
        print(f"FINAL: status={st} 候选={len(ci)} 有version={len(fv)} 有grade={len(gr)} "
              f"grades={sorted({x.get('grade') for x in gr})}")
        break
    if stall_seconds >= 1800:
        print("STALL: 30 分钟无代数推进 → 记录停滞证据")
        break
    time.sleep(300)
print("observe end")
