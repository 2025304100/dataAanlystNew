#!/usr/bin/env python3
"""TD2 开工登记（幂等）：PROGRESS.json 置 TD4→TD2 in_progress + 自检。"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")

prog_path = HERE / "PROGRESS.json"
prog = json.loads(prog_path.read_text(encoding="utf-8"))
tasks = prog.setdefault("tasks", {})
entry = tasks.get("TD2")
if entry is None:
    tasks["TD2"] = {"status": "in_progress", "agent": "agent-senior-dev", "started_at": now}
    print("[PROGRESS] TD2 已置 in_progress")
elif entry.get("status") == "done":
    print("[PROGRESS] TD2 已 done，跳过")
else:
    entry["status"] = "in_progress"
    entry.setdefault("started_at", now)
    print(f"[PROGRESS] TD2 已存在（{entry.get('status')}），确认 in_progress")

prog["updated_at"] = now
prog["updated_by"] = "agent-senior-dev"
prog_path.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

r = subprocess.run([sys.executable, str(HERE / "_selfcheck_conflict.py")],
                   capture_output=True, text=True, encoding="utf-8")
print("\n".join(r.stdout.strip().splitlines()[-3:]))
if r.returncode != 0:
    sys.exit(f"[FATAL] 自检失败\n{r.stdout}\n{r.stderr}")
