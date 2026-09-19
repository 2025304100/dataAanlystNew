# -*- coding: utf-8 -*-
"""对比 zh-CN / en-US 顶层 key 差集（定位 translations 红因）。"""
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend/src/i18n"

PAT = re.compile(r"^\s{4}([A-Za-z_][\w.]*)\s*:")


def keys(p: Path) -> list[str]:
    out: list[str] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        m = PAT.match(ln)
        if m:
            out.append(m.group(1))
    return out


zh = keys(FE / "zh-CN.ts")
en = keys(FE / "en-US.ts")
sz, se = set(zh), set(en)
only_zh = sorted(sz - se)
only_en = sorted(se - sz)
print(f"zh keys: {len(zh)} (unique {len(sz)}) | en keys: {len(en)} (unique {len(se)})")
print(f"\nonly in zh-CN ({len(only_zh)}):")
for k in only_zh[:40]:
    print("   ", k)
print(f"\nonly in en-US ({len(only_en)}):")
for k in only_en[:40]:
    print("   ", k)

mine = {"factorMiningTabTitle", "miningTabWizard", "miningTabRuns",
        "miningStepPool", "miningStepTimeTarget", "miningStepField",
        "miningStepEvolution", "miningStepRun", "miningWizardPending",
        "miningRunsLoading", "miningRunsEmpty", "miningRunsError",
        "miningRunsColRun", "miningRunsColStatus", "miningRunsColProgress"}
print("\n本卡新增 key 在两份中的存在情况:")
for k in sorted(mine):
    print(f"   {k:<24} zh={k in sz} en={k in se}")
