# -*- coding: utf-8 -*-
"""C5 红线扫描：mining/**（含新 selection/）禁 import F1 经验库 model。"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MINE = os.path.join(ROOT, "app", "services", "factors", "mining")
BANNED = ("factor_experience", "from app.models.factor_",
          "import app.models.factor_")
hits = []
for dirpath, _dirnames, filenames in os.walk(MINE):
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        path = os.path.join(dirpath, fn)
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                for token in BANNED:
                    if token in line:
                        hits.append(f"{path}:{lineno}: {token} -> {line.strip()}")
out = os.path.join(ROOT, ".workbuddy", "mining", "_t33_c5_out.txt")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(hits) if hits else "CLEAN - no F1 model import in mining/")
print("HITS:", len(hits))
