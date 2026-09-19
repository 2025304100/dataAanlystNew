# -*- coding: utf-8 -*-
"""T26 C12 粒度豁免：第 9 产物 tests/services/factors/experience/ 是 DoD 强制登记项。"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent

tpath = ROOT / ".workbuddy/mining/tasks.json"
tasks = json.loads(tpath.read_text(encoding="utf-8"))
t26 = next(t for t in tasks["tasks"] if t["id"] == "T26")
t26["granularity_exempt"] = True
t26["granularity_note"] = (
    "第 9 产物 tests/services/factors/experience/ 为 DoD 命令直接引用的测试目录"
    "（C17 强制登记），非新增决策面；其余 8 产物维持原粒度不变。"
)
tpath.write_text(
    json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("T26 granularity_exempt set; writes:", len(t26["writes"]))
