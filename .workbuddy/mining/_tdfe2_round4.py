# -*- coding: utf-8 -*-
"""TD-FE-RED-2 第 4 轮（样板）：共享 helper 导致单用例修复不可行 → 必须整文件重写。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["round4_at"] = NOW
d2["round4_findings"] = {
    "action": (
        "按「逐用例样板」推进首个用例 `FactorModelPage > renders runtime / models / factorSets after load`："
        "已完整定性 —— 组件 `loadData()` 调用镜像层三件套 "
        "`scoringGetFactorModelListAsFactor(20)` / `scoringListFactorSetsAsFactor(\"any\", 50)` / "
        "`scoringGetOverviewAsFactor()`，并按 `modelList.items` / `fsList`（**直接当数组**）/ "
        "`scoringOverview.runtime` 解构；而测试的共享 helper `mockLoadSuccess` 把数据喂给"
        "**旧方法** `getFactorModels` / `listFactorSets` → 组件拿不到数据"
    ),
    "attempt": (
        "修复：把 helper 改为注入**新方法**（形状对齐解构：`{items}` / 数组 / `{runtime}`），"
        "并把断言 182-183 改为新方法"
    ),
    "result": (
        "**恶化：该文件 12 → 14 失败** → 立即用 _rollback_sample1.py 回滚 → "
        "**验证恢复 12 failed / 3 passed**"
    ),
    "decisive_finding": (
        "`mockLoadSuccess` 是**全文件共享 helper**：一旦改为注入新方法，组件就会**真实渲染数据**，"
        "而其余 11 个用例的断言全部建立在「组件渲染失败态/空态」的旧假设上 → 失败集合被整体改写。"
        "**结论：单用例无法独立修复，任何修复都会改变整文件的测试基线** → "
        "必须**整文件重写**（helper + 全部用例断言按组件当前实现重建）。"
    ),
    "four_round_summary": (
        "四轮尝试（①批量改断言 ②补个别 mock ③补全量 31 个 mock ④修共享 helper+样板断言）"
        "**全部失败，三次恶化均已回滚并验证恢复基线**。定性已充分：这批测试**是在组件旧实现下写的**，"
        "P1.1 Settings 镜像适配层改造后**整个测试文件的基线失效** —— 属「测试套件与组件实现脱节」，"
        "需**重写测试**而非修补。"
    ),
    "options": [
        "A. 整文件重写（每文件一轮：helper + 全部断言按组件当前实现重建）—— 彻底但需 7 轮",
        "B. 保留文件、对已脱节用例 skip 并写明原因（TODO: 按 P1.1 镜像层重写）—— "
        "可让 npm test 变绿供 CI 使用，同时不丢失信息",
        "C. 挂账保持现状（22 项既有债，不阻塞已完成的排期）",
    ],
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("第 4 轮结论已登记")

report = ROOT / "docs/前端既有红定性报告.md"
report.open("a", encoding="utf-8").write("""

---

## 10. TD-FE-RED-2 第 4 轮（逐用例样板）：共享 helper 卡死单点修复（2026-09-19）

### 10.1 样板用例的完整定性
用例：`FactorModelPage > renders runtime / models / factorSets after load`

组件 `loadData()`（实测）：
```js
const [modelList, fsList, scoringOverview] = await Promise.all([
  api.scoringGetFactorModelListAsFactor(20),
  api.scoringListFactorSetsAsFactor("any", 50),
  api.scoringGetOverviewAsFactor(),
]);
setRuntime(scoringOverview.runtime);
setModels(modelList.items);
setFactorSets(fsList);          // ← fsList **直接当数组**使用
```

测试的共享 helper：
```js
mockApi.getFactorModels.mockResolvedValue({ runtime, items: models });   // 旧方法
mockApi.listFactorSets.mockResolvedValue(factorSets);                    // 旧方法
```
→ 数据喂给了**组件不再调用的旧方法** → 组件拿不到数据 → 渲染失败态 → 该文件 12 项失败。

### 10.2 修复尝试与结果
按定性把 helper 改为注入新方法（`{items}` / 数组 / `{runtime}`），断言改为新方法：

> **该文件失败数 12 → 14（恶化）** → 已回滚，验证恢复 **12 failed / 3 passed**

### 10.3 决定性发现
`mockLoadSuccess` 是**全文件共享 helper**。改它 → 组件**开始真实渲染数据** →
其余 11 个用例的断言全部建立在「组件渲染失败态」的旧假设上 → **失败集合被整体改写**。

**因此：单用例无法独立修复**——任何"逐用例"修复都无法回避共享 helper 的联动。

### 10.4 四轮总结论
| 轮次 | 尝试 | 结果 |
|---|---|---|
| 1 | 批量改断言 | 12→14，回滚 |
| 2 | 补个别 mock | 22→22，无效 |
| 3 | 补全量 31 个 mock | 22→24，回滚 |
| 4 | 修共享 helper + 样板断言 | 12→14，回滚 |

→ **这批测试是在组件旧实现下写的**；P1.1 Settings 镜像适配层改造后，
**整个测试文件的基线失效** —— 性质是「**测试套件与组件实现脱节**」，
必须**重写测试**，而非修补。

### 10.5 处置选项（需拍板）
| 方案 | 说明 | 代价 |
|---|---|---|
| A. 整文件重写 | 每文件一轮：helper + 全部断言按组件当前实现重建 | 7 轮，需组件知识 |
| **B. skip + 注明原因**（推荐） | 保留文件，对已脱节用例 `it.skip` 并写「TODO: 按 P1.1 镜像层重写」 → `npm test` 可转绿供 CI 使用，信息不丢失 | 低 |
| C. 挂账 | 保持现状（22 项既有债），不阻塞已完成排期 | 0（但 CI 红） |
""")
print("报告已追加 §10")
