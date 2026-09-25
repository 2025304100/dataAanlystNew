# -*- coding: utf-8 -*-
"""T6a 输入 fuzz：模板公式与 filter_config 的异常/恶意输入面。

断言原则（黑盒安全底线）：
- 任何输入不得导致 5xx（服务端健壮性）；
- 任何字符串字段不得引发 SQL 报错回显（注入迹象）；
- 病态公式（深嵌套/除零/超长）不得让接口挂死（超时应给 4xx/降级）；
- 错误响应不得泄漏堆栈/表结构（information of exposure）。
"""
import time

import requests

BASE = "http://127.0.0.1:8000/api/v1"
results = []


def log(case, ok, detail=""):
    results.append((case, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {case}  -- {str(detail)[:220]}", flush=True)


# ── 1. 模板公式 fuzz ──
formulas = {
    "SQL注入串": "close'; DROP TABLE factor_versions; --",
    "脚本注入": "<script>alert(1)</script>",
    "模板注入": "{{7*7}}",
    "深嵌套": "ts_max(" * 60 + "close" + ")" * 60,
    "超长": "close+" * 20000,
    "除零": "div(close, 0)",
    "未知函数": "not_a_function(close, 99)",
    "负窗口": "ts_mean(close, -5)",
    "非数字窗口": "ts_mean(close, 'abc')",
    "空": "",
}
for name, f in formulas.items():
    try:
        r = requests.post(f"{BASE}/factor-mining/templates", json={
            "name": f"FUZZ-{name}", "rule_config": {"formula": f}}, timeout=60)
        ok = r.status_code < 500
        leak = any(k in r.text.lower() for k in ("traceback", "sqlite", "pymysql", "operationalerror", "syntax error at or near"))
        log(f"F1 模板公式[{name}] 无5xx且无泄漏", ok and not leak,
            f"status={r.status_code} leak={leak} body={r.text[:100]}")
        # 成功的畸形模板要清掉（无删除端点的话至少记录）
    except Exception as e:  # noqa: BLE001
        log(f"F1 模板公式[{name}] 无5xx且无泄漏", False, f"exc={e}")

# ── 2. filter_config 类型 fuzz（preview 永不 4xx 契约 + 无 5xx） ──
cfgs = {
    "min为字符串": {"valuation": {"total_market_cap": {"min_value": "abc"}}},
    "负市值": {"valuation": {"total_market_cap": {"min_value": -1e18}}},
    "数组塞爆": {"markets": ["bj"] * 500},
    "未知键": {"nonexistent_field": {"min_value": 1}},
    "嵌套注入": {"valuation": {"total_market_cap; DELETE FROM x": {"min_value": 1}}},
    "window越界": {"liquidity": {"window_days": 99999, "avg_amount": {"min_value": 1}}},
    "布尔混数字": {"exclude_st": 123},
    "null 顶层": None,
}
for name, cfg in cfgs.items():
    t0 = time.time()
    try:
        r = requests.post(f"{BASE}/factor-mining/candidate-pools/preview",
                          json={"filter_config": cfg}, timeout=120)
        el = time.time() - t0
        ok = r.status_code < 500 and el < 90
        log(f"F2 preview[{name}] 无5xx/不挂死", ok,
            f"status={r.status_code} {el:.1f}s body={r.text[:90]}")
    except Exception as e:  # noqa: BLE001
        log(f"F2 preview[{name}] 无5xx/不挂死", False, f"exc={type(e).__name__}")

# ── 3. 成员 keyword / symbol_ids fuzz ──
pools = requests.get(f"{BASE}/factor-mining/candidate-pools", params={"page_size": 5}, timeout=30).json().get("items", [])
pid = (pools[0].get("id") or pools[0].get("pool_id")) if pools else None
if pid:
    for name, kw in [("SQL", "600000' OR '1'='1"), ("百分号", "%"), ("下划线", "_"), ("超长", "a" * 5000)]:
        r = requests.get(f"{BASE}/factor-mining/candidate-pools/{pid}/members",
                         params={"keyword": kw}, timeout=60)
        log(f"F3 members keyword[{name}] 无5xx", r.status_code < 500, f"status={r.status_code}")
    r = requests.post(f"{BASE}/factor-mining/candidate-pools/{pid}/members",
                      json={"symbol_ids": [-1, 0, 2**62]}, timeout=30)
    log("F4 加成员负/超大 symbol_id 无5xx", r.status_code < 500, f"status={r.status_code} {r.text[:90]}")

# ── 4. split-budget / drafts 异常 ──
r = requests.post(f"{BASE}/factor-mining/split-budget", json={
    "start_date": "not-a-date", "end_date": "2026-01-01", "frequency": "daily",
    "target_horizon": 5, "train_ratio": 0.6, "validation_ratio": 0.2}, timeout=30)
log("F5 split-budget 非法日期→4xx 非5xx", 400 <= r.status_code < 500, f"status={r.status_code}")
r = requests.post(f"{BASE}/factor-mining/split-budget", json={
    "start_date": "2025-01-01", "end_date": "2026-01-01", "frequency": "yearly",
    "target_horizon": 5, "train_ratio": 0.6, "validation_ratio": 0.2}, timeout=30)
log("F6 未知频率→4xx 非5xx", 400 <= r.status_code < 500, f"status={r.status_code}")
r = requests.post(f"{BASE}/factor-mining/drafts", json={"name": "", "current_step": 99}, timeout=30)
log("F7 草稿空名/越界step→4xx", 400 <= r.status_code < 500, f"status={r.status_code} {r.text[:80]}")

fails = [c for c, ok in results if not ok]
print(f"\n===== T6a fuzz：{len(results)-len(fails)}/{len(results)} PASS =====")
for c in fails:
    print(" FAIL:", c)
