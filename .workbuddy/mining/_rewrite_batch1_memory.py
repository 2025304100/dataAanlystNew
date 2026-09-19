# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 C 类重写批次 1 记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## C 类重写 · 批次 1 ✅（14:00）—— 3 项真正修复，工作流验证通过

### 成果
| 项 | 结果 |
|---|---|
| 两文件（collections 2 项 + t13-supplement 1 项） | **13 passed / 0 failed，exit 0** |
| **全量** | **793 passed / 0 failed / 19 skipped，exit 0** |
| 验收标准 | **failed 未增加 ✓、skipped 22 → 19（单调减少）✓** |

### 三项修复（各自的定性 → 处置）
1. **collections 的表头断言（12 处）**：组件改用
   `localizedLabel(key, 中文兜底)`（`t(key)===key ? fallback : t(key)`）——
   mock 的 t 返回 key → 组件渲染**中文兜底**（「因子代码」等）→ 断言 key 必然失败
   → **改断言为中文兜底**（测试过时，非组件缺陷）。
2. **t13-supplement T13⑥（废弃门禁）**：组件**已正确实现**门禁
   （`activeFsId === fs.id` → disabled + tooltip 含「正在使用的模型」），
   但测试 helper `seedSuccess` 把 runtime 注入到 `scoringGetFactorModelListAsFactor`，
   而组件**是从 `scoringGetOverviewAsFactor()` 取 runtime**（P1.1 镜像层）
   → 补该注入即修复（**测试基建过时，组件无 bug**）。
3. 3 项同时把 `it.skip` 复原为 `it`，TODO 注释移除（真实修复而非跳过）。

### 成功套路（已写入 MEMORY，供后续批次复用）
- **同文件其它用例通过 ⇒ mock/helper 健康** ⇒ 属局部断言问题，**可逐用例修**
  （与 `FactorModelPage` 主文件「全文件基线失效」不同——那里才需要整文件重写）；
- 断言要跟随组件的**取词方式**：走 `t()` 时按 mock 语义断言，走 `localizedLabel` 时断言中文兜底；
- 数据要跟随组件的**取值位置**（镜像层后 runtime 来自 `scoringGetOverviewAsFactor`）；
- 每批跑全量验「failed 不增加 + skipped 单调减少」。

### 剩余
**19 项 skipped**（主文件 `FactorModelPage.test.tsx` 12 项 + `FactorModelSettings` 3 项 +
`ExternalDataSync` 1 + `FactorEvaluationLab` 2 + `FactorLibrary` 1）——
主文件 12 项属**整文件基线失效**（共享 helper `mockLoadSuccess` 联动），
需按「先重写 helper、再逐用例校准」推进；批次 2 建议从它开始。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended rewrite batch1 ->", p.name)
