# -*- coding: utf-8 -*-
"""T27 开工前侦查：任务卡状态 + PROGRESS 占用 + 前端目标文件现状。"""
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"

tasks = json.loads((M / "tasks.json").read_text(encoding="utf-8"))
prog = json.loads((M / "PROGRESS.json").read_text(encoding="utf-8"))
p_tasks = prog["tasks"]

done = {k for k, v in p_tasks.items() if v.get("status") == "done"}
print("== PROGRESS done count ==", len(done))
for cid in ("T21", "T22", "T23", "T24", "T25", "T26", "T27", "T28", "T33", "T34", "T35", "T38"):
    st = p_tasks.get(cid, {}).get("status", "-")
    card = next((t for t in tasks["tasks"] if t["id"] == cid), None)
    deps = card.get("deps") if card else None
    ready = "-"
    if card is not None and st == "-":
        ready = "READY" if all(d in done for d in (deps or [])) else f"blocked{deps}"
    print(f"  {cid}: progress={st:<12} deps={deps} {ready}"
          f"  {(card.get('title') if card else '')}")

print("\n== frontend 目标文件现状 ==")
targets = [
    "frontend/src/components/Settings.tsx",
    "frontend/src/components/factors/mining/MiningShell.tsx",
    "frontend/src/types/mining.ts",
    "frontend/src/api/factorMining.ts",
    "frontend/src/i18n/zh-CN.ts",
    "frontend/src/i18n/en-US.ts",
]
for t in targets:
    p = ROOT / t
    print(f"  {'EXISTS' if p.exists() else 'MISSING':<8} "
          f"{(p.stat().st_size if p.exists() else '-'):>7} B  {t}")

print("\n== frontend 目录概览 ==")
fe = ROOT / "frontend"
print("  frontend exists:", fe.exists())
if fe.exists():
    pkg = fe / "package.json"
    print("  package.json:", "EXISTS" if pkg.exists() else "MISSING")
    if pkg.exists():
        pj = json.loads(pkg.read_text(encoding="utf-8"))
        print("  scripts:", json.dumps(pj.get("scripts", {}), ensure_ascii=False))
        print("  test_runner:", json.dumps(
            {k: v for k, v in pj.get("devDependencies", {}).items()
             if "test" in k or "vitest" in k or "jest" in k}, ensure_ascii=False))
    # 测试文件
    tests = []
    for base in ("src", "tests"):
        for dirpath, dirnames, filenames in os.walk(fe / base):
            for fn in filenames:
                if fn.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
                    fp = os.path.join(dirpath, fn)
                    tests.append(os.path.relpath(fp, fe).replace("\\", "/"))
    print("  test files:", len(tests))
    for t in sorted(tests)[:30]:
        print("    ", t)
