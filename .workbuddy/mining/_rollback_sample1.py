# -*- coding: utf-8 -*-
"""回滚样板修复（mockLoadSuccess + 断言），恢复文件原状。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / "frontend/src/components/__tests__/FactorModelPage.test.tsx"
text = p.read_text(encoding="utf-8")

NEW_HELPER = """function mockLoadSuccess(overrides: { runtime?: any; models?: any[]; factorSets?: any[] } = {}) {
  const runtime = makeRuntime(overrides.runtime);
  const models = overrides.models ?? [makeModel()];
  const factorSets = overrides.factorSets ?? [makeFactorSet()];
  // 组件已按 P1.1 镜像适配层改造（loadData 调用 scoring*AsFactor 三件套），
  // 故数据必须注入**新方法**，且形状与组件解构一致：
  //   modelList.items → models；scoringOverview.runtime → runtime；fsList **直接是数组**
  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: models });
  mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);
  mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ runtime });
  return { runtime, models, factorSets };
}"""

OLD_HELPER = """function mockLoadSuccess(overrides: { runtime?: any; models?: any[]; factorSets?: any[] } = {}) {
  const runtime = makeRuntime(overrides.runtime);
  const models = overrides.models ?? [makeModel()];
  const factorSets = overrides.factorSets ?? [makeFactorSet()];
  mockApi.getFactorModels.mockResolvedValue({ runtime, items: models });
  mockApi.listFactorSets.mockResolvedValue(factorSets);
  return { runtime, models, factorSets };
}"""

NEW_ASSERT = """    await waitFor(() => {
      // 组件已改用 P1.1 镜像层的 scoring*AsFactor 方法（旧 getFactorModels/listFactorSets 不再被调用）
      expect(mockApi.scoringGetFactorModelListAsFactor).toHaveBeenCalledWith(20);
      expect(mockApi.scoringListFactorSetsAsFactor).toHaveBeenCalledWith("any", 50);
    });"""

OLD_ASSERT = """    await waitFor(() => {
      expect(mockApi.getFactorModels).toHaveBeenCalledWith(undefined, 20);
      expect(mockApi.listFactorSets).toHaveBeenCalledWith(undefined, 50);
    });"""

n1 = text.count(NEW_HELPER)
n2 = text.count(NEW_ASSERT)
text = text.replace(NEW_HELPER, OLD_HELPER).replace(NEW_ASSERT, OLD_ASSERT)
p.write_text(text, encoding="utf-8")
print(f"回滚：helper {n1} 处、断言 {n2} 处")
print("残留 scoringListFactorSetsAsFactor.mockResolvedValue:",
      text.count("scoringListFactorSetsAsFactor.mockResolvedValue"))
