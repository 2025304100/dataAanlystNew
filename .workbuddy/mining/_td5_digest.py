# -*- coding: utf-8 -*-
"""TD5 报告素材 digest：漂移明细 / 字符集分布 / 与 TD1 基线 diff / 列方向 / CHECK 明细。"""
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
J = os.path.join(REPO, ".workbuddy", "mining", "evidence", "schema_drift_audit_2026-09.json")
BASE = os.path.join(REPO, ".workbuddy", "mining", "_td1_drift_baseline.txt")

d = json.loads(open(J, encoding="utf-8").read())
drift = {r["table"]: r for r in d["drift_tables"]}
print("== 漂移表清单（49） ==")
for t in sorted(drift):
    r = drift[t]
    tags = []
    for k in ("missing_orm", "missing_db", "col_db_only", "col_orm_only", "missing_idx",
              "extra_idx", "missing_uq", "missing_fk", "extra_fk", "fk_mismatch",
              "engine", "charset", "missing_check"):
        if r.get(k):
            tags.append(k)
    print(f"{t}: {','.join(tags)}")

print("\n== 列差异方向明细 ==")
for t in sorted(drift):
    r = drift[t]
    if r.get("col_db_only") or r.get("col_orm_only"):
        print(f"{t}: 库多={r['col_db_only']}  ORM多={r['col_orm_only']}")

print("\n== CHECK 约束明细 ==")
for t in sorted(drift):
    r = drift[t]
    if r.get("missing_check"):
        print(f"{t}: {r['missing_check']}")

print("\n== FK 不符明细 ==")
for t in sorted(drift):
    r = drift[t]
    if r.get("fk_mismatch"):
        for m in r["fk_mismatch"]:
            print(f"{t}: {m}")

print("\n== 缺外键明细 ==")
for t in sorted(drift):
    r = drift[t]
    if r.get("missing_fk"):
        print(f"{t}: {[fk[0] for fk in r['missing_fk']]}")

print("\n== 多索引明细 ==")
for t in sorted(drift):
    r = drift[t]
    if r.get("extra_idx"):
        print(f"{t}: {r['extra_idx']}")

print("\n== 字符集/排序规则分布（全库 table_options） ==")
to = d.get("table_options") or {}
coll_count = {}
eng_count = {}
for t, o in sorted(to.items()):
    c = str(o.get("collation") or "?")
    e = str(o.get("engine") or "?").upper()
    coll_count[c] = coll_count.get(c, 0) + 1
    eng_count[e] = eng_count.get(e, 0) + 1
print(f"覆盖 {len(to)} 张")
print("引擎分布:", eng_count)
for c, n in sorted(coll_count.items(), key=lambda kv: -kv[1]):
    tabs = [t for t, o in to.items() if str(o.get("collation") or "?") == c]
    show = ",".join(tabs[:12]) + ("..." if len(tabs) > 12 else "")
    print(f"{c}: {n}  ({show})")

# 与 TD1 基线 diff
txt = open(BASE, encoding="utf-8").read()
base_tables = set(re.findall(r"\[DRIFT\] (\w+)", txt))
now_tables = set(drift)
print("\n== 与 TD1 基线 diff ==")
print("基线 48 张；现在", len(now_tables))
print("新增:", sorted(now_tables - base_tables))
print("消失:", sorted(base_tables - now_tables))
