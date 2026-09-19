# -*- coding: utf-8 -*-
"""TD3 开工修正：writes 补登记 app/core/db_numeric.py（幂等）。

理由：5 个写入点需要「递归清洗 + allow_nan=False」统一辅助，唯一合理落点是
db_numeric（clean_numeric_fields 只洗顶层标量，metrics_json 实测有嵌套——
wp5_eval_task 的 stress_test 子 dict）。不补登记就得造第 2 个清洗实现，
违反 R28 单一实现铁律（T14/T18 双实现教训）。
"""
import json

PATH = r".workbuddy/mining/tasks.json"
ITEM = "app/core/db_numeric.py"

with open(PATH, encoding="utf-8") as f:
    data = json.load(f)

card = [t for t in data["tasks"] if t["id"] == "TD3"][0]
writes = card.get("writes", [])
if ITEM in writes:
    print("already registered, skip (idempotent)")
else:
    writes.append(ITEM)
    card["writes"] = writes
    with open(PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(PATH, encoding="utf-8") as f:
        chk = json.load(f)
    c2 = [t for t in chk["tasks"] if t["id"] == "TD3"][0]
    assert ITEM in c2["writes"]
    print("TD3 writes += app/core/db_numeric.py")
