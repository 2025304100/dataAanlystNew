# -*- coding: utf-8 -*-
"""C 类重写批次 2：FactorModelPage.test.tsx（12 项，整文件基线失效）。

定性（本轮实测）：
- 组件 `loadData()` 用**镜像层三件套**（`scoringGetFactorModelListAsFactor(20)` /
  `scoringListFactorSetsAsFactor("any",50)` / `scoringGetOverviewAsFactor()`），
  并从 `scoringOverview.runtime` / `modelList.items` / `fsList`（数组）解构；
- 测试 helper 却把数据喂给**旧方法** `getFactorModels` / `listFactorSets`，
  且**未注入** `scoringGetOverviewAsFactor` → `scoringOverview.runtime` 抛错 → 渲染错误态
  → 12 个断言找不到元素（第 4 轮只改一半故恶化，本轮整文件一并改）；
- 14 个 key 断言**保持不动**（组件走 `t()`，mock 的 t 返回 key ⇒ 页面渲染 key）。

本脚本：
 A. 补该文件 mock 中缺失的组件调用方法（安全默认值）；
 B. helper 改注入三件套（形状对齐解构）；
 C. 4 组方法名替换（mock 注入 + 断言）；
 D. 移除 TODO 注释并把 `it.skip` 复原为 `it`。
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"
T = FE / "src/components/__tests__/FactorModelPage.test.tsx"
C = FE / "src/components/factors/FactorModelPage.tsx"

text = T.read_text(encoding="utf-8")

# ── A. 补缺失 mock 定义 ─────────────────────────────────────────────
called = set(re.findall(r"api\.(\w+)\s*\(", C.read_text(encoding="utf-8")))
mocked = set(re.findall(r"^\s*(\w+)\s*:", text, re.M))
missing = sorted(called - mocked)

PAGE = "vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 }))"
EMPTY = "vi.fn(async () => ({}))"

def val(n: str) -> str:
    return PAGE if re.search(r"(List|Relations)", n) else EMPTY

if missing:
    lines = text.splitlines()
    idx = next(i for i, l in enumerate(lines) if "scoringGetFactorModelListAsFactor:" in l)
    indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
    lines[idx + 1: idx + 1] = [f"{indent}{n}: {val(n)}," for n in missing]
    text = "\n".join(lines) + "\n"
print(f"A. 补 mock 定义 {len(missing)} 个: {missing}")

# ── B. helper 改注入三件套 ──────────────────────────────────────────
OLD_A = "  mockApi.getFactorModels.mockResolvedValue({ runtime, items: models });\n" \
        "  mockApi.listFactorSets.mockResolvedValue(factorSets);"
NEW_A = ("  // 镜像层（P1.1）：组件从这三个方法取数据，形状必须对齐其解构\n"
         "  mockApi.scoringGetOverviewAsFactor.mockResolvedValue({ runtime });\n"
         "  mockApi.scoringGetFactorModelListAsFactor.mockResolvedValue({ items: models });\n"
         "  mockApi.scoringListFactorSetsAsFactor.mockResolvedValue(factorSets);")
assert OLD_A in text, "helper 注入行未找到"
text = text.replace(OLD_A, NEW_A, 1)
print("B. helper 已改注入三件套")

# ── C. 方法名替换（注入 + 断言）────────────────────────────────────
SUBS = [
    # 断言：加载
    ('expect(mockApi.getFactorModels).toHaveBeenCalledWith(undefined, 20);',
     'expect(mockApi.scoringGetFactorModelListAsFactor).toHaveBeenCalledWith(20);'),
    ('expect(mockApi.listFactorSets).toHaveBeenCalledWith(undefined, 50);',
     'expect(mockApi.scoringListFactorSetsAsFactor).toHaveBeenCalledWith("any", 50);'),
    # 加载失败用例
    ('mockApi.getFactorModels.mockRejectedValue(new Error("network error"));\n    mockApi.listFactorSets.mockResolvedValue([]);',
     'mockApi.scoringGetOverviewAsFactor.mockRejectedValue(new Error("network error"));\n    mockApi.scoringListFactorSetsAsFactor.mockResolvedValue([]);'),
    # 详情 / 激活 / 回退
    ('mockApi.getFactorModel.mockResolvedValue(', 'mockApi.scoringGetModelDetail.mockResolvedValue('),
    ('mockApi.activateFactorModel.mockResolvedValue(', 'mockApi.scoringActivateModel.mockResolvedValue('),
    ('mockApi.activateFactorModel.mockRejectedValue(', 'mockApi.scoringActivateModel.mockRejectedValue('),
    ('expect(mockApi.activateFactorModel).toHaveBeenCalledWith(',
     'expect(mockApi.scoringActivateModel).toHaveBeenCalledWith('),
    ('mockApi.fallbackFactorModel.mockResolvedValue(', 'mockApi.scoringFallbackToManual.mockResolvedValue('),
    ('expect(mockApi.fallbackFactorModel).not.toHaveBeenCalled();',
     'expect(mockApi.scoringFallbackToManual).not.toHaveBeenCalled();'),
    ('expect(mockApi.fallbackFactorModel).toHaveBeenCalledWith("IC 衰减触发回退");',
     'expect(mockApi.scoringFallbackToManual).toHaveBeenCalledWith("IC 衰减触发回退", "factor_center:fallback");'),
]
for old, new in SUBS:
    n = text.count(old)
    if n:
        text = text.replace(old, new)
    print(f"C. {n} x {old[:64]}...")

# ── D. 取消 skip + 移除 TODO ─────────────────────────────────────────
n_skip = text.count("it.skip(")
text = text.replace("it.skip(", "it(")
text = re.sub(r"^\s*// TODO\(P1\.1 镜像适配层\)[^\n]*\n", "", text, flags=re.MULTILINE)
print(f"D. 取消 skip {n_skip} 个；残留 TODO {text.count('TODO(P1.1')} 处")

T.write_text(text, encoding="utf-8")
print("\n已写入。剩余 mock 缺失再检查:", len(called - set(re.findall(r'^\s*(\w+)\s*:', text, re.M))))
