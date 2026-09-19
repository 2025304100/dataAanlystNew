# -*- coding: utf-8 -*-
"""TD7 卡补丁：DoD② 引用的防波及测试文件登记进 writes（C17；照 TD6 惯例只跑不改）。幂等。"""
import json
from pathlib import Path

P = Path(r"D:\ai_project\dataAanlystNew\.workbuddy\mining\tasks.json")
with open(P, encoding="utf-8") as f:
    tk = json.load(f)
card = next(t for t in tk["tasks"] if t["id"] == "TD7")
for f_ in ("tests/test_g1_factor_usage.py", "tests/test_backtest_detail_snapshot_contract.py"):
    if f_ not in card["writes"]:
        card["writes"].append(f_)
with open(P, "w", encoding="utf-8") as f:
    json.dump(tk, f, ensure_ascii=False, indent=2)
    f.write("\n")
print("TD7 writes:", len(card["writes"]))
