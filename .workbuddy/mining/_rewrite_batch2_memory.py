# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 C 类重写批次 2 记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## C 类重写 · 批次 2 ✅（16:35）—— 主文件 12 项全部真实修复

### 成果
| 范围 | 结果 |
|---|---|
| `FactorModelPage.test.tsx` | **15 passed / 0 failed，exit 0**（含原 12 项 skip 全部复原） |
| **全量** | **805 passed / 0 failed / 7 skipped，exit 0** |
| 验收 | failed 未增加 ✓、skipped **19 → 7**（单调减少）✓ |

### 做了什么（整文件重写，非逐点补丁）
1. **补 mock 定义**：该文件缺 15 个「组件调用但 mock 未定义」的方法；
2. **重写 helper** `mockLoadSuccess` → 注入**镜像层三件套**且形状对齐组件解构
   （`scoringGetOverviewAsFactor → {runtime}`、`scoringGetFactorModelListAsFactor → {items}`、
   `scoringListFactorSetsAsFactor → 数组`）；
3. **断言对齐**：加载断言改新方法；激活断言补第 4 参 `"factor_center:activate"`；
   回退断言补第 2 参 `"factor_center:fallback"`；
4. **删除 1 条失效断言**（组件已移除 `factorModelNoFactorSets` 文案）。

### 关键教训（已写入 MEMORY）
- 🚨 **禁止按名字猜方法归属**：我把详情注入误改为 `scoringGetModelDetail`，但组件加载详情
  **仍用 `api.getFactorModel`**（`scoringGetModelDetail` 是 relations 场景）→ 该错误让 1 项持续失败，
  **回改即绿**。**「镜像层改造」≠「所有方法都改名」，必须逐个核对组件实际调用点。**
- ✅ **主文件这类「基线整体失效」只能整文件重写**：第 4 轮只改一半 → 12→14 恶化；
  本轮 helper + 全部断言一起改 → 12 项全绿。

### 剩余 7 项 skipped
`FactorModelSettings` 3 ｜ `FactorEvaluationLab` 2 ｜ `ExternalDataSync` 1 ｜ `FactorLibrary` 1
—— 批次 3 建议按「单文件」逐个处理（这几个文件的规模都比主文件小）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended rewrite batch2 ->", p.name)
