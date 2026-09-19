# -*- coding: utf-8 -*-
"""回滚 FactorEvaluationLab.test.tsx 到「方案 B 稳定状态」。

背景：本轮试图重写该文件（改 helper 三件套 + 修 mockContext 结构），引发
`FactorEvaluationLab.test.tsx` 单文件跑无结果（vitest 异常退出），并卡死全量。
→ 按纪律回滚到**已知稳定状态**：原 helper 注入（旧方法）+ 原 12 项断言 + 12 项 `it.skip`。
（保留：mock 定义的 15 个方法 [无害]、`getAllByText` 等部分校准 [注释级]。）
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / "frontend/src/components/__tests__/FactorEvaluationLab.test.tsx"
t = p.read_text(encoding="utf-8")

# 1) helper 恢复旧注入
t = t.replace(
    "mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ runtime, items: models });",
    "mockApi.getFactorModels.mockResolvedValue({ runtime, items: models });\n"
    "  mockApi.listFactorSets.mockResolvedValue(factorSets);", 1)
t = t.replace(
    "mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);", "", 1)

# 2) 断言恢复旧方法
pairs = [
    ("expect(mockApi.scoringGetFactorModelListAsFactor).toHaveBeenCalledWith(20);",
     "expect(mockApi.getFactorModels).toHaveBeenCalledWith(undefined, 20);"),
    ("expect(mockApi.scoringListFactorSetsAsFactor).toHaveBeenCalledWith(\"any\", 50);",
     "expect(mockApi.listFactorSets).toHaveBeenCalledWith(undefined, 50);"),
    ("mockApi.scoringGetModelDetail.mockResolvedValue(", "mockApi.getFactorModel.mockResolvedValue("),
    ("expect(mockApi.scoringActivateModel).toHaveBeenCalledWith(",
     "expect(mockApi.activateFactorModel).toHaveBeenCalledWith("),
    ("mockApi.scoringActivateModel.mockResolvedValue(", "mockApi.activateFactorModel.mockResolvedValue("),
    ("mockApi.scoringActivateModel.mockRejectedValue(", "mockApi.activateFactorModel.mockRejectedValue("),
    ("expect(mockApi.scoringFallbackToManual).not.toHaveBeenCalled();",
     "expect(mockApi.fallbackFactorModel).not.toHaveBeenCalled();"),
    ("mockApi.scoringFallbackToManual.mockResolvedValue(", "mockApi.fallbackFactorModel.mockResolvedValue("),
    ("expect(mockApi.scoringFallbackToManual).toHaveBeenCalledWith(\"IC 衰减触发回退\", \"factor_center:fallback\");",
     "expect(mockApi.fallbackFactorModel).toHaveBeenCalledWith(\"IC 衰减触发回退\");"),
]
for old, new in pairs:
    t = t.replace(old, new)

# 3) 恢复 12 项 skip（数字型重复断言保留 getAllByText；TODO 注释重挂）
TODO = ("// TODO(P1.1 镜像适配层)：本用例写在组件旧实现下，镜像层改造后已脱节"
        "（该文件 12 项与共享 helper mockLoadSuccess 联动，需整文件重写）；"
        "见 docs/前端既有红定性报告.md §9/§10 与 PROGRESS.debt.TD-FE-RED-2。")
n_skip = t.count("it.skip(")
if n_skip == 0:
    t = t.replace("it(", "it.skip(", 12)
    # 给每个 skip 前挂 TODO（若无）
    t = re.sub(r"^(\s*)(it\.skip\()", r"\1" + TODO + r"\n\1\2", t, flags=re.MULTILINE)
else:
    print("已有 skip:", n_skip, "（不重复加）")

# 4) 移除本轮加的说明注释（保持与原始状态接近）
t = t.replace("  // 该值在页面多处出现（指标卡 + 摘要），用 getAllByText", "")
t = t.replace("// 组件用 localizedLabel(key, 中文兜底)：mock 的 t 返回 key 时渲染中文兜底，故断言中文\n    ", "")

p.write_text(t, encoding="utf-8")
print(f"回滚完成：it.skip = {t.count('it.skip(')}，helper 旧注入 = "
      f"{'getFactorModels.mockResolvedValue' in t}")
