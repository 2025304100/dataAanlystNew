# -*- coding: utf-8 -*-
"""T34 C5 红线扫描：新模块引用方向必须纯净（禁触 F1/DB/路由）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent

NEW = [
    "app/services/factors/mining/diversity.py",
    "app/services/factors/mining/convergence.py",
    "app/services/factors/mining/reproduction/scheduler.py",
    "app/services/factors/mining/reproduction/mutation.py",
    "app/services/factors/mining/reproduction/crossover.py",
    "app/services/factors/mining/reproduction/injection.py",
]

FORBIDDEN = (
    "factor_experience", "app.models", "app.api", "app.routes",
    "sqlalchemy", "pandas", "FactorWarehouse",
)

print("== 新模块 import 面 ==")
bad_total: list[tuple[str, str]] = []
for rel in NEW:
    p = ROOT / rel
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
             if ln.strip().startswith(("import ", "from "))]
    print(f"\n-- {rel}")
    for ln in lines:
        print("   ", ln)
    for ln in lines:
        for k in FORBIDDEN:
            if k in ln:
                bad_total.append((rel, ln))
print("\nFORBIDDEN hits:", bad_total if bad_total else "NONE（纯算法模块）")

print("\n== 全仓谁引用了新模块（当前应只有测试）==")
for base in ("app", "tests"):
    for p in (ROOT / base).rglob("*.py"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        for rel in NEW:
            mod = rel[:-3].replace("/", ".").removesuffix(".__init__")
            stem = mod.split(".")[-1]
            if stem in ("diversity", "convergence", "scheduler", "mutation",
                        "crossover", "injection", "reproduction") and \
                    f"mining.{stem}" in txt or f"mining.reproduction" in txt or \
                    f"mining import {stem}" in txt:
                print("   ", str(p.relative_to(ROOT)).replace("\\", "/"))
                break
