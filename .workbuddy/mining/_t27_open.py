# -*- coding: utf-8 -*-
"""T27 开工登记：selfcheck 写权限校验 + PROGRESS.json 立 in_progress。

writes 6 项（3 改 3 新建）：Settings.tsx / MiningShell.tsx / types/mining.ts /
api/factorMining.ts / i18n zh-CN.ts / i18n en-US.ts。
C12 粒度：6 项 ≤8，无需豁免。
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

# ── 1. selfcheck：写权限冲突检测（改动前跑，确认不被其他卡占用）────────
r = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), str(M / "_selfcheck_conflict.py")],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-3:]
print("selfcheck exit:", r.returncode)
for ln in tail:
    print("  ", ln)
assert r.returncode == 0, "selfcheck 未通过，禁止开工"

# ── 2. tasks.json：核对 T27 卡片与 writes 声明 ────────────────────────
tasks = json.loads((M / "tasks.json").read_text(encoding="utf-8"))
t27 = next(t for t in tasks["tasks"] if t["id"] == "T27")
print("\ntasks.json T27:", t27["title"], "| phase:", t27["phase"], "| budget:", t27["budget"])
print("not_do:", t27["not_do"])
for w in t27["writes"]:
    p = ROOT / w
    print(f"  {'EXISTS' if p.exists() else 'NEW   ':<7} {w}")

# 冲突对撞检查：本卡 6 项不得与其他 in_progress 卡 writes 重叠
prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
inprog = {k for k, v in prog["tasks"].items() if v.get("status") == "in_progress"}
print("\nin_progress cards:", sorted(inprog) or "none")
overlap = set()
for cid in inprog:
    card = next((t for t in tasks["tasks"] if t["id"] == cid), None)
    if not card:
        continue
    for w in card.get("writes", []):
        if w in t27["writes"]:
            overlap.add((cid, w))
print("write overlap with in_progress cards:", overlap or "NONE")
assert not overlap, "写权限与在跑卡重叠，禁止开工"

# ── 3. PROGRESS.json：立 T27 in_progress ──────────────────────────────
assert "T27" not in prog["tasks"], "T27 已有条目，拒绝重复登记"
prog["tasks"]["T27"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T27 -> in_progress @", NOW)
