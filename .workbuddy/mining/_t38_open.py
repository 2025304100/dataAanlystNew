# -*- coding: utf-8 -*-
"""T38 开工登记：selfcheck + PROGRESS.json 立 in_progress。

writes 2 项（parallel.py 为**条件性**：结论「不做」时不落地，
依据 not_do「不在无探针数据支撑时盲目实现并行」）。
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
tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-2:]
print("selfcheck exit:", r.returncode)
for ln in tail:
    print("  ", ln)
assert r.returncode == 0, "selfcheck 未通过"

tasks = json.loads((M / "tasks.json").read_text(encoding="utf-8"))
t38 = next(t for t in tasks["tasks"] if t["id"] == "T38")
print("\nT38:", t38["title"], "| phase:", t38["phase"], "| budget:", t38["budget"])
print("not_do:", t38["not_do"])
print("dod:", t38["dod"])
for w in t38["writes"]:
    print(f"  {'EXISTS' if (ROOT / w).exists() else 'NEW   ':<7} {w}")

prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
done = {k for k, v in prog["tasks"].items() if v.get("status") == "done"}
missing = [d for d in t38["deps"] if d not in done]
print("\n未满足依赖:", missing or "NONE")
assert not missing, f"依赖未满足: {missing}"

inprog = {k for k, v in prog["tasks"].items() if v.get("status") == "in_progress"}
print("in_progress:", sorted(inprog) or "none")
overlap = set()
for cid in inprog:
    card = next((t for t in tasks["tasks"] if t["id"] == cid), None)
    if card:
        for w in card.get("writes", []):
            if w in t38["writes"]:
                overlap.add((cid, w))
print("write overlap:", overlap or "NONE")
assert not overlap

assert "T38" not in prog["tasks"], "T38 已有条目"
prog["tasks"]["T38"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T38 -> in_progress @", NOW)
