# -*- coding: utf-8 -*-
"""修正 _fix_fe_red.py 首轮插入漏逗号的问题（一次性补救）。

首轮 MOCK_LINE 末尾缺 `,` → 后一行是 `activateFactorModel:` →
esbuild 报 `Expected "}" but found "activateFactorModel"`。
本脚本按**整行字符串精确匹配**那一行并补逗号（比正则安全）。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent

TARGETS = [
    "src/components/__tests__/FactorModelPage.test.tsx",
    "src/components/__tests__/FactorModelSettings.test.tsx",
    "src/components/factors/__tests__/FactorBidirectionalLinks.test.tsx",
    "src/components/factors/__tests__/FactorModelPage.collections.test.tsx",
    "src/components/factors/__tests__/FactorModelPage.t13-supplement.test.tsx",
]

#: 本工具生成、但缺尾逗号的那一行（strip 后比较）
BARE = (
    "scoringGetOverviewAsFactor: vi.fn(async () => ({ feature_enabled: true, "
    "config: {}, runtime: {}, health: {}, latest_trade_date: null, "
    "factor_coverage: [] }))"
)

for rel in TARGETS:
    p = ROOT / "frontend" / rel
    lines = p.read_text(encoding="utf-8").splitlines()
    hits = 0
    for i, line in enumerate(lines):
        if line.strip() == BARE:
            lines[i] = line + ","
            hits += 1
    if hits:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {'FIXED ' if hits else 'NOCHANGE'} x{hits}  {rel}")
