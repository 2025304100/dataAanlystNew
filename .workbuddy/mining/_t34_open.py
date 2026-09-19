# -*- coding: utf-8 -*-
"""T34 开工登记：selfcheck 写权限校验 + PROGRESS.json 立 in_progress。

writes 7 项（6 实现 + 1 测试）：reproduction/{scheduler,mutation,crossover,injection}.py
+ diversity.py + convergence.py + tests/.../test_reproduction.py。
C12 粒度：7 项 ≤8，无需豁免。
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

# ── 1. selfcheck ────────────────────────────────────────────────────
r = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), str(M / "_selfcheck_conflict.py")],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-2:]
print("selfcheck exit:", r.returncode)
for ln in tail:
    print("  ", ln)
assert r.returncode == 0, "selfcheck 未通过，禁止开工"

# ── 2. tasks.json：核对卡片 ──────────────────────────────────────────
tasks = json.loads((M / "tasks.json").read_text(encoding="utf-8"))
t34 = next(t for t in tasks["tasks"] if t["id"] == "T34")
print("\ntasks.json T34:", t34["title"], "| phase:", t34["phase"],
      "| budget:", t34["budget"], "| deps:", t34["deps"])
print("not_do:", t34["not_do"])
print("pitfalls:")
for p in t34["pitfalls"]:
    print("   -", p)
for w in t34["writes"]:
    p = ROOT / w
    print(f"  {'EXISTS' if p.exists() else 'NEW   ':<7} {w}")

# ── 3. 依赖满足性与写冲突 ────────────────────────────────────────────
prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
done = {k for k, v in prog["tasks"].items() if v.get("status") == "done"}
missing = [d for d in t34["deps"] if d not in done]
print("\n未满足依赖:", missing or "NONE")
assert not missing, f"依赖未满足: {missing}"

inprog = {k for k, v in prog["tasks"].items() if v.get("status") == "in_progress"}
print("in_progress cards:", sorted(inprog) or "none")
overlap = set()
for cid in inprog:
    card = next((t for t in tasks["tasks"] if t["id"] == cid), None)
    if not card:
        continue
    for w in card.get("writes", []):
        if w in t34["writes"]:
            overlap.add((cid, w))
print("write overlap:", overlap or "NONE")
assert not overlap, "写权限与在跑卡重叠"

# ── 4. PROGRESS：立 T34 in_progress ──────────────────────────────────
assert "T34" not in prog["tasks"], "T34 已有条目，拒绝重复登记"
prog["tasks"]["T34"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T34 -> in_progress @", NOW)
