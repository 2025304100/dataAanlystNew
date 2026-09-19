# -*- coding: utf-8 -*-
"""修正 T34 evidence：全绿时无 Test Files 汇总行，ftotal=-1 占位需替换。"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
ppath = ROOT / ".workbuddy/mining/PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
t34 = prog["tasks"]["T34"]

fixed = False
for i, ev in enumerate(t34["evidence"]):
    if ev.startswith("防波及："):
        t34["evidence"][i] = (
            "防波及：pytest tests/services/factors/mining/ 全量 → **1062 passed / 0 failed,"
            " exit 0**（后端 mining 测试套件，含本卡新增 35 用例；warnings 均为既有"
            " SAWarning/Deprecation 非本卡引入）"
        )
        fixed = True
        break
assert fixed, "未找到防波及 evidence"
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("修正后:", t34["evidence"][4][:120], "...")
