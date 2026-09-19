# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 C 类重写批次 4 记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## C 类重写 · 批次 4（阶段）✅（17:05）—— ExternalDataSync 修复；FactorEvaluationLab 部分校准

### 成果
| 项 | 结果 |
|---|---|
| `ExternalDataSync.test.tsx` | **11 passed / 0 failed，exit 0**（原 skip 复原） |
| **全量** | **807 passed / 0 failed / 5 skipped，exit 0** |
| 验收 | failed 恒为 0 ✓、skipped **6 → 5** ✓ |

### ExternalDataSync 的两处过时（均已修）
1. **按钮文案**：组件注释明确「P2-4: UX 语义别名 — 显示文案统一使用「评分流水线」而不是「因子」」，
   按钮实际文案是「**刷新评分特征**」，测试仍点「更新因子评分」；
2. **方法名**：组件调 `api.scoringCreateTask({start_date, end_date, full_refresh, train_model:false, materialize_scores:true})`，
   测试断言 `createFactorPipelineTask` —— **参数完全一致，只是方法名过时**；
   顺带补 `scoringCreateTask` / `scoringGetTask` / `importTailProxyMinutes` 三个 mock。

### FactorEvaluationLab（部分完成，仍 skip）
- 已校准：`-0.6707` / `-9.1783` / `98.29%` / `404479` 由 `getByText` 改 `getAllByText`（值在页面多处渲染）；
- 已补：`getEvaluationTaskHeartbeat` mock —— **组件轮询走 heartbeat，仅返回终态后才调 `getEvaluationTask`**，
  只 mock 后者会伪装成「spy 0 次调用」；
- **未完成**：覆盖率**显示格式**已变（`98.29%` 已不在页面）→ 下批需先确认组件覆盖率渲染格式（该处为硬编码，非 t() 包裹）。

### 新增教训（已写入 MEMORY）
- ✅ **「Found multiple elements」** → 改 `getAllByText(...).length).toBeGreaterThan(0)`；
- 🚨 **「spy 0 次调用」先查组件是否换了方法**（轮询 heartbeat、create 改 scoringCreateTask）——
  这类失败极易被误判为「组件没实现」，实为**断言方法名过时**。

### 累计
| 阶段 | skipped |
|---|---|
| 方案 B 落地 | 22 |
| 批次 1 | 19 |
| 批次 2 | 7 |
| 批次 3（阶段） | 6 |
| **批次 4（阶段）** | **5** |

### 剩余 5 项
`FactorEvaluationLab` 2 项（覆盖率格式、轮询终态断言）｜`FactorModelSettings` 3 项
（找不到 `ridge-20260714`、无法定位运行流水线按钮、仓库初始化 spy 0 次）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended rewrite batch4 ->", p.name)
