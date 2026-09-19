# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 C 类重写批次 3（阶段）记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## C 类重写 · 批次 3（阶段）✅（15:45）—— FactorLibrary 修复，基线保持绿

### 成果
| 项 | 结果 |
|---|---|
| `FactorLibrary.test.tsx` | **1 passed / 0 failed，exit 0**（原 skip 复原为真实用例） |
| **全量** | **806 passed / 0 failed / 6 skipped，exit 0** |
| 验收 | failed 恒为 0 ✓、skipped **7 → 6** ✓ |

### 本批的做法与发现
1. **FactorLibrary**：测试 mock 里只有旧 `listFactorDefinitions`，组件已改用镜像层
   `scoringListFactorDefinitions({page,page_size:100})` → 补 6 个 scoring* mock + 断言改新方法；
2. **踩坑并纠正**：我先把 `scoringListFactorDrafts` mock 成 `{items:[],total:0}`，
   组件抛 `TypeError: drafts.filter is not a function` → React 渲染失败 → 页面只剩 `<div />`
   → 断言全找不到。**组件是直接把它当数组用的**，改成 `[]` 即绿。
3. **纪律动作**：本轮取消 7 个 skip 逐个啃，其中 **1 项修好（复原为 `it`）**、
   **6 项未完成 → 恢复 `it.skip` + TODO**，确保 `failed` 恒为 0（不把未完成工作暴露成红）。

### 新增教训（已写入 MEMORY）
🚨 **mock 值形状必须对齐组件的使用方式**：判据是「组件里 `x.filter/map/forEach` ⇒ mock 给数组；
`x.items` ⇒ 给 `{items}`」。形状不符不会报「mock 错」，而是**在组件里炸成 TypeError**
→ 页面空壳 → 断言全崩，**症状与「元素找不到」极像，容易误判为断言过时**。

### 剩余 6 项 skipped（各有明确错误，供下批直接上手）
| 文件 | 项数 | 已知错误 |
|---|---|---|
| `FactorModelSettings.test.tsx` | 3 | 找不到 `ridge-20260714`；找不到按钮 `/运行流水线/`；仓库初始化 spy 0 次 |
| `FactorEvaluationLab.test.tsx` | 2 | `Found multiple elements with text: -0.6707`（重复渲染）；spy 未收到 `task-running` |
| `ExternalDataSync.test.tsx` | 1 | 找不到按钮「更新因子评分」 |

### 累计
| 阶段 | skipped |
|---|---|
| 方案 B 落地 | 22 |
| 批次 1 | 19 |
| 批次 2 | 7 |
| **批次 3（阶段）** | **6** |
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended rewrite batch3 ->", p.name)
