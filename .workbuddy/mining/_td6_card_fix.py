# -*- coding: utf-8 -*-
"""TD6 卡补丁：reads 补原始文档引用；writes 登记复用的既有测试文件（selfcheck 合规）。幂等。"""
import json

TPATH = r".workbuddy/mining/tasks.json"

DOCS = [
    "docs/因子挖掘系统-系统设计文档-v2.0.md",
    "docs/因子挖掘系统-开发需求文档-落地版.md",
]
TESTS = [
    "tests/test_g1_factor_usage.py",
    "tests/test_backtest_detail_snapshot_contract.py",
]

with open(TPATH, encoding="utf-8") as f:
    tdata = json.load(f)

card = next(t for t in tdata["tasks"] if t.get("id") == "TD6")
reads = card.setdefault("reads", [])
writes = card.setdefault("writes", [])
changed = []
# 粒度纪律 ≤8：__init__.py 实际不改（排除行原样保留，裁决说明进壳 docstring）
DROP = "app/models/__init__.py"
if DROP in writes:
    writes.remove(DROP)
    changed.append("writes-" + DROP)
for d in DOCS:
    if d not in reads:
        reads.append(d)
        changed.append("reads+" + d)
for t in TESTS:
    if t not in writes:
        writes.append(t)
        changed.append("writes+" + t)

if changed:
    with open(TPATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tdata, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("patched:", ", ".join(changed))
else:
    print("TD6 card already compliant, skip")
