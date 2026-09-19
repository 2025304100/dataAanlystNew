# -*- coding: utf-8 -*-
"""TD7 DoD③：验证 portfolio_factor_usage.py 无 int(snap.id)/int(data["snapshot_id"]) 残留。exit 0=干净。"""
import re
import sys
from pathlib import Path

SRC = Path(r"D:\ai_project\dataAanlystNew\app\services\portfolio_factor_usage.py")
text = SRC.read_text(encoding="utf-8")

bad = []
for i, line in enumerate(text.splitlines(), 1):
    if re.search(r"int\(\s*snap\.id\s*\)", line) or re.search(r'int\(\s*data\[\s*["\']snapshot_id["\']\s*\]\s*\)', line):
        bad.append(f"{i}: {line.strip()}")

if bad:
    print("RESIDUAL int() casts on string-PK snapshot_id:")
    for b in bad:
        print("  " + b)
    sys.exit(1)
print("clean: no int(snap.id)/int(data['snapshot_id']) casts")
