# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加批次 5 尝试记录（改用脚本文件避免 bash 反引号截断）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## C 类重写 · 批次 5 尝试（19:40）—— FactorEvaluationLab 未完成，已恢复稳定

### 教训（重要，两条）
1. 🚨 **批量 skip 必须按失败清单**（用 JSON reporter 的用例名），不能按文档顺序取前 N 个
   `it(` —— 回滚时错 skip 了通过的用例，导致轮询用例重复运行 → **vitest 挂起、全量卡死**。
2. 🚨 **该文件 mockContext 被第 3 轮批量脚本误插了 7 个 api 方法**
   （scoringGetOverviewAsFactor 等），已确认**组件经 context 使用它们** → 不能删除
   （删除即崩）。清理"多余 mock"前必须先确认组件依赖。

### 结果
- FactorEvaluationLab 2 项重写未完成（覆盖率显示格式、轮询终态断言），已恢复 skip；
- **全量恢复稳定：807 passed / 0 failed / 5 skipped，exit 0** ✓
- 诊断进展已记入 `PROGRESS.debt.TD-FE-RED-2.rewrite_progress.batch5_attempt`。

### 状态
| 项 | 值 |
|---|---|
| 排期 | **47/47（100%）** |
| 前端全量 | **807 passed / 0 failed / 5 skipped，exit 0** |
| C 类重写 | **17/22 已真实修复**，余 5 项 skip + TODO（2 文件，各有明确错误定位） |
| 后端 mining 全量 | 1090 passed |
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended batch5 attempt ->", p.name)
