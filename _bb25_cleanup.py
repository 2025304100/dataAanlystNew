# -*- coding: utf-8 -*-
"""f7：用新删除端点清理黑盒测试垃圾数据。

范围：
- 池：所有「因子挖掘候选池」(UI 测试产物)、BB24*、旧 BB23*（保留 1 个最新北交所池作回归源）
- 模板：FUZZ-*（T6a fuzz 产物）、BB23-个人模板
- 草稿：BB23-草稿、空名草稿（F7 产物）、T2/ops 测试草稿（按名匹配）
"""
import requests

BASE = "http://127.0.0.1:8000/api/v1"

# ── 池清理 ──
pools = requests.get(f"{BASE}/factor-mining/candidate-pools",
                     params={"page_size": 200}, timeout=30).json().get("items", [])
bj_pools = [p for p in pools if "北交所" in str(p.get("name", ""))]
keep = bj_pools[0] if bj_pools else None  # 列表默认按更新时间倒序，保留最新北交所池
print(f"pools total={len(pools)} keep={keep.get('name') if keep else None}")
deleted = 0
for p in pools:
    name = str(p.get("name", ""))
    pid = p.get("id") or p.get("pool_id")
    if keep and pid == (keep.get("id") or keep.get("pool_id")):
        continue
    if name.startswith(("BB23", "BB24")) or name == "因子挖掘候选池":
        requests.delete(f"{BASE}/factor-mining/candidate-pools/{pid}/snapshot/latest", timeout=30)
        r = requests.delete(f"{BASE}/factor-mining/candidate-pools/{pid}", timeout=30)
        if r.status_code < 300:
            deleted += 1
        else:
            print(" pool fail:", name, r.status_code, r.text[:80])
print("pools deleted:", deleted)

# ── 模板清理 ──
tls = requests.get(f"{BASE}/factor-mining/templates", params={"limit": 200}, timeout=30).json()
items = tls if isinstance(tls, list) else tls.get("items", [])
tdel = 0
for t in items:
    name = str(t.get("name", ""))
    if name.startswith(("FUZZ-", "BB23")):
        tid = t.get("template_id") or t.get("id")
        r = requests.delete(f"{BASE}/factor-mining/templates/{tid}", timeout=30)
        if r.status_code < 300:
            tdel += 1
        else:
            print(" tpl fail:", name, r.status_code, r.text[:80])
print("templates deleted:", tdel)

# ── 草稿清理 ──
drs = requests.get(f"{BASE}/factor-mining/drafts", params={"limit": 200}, timeout=30).json()
dlist = drs if isinstance(drs, list) else drs.get("items", [])
ddel = 0
for d in dlist:
    name = str(d.get("name") or "")
    did = d.get("draft_id") or d.get("id")
    if name.startswith("BB23") or name == "" or name.startswith("FUZZ"):
        r = requests.delete(f"{BASE}/factor-mining/drafts/{did}", timeout=30)
        if r.status_code < 300:
            ddel += 1
        else:
            print(" draft fail:", did, r.status_code, r.text[:80])
print("drafts deleted:", ddel, "; drafts left:", len(dlist) - ddel)
