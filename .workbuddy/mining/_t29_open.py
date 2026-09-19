# -*- coding: utf-8 -*-
"""T29 开工登记：writes 补登记 + selfcheck + PROGRESS 立 in_progress。

补登记理由（与 T28 同款）：前端卡文案必须走 t()、translations 要求两份 key
集合严格相等 → 不写 i18n 无法合规交付。
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

ADD = ["frontend/src/i18n/zh-CN.ts", "frontend/src/i18n/en-US.ts"]

tpath = M / "tasks.json"
data = json.loads(tpath.read_text(encoding="utf-8"))
t29 = next(t for t in data["tasks"] if t["id"] == "T29")
before = list(t29["writes"])
for w in ADD:
    if w not in t29["writes"]:
        t29["writes"].append(w)
tpath.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("writes 补登记:", before, "->", t29["writes"])

env_off = {"CODEBUDDY_SAFE_DELETE_ENABLED": "0"}
import os
os.environ.update(env_off)
r = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), str(M / "_selfcheck_conflict.py")],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
print("selfcheck exit:", r.returncode)
for ln in [l for l in (r.stdout or "").splitlines() if l.strip()][-2:]:
    print("  ", ln)
assert r.returncode == 0

prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
done = {k for k, v in prog["tasks"].items() if v.get("status") == "done"}
missing = [d for d in t29["deps"] if d not in done]
print("\nT29:", t29["title"], "| phase:", t29["phase"], "| budget:", t29["budget"])
print("not_do:", t29["not_do"])
print("pitfalls:", t29["pitfalls"])
print("未满足依赖:", missing or "NONE")
assert not missing

inprog = {k for k, v in prog["tasks"].items() if v.get("status") == "in_progress"}
print("in_progress:", sorted(inprog) or "none")
assert "T29" not in prog["tasks"]
prog["tasks"]["T29"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T29 -> in_progress @", NOW)
