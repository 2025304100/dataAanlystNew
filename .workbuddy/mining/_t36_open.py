# -*- coding: utf-8 -*-
"""T36 开工登记：selfcheck + PROGRESS 立 in_progress。

迁移编号 0059（未占用，链尾 wps_0023_058 / 0058_f1_experience_tables）；
文件名按既有约定 `YYYY_MM_DD_<编号>_wps_0023_<rev>_<name>.py`。
"""
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")
M = ROOT / ".workbuddy/mining"

r = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), str(M / "_selfcheck_conflict.py")],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
print("selfcheck exit:", r.returncode)
for ln in [l for l in (r.stdout or "").splitlines() if l.strip()][-2:]:
    print("  ", ln)
assert r.returncode == 0

tasks_doc = json.loads((M / "tasks.json").read_text(encoding="utf-8"))
t36 = next(t for t in tasks_doc["tasks"] if t["id"] == "T36")
print("\nT36:", t36["title"], "| phase:", t36["phase"], "| budget:", t36["budget"])
print("not_do:", t36["not_do"])
print("writes:", json.dumps(t36["writes"], ensure_ascii=False))

prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
done = {k for k, v in prog["tasks"].items() if v.get("status") == "done"}
missing = [d for d in t36["deps"] if d not in done]
print("未满足依赖:", missing or "NONE")
assert not missing

inprog = {k for k, v in prog["tasks"].items() if v.get("status") == "in_progress"}
print("in_progress:", sorted(inprog) or "none")
overlap = set()
for cid in inprog:
    card = next((t for t in tasks_doc["tasks"] if t["id"] == cid), None)
    if card:
        for w in card.get("writes", []):
            if w in t36["writes"]:
                overlap.add((cid, w))
print("write overlap:", overlap or "NONE")
assert not overlap

assert "T36" not in prog["tasks"], "T36 已有条目"
prog["tasks"]["T36"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T36 -> in_progress @", NOW)
