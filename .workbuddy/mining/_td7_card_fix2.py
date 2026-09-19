# -*- coding: utf-8 -*-
"""TD7 卡补丁②：g1/backtest_detail 共享测试文件登记进 high_conflict_files（C18）。幂等。"""
import json
from pathlib import Path

P = Path(r"D:\ai_project\dataAanlystNew\.workbuddy\mining\tasks.json")
with open(P, encoding="utf-8") as f:
    tk = json.load(f)
hc = tk.setdefault("high_conflict_files", {})
for f_ in ("tests/test_g1_factor_usage.py", "tests/test_backtest_detail_snapshot_contract.py"):
    lst = hc.setdefault(f_, [])
    if "TD7" not in lst:
        lst.append("TD7")
with open(P, "w", encoding="utf-8") as f:
    json.dump(tk, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("registered:", {k: v for k, v in hc.items() if k.startswith("tests/")})
