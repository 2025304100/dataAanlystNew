# -*- coding: utf-8 -*-
"""既有前端红收口（技术债 TD-FE-RED）：为缺 mock 的测试补齐 `scoringGetOverviewAsFactor`。

根因（实测）：`api/client.ts` 新增 P1.1 Settings 镜像适配层方法
`scoringGetOverviewAsFactor`，`FactorModelSettings.tsx` / `FactorModelPage.tsx`
消费它；**部分测试文件的 api mock 未同步该方法** → 组件渲染错误 alert
（`api.scoringGetOverviewAsFactor is not a function`）→ 元素找不到 → 整文件红。

修复：在缺该键的测试文件里补一行 mock（形状对齐 `FactorModelSettings.pipeline-ui.test.tsx`
的 OVERVIEW_BASE）。**只加 mock，不改断言**（不改测试语义）。

用法：
    python .workbuddy/mining/_fix_fe_red.py            # 干跑（只报告）
    python .workbuddy/mining/_fix_fe_red.py --apply     # 实际修改
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
SWEEP = ROOT / ".workbuddy/mining/t37_fe_sweep.txt"
KEY = "scoringGetOverviewAsFactor"
MOCK_LINE = (
    '{indent}{key}: vi.fn(async () => ({{ feature_enabled: true, config: {{}}, '
    'runtime: {{}}, health: {{}}, latest_trade_date: null, factor_coverage: [] }})),'
)

#: 插入锚点（按优先级）——在锚点行**之前**插入
ANCHORS = [
    "scoringGetFactorDefinition:",
    "activateFactorModel:",
    "initializeFactorWarehouse:",
    "updateFactorSystemConfig:",
]


def failed_files() -> list[str]:
    raw = SWEEP.read_bytes().decode("utf-8-sig", errors="replace")
    txt = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    return sorted(set(re.findall(r"FAIL\s+([^\s\[:]+\.(?:tsx|ts))", txt)))


def main() -> int:
    apply = "--apply" in sys.argv
    files = failed_files()
    print(f"失败文件 {len(files)} 个")
    fixed, skipped, manual = [], [], []

    for rel in files:
        p = ROOT / "frontend" / rel
        if not p.exists():
            manual.append((rel, "文件不存在"))
            continue
        text = p.read_text(encoding="utf-8")
        if KEY in text:
            skipped.append(rel)
            continue

        lines = text.splitlines()
        idx, indent = None, "    "
        for anchor in ANCHORS:
            for i, line in enumerate(lines):
                if anchor in line and "//" not in line.split(anchor)[0]:
                    idx = i
                    indent = line[: len(line) - len(line.lstrip())]
                    break
            if idx is not None:
                break
        if idx is None:
            manual.append((rel, "未找到插入锚点"))
            continue

        new_line = MOCK_LINE.format(indent=indent, key=KEY)
        lines.insert(idx, new_line)
        if apply:
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        fixed.append(rel)

    print(f"\n可自动补 mock: {len(fixed)}")
    for r in fixed:
        print("  +", r)
    if skipped:
        print(f"\n已有该 mock（跳过）: {len(skipped)}")
        for r in skipped:
            print("  =", r)
    if manual:
        print(f"\n需人工处理: {len(manual)}")
        for r, why in manual:
            print(f"  ! {r} —— {why}")
    print("\n" + ("已应用修改" if apply else "干跑未修改（加 --apply 生效）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
