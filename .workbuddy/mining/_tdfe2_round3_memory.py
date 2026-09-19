# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 TD-FE-RED-2 第 3 轮 + 三轮定论。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## TD-FE-RED-2 第 3 轮 + 三轮定论（13:25）—— 结论：不可自动化，需逐用例人工判定

### 第 3 轮动作（改用动态扫描，不再猜方法名）
从组件源码提取 `api.<method>(` 调用集，与测试 mock 定义集做**差集**：
- `FactorModelPage`：组件调用 18 个，mock 只定义 3 个 → **缺 15 个**
- `FactorModelSettings`：缺 9 个；`ExternalDataSync` 缺 3 个；两个 collections/t13 各缺 2 个
- **合计 31 个**（`_fix_missing_mocks.py` 动态补齐，`list*`→分页对象、其余→空对象，esbuild 预检全过）

### 结果：**恶化 22 → 24** → 精确回滚
`_rollback_missing_mocks.py` 按「文件 × 方法名」删除（安全性：这些名字补齐前必不在该文件 mock 中），
**验证恢复 22 failed / 37 passed** ✓

### 三轮尝试汇总（全部失败）
| 轮次 | 假设 | 结果 |
|---|---|---|
| 1 | 断言写法过时 | 12 → 14 失败，回滚 |
| 2 | 个别 mock 缺失 | 22 → 22，无效 |
| 3 | mock 大面积缺失 | **22 → 24**，回滚 |

### 决定性定性（写进 MEMORY）
根因**不是测试基础设施**，而是**测试期望的数据结构/交互契约 与 组件当前实现
（P1.1 Settings 镜像适配层改造后）不匹配**。判据：补 mock 后组件进入**另一条渲染路径**，
失败集合被"转移"而非消除（22→24）——属语义差异。

→ **只能逐用例人工判定**「组件对→改测试 / 组件错→改组件」，每项留定性依据。**不可自动化**。

### 止损建议（已在报告 §9.5 与 PROGRESS 登记）
排期已 **47/47**；这 22 项属**既有**技术债（最早基线即存在、非本次引入）。
保持 open、按需推进（一次一个用例），**不为清零而放宽标准或做批量迁就式修改**。

### 方法论沉淀（可复用）
- ✅ **动态扫描 mock 完整性**：组件 `api.X(` 调用集 vs 测试 mock 键定义集做差集 —— 比逐轮猜方法名靠谱；
- ⚠️ 但**「补全」≠「修好」**：本项目实测补全反而恶化，因为失败点是语义差异而非缺失；
- ✅ 两次恶化都在**同一轮内发现并回滚验证**，仓库始终停在基线状态（22/7）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended round3 ->", p.name)
