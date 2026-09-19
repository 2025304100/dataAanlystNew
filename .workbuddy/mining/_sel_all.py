# -*- coding: utf-8 -*-
"""全量选卡：列出所有未开工且依赖已满足的卡（按 phase 排序）。"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"

tasks = json.loads((M / "tasks.json").read_text(encoding="utf-8"))["tasks"]
prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))["tasks"]
status = {k: v.get("status") for k, v in prog.items()}
done = {k for k, v in status.items() if v == "done"}
inprog = {k for k, v in status.items() if v == "in_progress"}

print(f"done={len(done)} in_progress={len(inprog)} total_cards={len(tasks)}")
print("\n== 未开工卡片（status 未登记或 pending）==")
rows = []
for t in tasks:
    cid = t["id"]
    st = status.get(cid, "-")
    if st in ("done", "in_progress"):
        continue
    deps = t.get("deps") or []
    miss = [d for d in deps if d not in done]
    rows.append((t.get("phase", ""), cid, t.get("title", ""), t.get("budget", ""),
                 deps, miss, "READY" if not miss else f"缺{miss}"))
rows.sort()
for phase, cid, title, budget, deps, miss, flag in rows:
    print(f"  {phase:<5} {cid:<5} [{flag:<12}] budget={budget:<3} {title}")

print("\n== READY 卡（依赖满足，可立即开工）==")
ready = [r for r in rows if r[6] == "READY"]
for phase, cid, title, budget, deps, miss, flag in ready:
    print(f"  {phase:<5} {cid:<5} {title}  (budget={budget})")
print("\nREADY 数量:", len(ready))
