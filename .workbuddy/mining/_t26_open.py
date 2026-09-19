# -*- coding: utf-8 -*-
"""T26 开工双登记：tasks.json 补测试目录写权限（C17）+ PROGRESS.json 立条目。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

# ── 1. tasks.json：T26 writes 补测试目录 ──────────────────────────────
tpath = ROOT / ".workbuddy/mining/tasks.json"
tasks = json.loads(tpath.read_text(encoding="utf-8"))
t26 = next(t for t in tasks["tasks"] if t["id"] == "T26")
changed = False
if "tests/services/factors/experience/" not in t26["writes"]:
    t26["writes"].append("tests/services/factors/experience/")
    changed = True
tpath.write_text(
    json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("tasks.json T26 writes:", t26["writes"])
print("tasks.json changed:", changed)

# ── 2. PROGRESS.json：立 T26 in_progress 条目 ─────────────────────────
ppath = ROOT / ".workbuddy/mining/PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
tasks_sec = prog["tasks"]
assert "T26" not in tasks_sec, "T26 已有条目，拒绝重复登记"
tasks_sec["T26"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
ppath.write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.json T26 ->", tasks_sec["T26"]["status"], "@", NOW)
