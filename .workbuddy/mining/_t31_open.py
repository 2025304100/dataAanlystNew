# -*- coding: utf-8 -*-
"""T31 开工登记：writes 补登记（i18n）+ selfcheck + PROGRESS 立 in_progress。"""
import io
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")
M = ROOT / ".workbuddy/mining"
ADD = ["frontend/src/i18n/zh-CN.ts", "frontend/src/i18n/en-US.ts"]

tpath = M / "tasks.json"
data = json.loads(tpath.read_text(encoding="utf-8"))
t31 = next(t for t in data["tasks"] if t["id"] == "T31")
before = list(t31["writes"])
for w in ADD:
    if w not in t31["writes"]:
        t31["writes"].append(w)
tpath.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("writes 补登记:", before, "->", t31["writes"])

os.environ["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
r = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), str(M / "_selfcheck_conflict.py")],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
print("selfcheck exit:", r.returncode)
for ln in [l for l in (r.stdout or "").splitlines() if l.strip()][-2:]:
    print("  ", ln)
assert r.returncode == 0

prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
done = {k for k, v in prog["tasks"].items() if v.get("status") == "done"}
missing = [d for d in t31["deps"] if d not in done]
print("\nT31:", t31["title"], "| phase:", t31["phase"], "| budget:", t31["budget"])
print("not_do:", t31["not_do"])
print("pitfalls:", t31["pitfalls"])
print("未满足依赖:", missing or "NONE")
assert not missing
assert "T31" not in prog["tasks"]

prog["tasks"]["T31"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T31 -> in_progress @", NOW)
