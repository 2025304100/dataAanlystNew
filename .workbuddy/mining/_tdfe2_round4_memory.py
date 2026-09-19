# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 TD-FE-RED-2 第 4 轮（样板）记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## TD-FE-RED-2 第 4 轮（逐用例样板）：共享 helper 卡死单点修复（13:55）

### 样板用例的完整定性（有价值的部分）
`FactorModelPage > renders runtime / models / factorSets after load`：
- 组件 `loadData()` 调用**镜像层三件套** `scoringGetFactorModelListAsFactor(20)` /
  `scoringListFactorSetsAsFactor("any", 50)` / `scoringGetOverviewAsFactor()`，
  并按 `modelList.items`、**`fsList` 直接当数组**、`scoringOverview.runtime` 解构；
- 测试的共享 helper `mockLoadSuccess` 却把数据喂给**旧方法** `getFactorModels` / `listFactorSets`
  → 组件拿不到数据 → 渲染失败态 → 该文件 12 项失败。

### 修复尝试：**恶化 12 → 14** → 已回滚（验证恢复 12 failed / 3 passed）
把 helper 改为注入新方法（形状对齐解构 `{items}` / 数组 / `{runtime}`）+ 断言改新方法后：

> **决定性发现**：`mockLoadSuccess` 是**全文件共享 helper**。改它 → 组件**开始真实渲染数据**
> → 其余 11 个用例的断言全部建立在「组件渲染失败态」的旧假设上 → **失败集合被整体改写**。
> **因此单用例无法独立修复**，不存在"逐用例修"的路径。

### 四轮总结论（全部失败，三次恶化均已回滚）
| 轮次 | 尝试 | 结果 |
|---|---|---|
| 1 | 批量改断言 | 12→14，回滚 |
| 2 | 补个别 mock | 22→22，无效 |
| 3 | 补全量 31 个 mock | 22→24，回滚 |
| 4 | 修共享 helper + 样板断言 | 12→14，回滚 |

→ 定性完成：这批测试**写在组件旧实现下**，P1.1 Settings 镜像适配层改造后**整个测试文件基线失效**
—— 性质是「**测试套件与组件实现脱节**」，**必须重写测试**。

### 处置选项（已写入报告 §10.5，待拍板）
- **A** 整文件重写（每文件一轮：helper + 全部断言按当前实现重建）—— 彻底，需 7 轮；
- **B（推荐）** 对已脱节用例 `it.skip` 并写明「TODO: 按 P1.1 镜像层重写」→ `npm test` 可转绿供 CI 使用，信息不丢失；
- **C** 挂账保持现状（22 项既有债，不阻塞已完成排期）。

### 仓库状态
✅ 干净：四次实验全部回滚并验证恢复基线（**22 failed / 790 passed / 7 文件**），selfcheck ALL OK。
留下可复用资产：`_fix_missing_mocks.py`（动态扫描差集）、`_rollback_missing_mocks.py`
与 `_rollback_sample1.py`（精确回滚）、报告 §9/§10 完整留痕。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended round4 ->", p.name)
