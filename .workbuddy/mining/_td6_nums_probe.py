# -*- coding: utf-8 -*-
"""枚举 tasks.json 所有任务 writes 里预留的迁移编号 + versions 目录实占编号，找空位。"""
import json
import re
from pathlib import Path

ROOT = Path(r"D:\ai_project\dataAanlystNew")
with open(ROOT / ".workbuddy/mining/tasks.json", encoding="utf-8") as f:
    data = json.load(f)

reserved = {}
for t in data["tasks"]:
    for w in t.get("writes", []):
        m = re.search(r"_0*(\d+)_", w)
        if "alembic/versions" in w and m:
            reserved.setdefault(int(m.group(1)), []).append(t["id"])

on_disk = set()
for p in (ROOT / "alembic/versions").glob("*.py"):
    m = re.search(r"_0*(\d+)_", p.name)
    if m:
        on_disk.add(int(m.group(1)))

out = []
out.append("reserved by tasks.json: " + json.dumps(dict(sorted(reserved.items())), ensure_ascii=False))
out.append("on disk: " + str(sorted(on_disk)))
free = [n for n in range(59, 80) if n not in reserved and n not in on_disk]
out.append("next free (59..79): " + str(free[:5]))
with open(ROOT / ".workbuddy/mining/_td6_nums.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
print("OK")
