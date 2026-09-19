# -*- coding: utf-8 -*-
"""T28 开工登记：writes 补登记 + selfcheck + PROGRESS 立 in_progress。

**writes 补登记说明（重要）**：
原卡 writes 仅 `frontend/src/components/factors/mining/wizard/step1/`（目录）。
但本卡是前端卡，需求 §3.4 / 开发 §6 要求「所有文案必须走 t()、禁止硬编码中文」，
且 `translations.test.ts` 要求中英 key 集合严格相等——**不写 i18n 两份无法合规交付**。
故按 T27 同款口径补登记：
    frontend/src/i18n/zh-CN.ts
    frontend/src/i18n/en-US.ts
测试文件放 step1/ 目录内（MiningPoolStep.test.tsx），不越出 writes 声明。
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

# ── 1. 补登记 writes ─────────────────────────────────────────────────
tpath = M / "tasks.json"
data = json.loads(tpath.read_text(encoding="utf-8"))
t28 = next(t for t in data["tasks"] if t["id"] == "T28")
before = list(t28["writes"])
for w in ADD:
    if w not in t28["writes"]:
        t28["writes"].append(w)
tpath.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
print("writes 补登记:", before, "->", t28["writes"])

# ── 2. selfcheck ─────────────────────────────────────────────────────
r = subprocess.run(
    [str(ROOT / ".venv/Scripts/python.exe"), str(M / "_selfcheck_conflict.py")],
    cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()][-2:]
print("selfcheck exit:", r.returncode)
for ln in tail:
    print("  ", ln)
assert r.returncode == 0, "selfcheck 未通过"

# ── 3. 依赖与写冲突 ──────────────────────────────────────────────────
prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
done = {k for k, v in prog["tasks"].items() if v.get("status") == "done"}
missing = [d for d in t28["deps"] if d not in done]
print("\nT28:", t28["title"], "| phase:", t28["phase"], "| budget:", t28["budget"])
print("not_do:", t28["not_do"], "| pitfalls:", t28["pitfalls"])
print("未满足依赖:", missing or "NONE")
assert not missing, f"依赖未满足: {missing}"

inprog = {k for k, v in prog["tasks"].items() if v.get("status") == "in_progress"}
overlap = set()
for cid in inprog:
    card = next((t for t in data["tasks"] if t["id"] == cid), None)
    if card:
        for w in card.get("writes", []):
            if w in t28["writes"]:
                overlap.add((cid, w))
print("in_progress:", sorted(inprog) or "none", "| write overlap:", overlap or "NONE")
assert not overlap

# ── 4. PROGRESS 登记 ─────────────────────────────────────────────────
assert "T28" not in prog["tasks"], "T28 已有条目"
prog["tasks"]["T28"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
(M / "PROGRESS.json").write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("\nPROGRESS.json T28 -> in_progress @", NOW)
