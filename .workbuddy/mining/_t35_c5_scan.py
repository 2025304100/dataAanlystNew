# -*- coding: utf-8 -*-
"""T35 C5 红线扫描：statistical_tests.py 的引用方向必须纯净。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent

# 1) statistical_tests.py 自身的 import（不得触碰业务红线模块）
src = (ROOT / "app/services/factors/mining/statistical_tests.py").read_text(
    encoding="utf-8")
imports = [ln.strip() for ln in src.splitlines()
           if ln.strip().startswith(("import ", "from "))]
print("== statistical_tests.py imports ==")
for ln in imports:
    print("  ", ln)

FORBIDDEN = ("factor_experience", "candidates", "factor_model",
             "factor_mining ", "app.models", "app.api", "app.routes",
             "sqlalchemy", "pandas")
bad = [ln for ln in imports if any(k in ln for k in FORBIDDEN)]
print("FORBIDDEN hits:", bad if bad else "NONE (pure algorithm module)")

# 2) 全仓谁引用了 statistical_tests（当前应只有测试；消费方后续卡接入）
hits: list[str] = []
for base in ("app", "tests"):
    for p in (ROOT / base).rglob("*.py"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "statistical_tests" in txt:
            hits.append(str(p.relative_to(ROOT)).replace("\\", "/"))
print("== referencing files ==")
for h in sorted(hits):
    print("  ", h)
