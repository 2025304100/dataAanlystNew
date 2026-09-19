# -*- coding: utf-8 -*-
"""回滚 TD-FE-RED-2 第 3 轮（_fix_missing_mocks.py）插入的 31 个 mock 行。

安全性依据：这 31 个方法名在补齐前**必然不在**各自文件的 mock 中
（它们正是「组件调用但 mock 未定义」的差集），故按「文件 × 该方法名」精确删除不会误删原有 mock。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"

TARGETS: dict[str, list[str]] = {
    "src/components/__tests__/ExternalDataSync.test.tsx": [
        "importTailProxyMinutes", "scoringCreateTask", "scoringGetTask"],
    "src/components/__tests__/FactorModelPage.test.tsx": [
        "addFactorSetMember", "cloneFactorSet", "createFactorSet", "deprecateFactorSet",
        "getFactorSetDetail", "removeFactorSetMember", "scoringActivateModel",
        "scoringFallbackToManual", "scoringFreezeFactorSet", "scoringGetModelDetail",
        "scoringGetModelRelations", "scoringListFactorDefinitions",
        "scoringListFactorSetsAsFactor", "scoringRenameModel", "scoringTrainModel"],
    "src/components/__tests__/FactorModelSettings.test.tsx": [
        "scoringActivateModel", "scoringCancelTask", "scoringCreateTask",
        "scoringFallbackToManual", "scoringGetModelRelations", "scoringGetPipelineEta",
        "scoringGetTask", "scoringInitializeWarehouse", "scoringUpdateSystemConfig"],
    "src/components/factors/__tests__/FactorModelPage.collections.test.tsx": [
        "scoringGetModelRelations", "scoringRenameModel"],
    "src/components/factors/__tests__/FactorModelPage.t13-supplement.test.tsx": [
        "scoringGetModelRelations", "scoringRenameModel"],
}

total = 0
for rel, names in TARGETS.items():
    p = FE / rel
    lines = p.read_text(encoding="utf-8").splitlines()
    kept, removed = [], []
    for line in lines:
        m = re.match(r"^\s*(\w+):\s*vi\.fn\(async \(\) => ", line)
        if m and m.group(1) in names:
            removed.append(m.group(1))
            continue
        kept.append(line)
    p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    total += len(removed)
    print(f"  {rel}: 删除 {len(removed)} 行 {sorted(removed)}")
print(f"\n共回滚 {total} 个 mock 行")
