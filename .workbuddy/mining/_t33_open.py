# -*- coding: utf-8 -*-
"""T33 开工登记：selfcheck 通过后 PROGRESS.json 立 in_progress 条目。

tasks.json 的 T33 writes/reads 在排期生成时已完整（3 个 selection 模块 + 1 个
测试文件，共 4 项 ≤8，无需 C12 粒度豁免），无需改动。
"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

# ── 1. tasks.json：核对 T33 卡与 writes（只读校验，不改） ────────────
tpath = ROOT / ".workbuddy/mining/tasks.json"
tasks = json.loads(tpath.read_text(encoding="utf-8"))
t33 = next(t for t in tasks["tasks"] if t["id"] == "T33")
assert t33["writes"] == [
    "app/services/factors/mining/selection/track.py",
    "app/services/factors/mining/selection/multi_objective.py",
    "app/services/factors/mining/selection/tournament.py",
    "tests/services/factors/mining/test_selection.py",
], f"T33 writes 与预期不符: {t33['writes']}"
print("tasks.json T33 card OK, deps =", t33.get("deps"))

# ── 2. PROGRESS.json：立 T33 in_progress 条目 ─────────────────────────
ppath = ROOT / ".workbuddy/mining/PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
tasks_sec = prog["tasks"]
assert "T33" not in tasks_sec, "T33 已有条目，拒绝重复登记"
tasks_sec["T33"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
ppath.write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.json T33 ->", tasks_sec["T33"]["status"], "@", NOW)
