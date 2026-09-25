# -*- coding: utf-8 -*-
"""f5 验证：presets/preview 冷/热耗时（ScreeningPanel TTL 缓存生效性）。"""
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"

# presets：后端刚重启，第一次为冷路径
t0 = time.time()
r = requests.get(f"{BASE}/factor-mining/candidate-pools/filter-presets", timeout=300)
cold = time.time() - t0
t0 = time.time()
r2 = requests.get(f"{BASE}/factor-mining/candidate-pools/filter-presets", timeout=300)
warm = time.time() - t0
print(f"presets cold={cold:.2f}s({r.status_code}) warm={warm:.2f}s({r2.status_code})")

cfg = {"valuation": {"total_market_cap": {"min_value": 8e9, "max_value": 5e10}}}
t0 = time.time()
p1 = requests.post(f"{BASE}/factor-mining/candidate-pools/preview",
                   json={"filter_config": cfg}, timeout=300)
pc = time.time() - t0
t0 = time.time()
p2 = requests.post(f"{BASE}/factor-mining/candidate-pools/preview",
                   json={"filter_config": cfg}, timeout=300)
pw = time.time() - t0
hits1 = p1.json().get("hits") if p1.ok else "?"
hits2 = p2.json().get("hits") if p2.ok else "?"
print(f"preview cold={pc:.2f}s hits={hits1} | warm={pw:.2f}s hits={hits2}")
print("PASS f5 缓存生效（热查询 <2s 且两次结果一致）"
      if warm < 2 and pw < 2 and hits1 == hits2 else
      "FAIL f5 缓存未达预期（观察是否面板重建风暴或缓存键不稳定）")
