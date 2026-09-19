# -*- coding: utf-8 -*-
"""TD-FE-RED-2 第 3 轮：记录「补全量 mock 反而恶化」+ 三轮总结论 + 止损建议。"""
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
d2["round3_at"] = NOW
d2["round3_findings"] = {
    "action": (
        "**动态扫描**「组件调用但测试 mock 未定义」的方法（不硬编码）→ 发现 mock 大面积缺失："
        "FactorModelPage 组件调用 18 个方法而 mock 只定义 3 个（缺 15）、FactorModelSettings 缺 9、"
        "ExternalDataSync 缺 3、两个 collections/t13 各缺 2 —— 合计 **31 个**；"
        "用 _fix_missing_mocks.py 补齐（list* → 分页对象，其余 → 空对象；写后 esbuild 预检全过）"
    ),
    "result": (
        "**失败数恶化：22 → 24（+2）** → 按护栏立即用 _rollback_missing_mocks.py 精确回滚"
        "（按「文件 × 方法名」删除，安全性依据：这些名字在补齐前必然不在该文件 mock 中），"
        "**验证恢复 22 failed / 37 passed**"
    ),
    "decisive_conclusion": (
        "三轮尝试（①批量改断言 ②补 2 个缺失 mock ③补全量 31 个缺失 mock）**均无法降低失败数**，"
        "其中两次恶化均已回滚。决定性定性：**本批红的根因不是测试基础设施**"
        "（既不是断言写法，也不是 mock 缺失），而是**测试期望的数据结构/交互契约与组件当前实现"
        "（P1.1 Settings 镜像适配层改造后）不匹配**。补 mock 会让组件进入**另一条渲染路径**，"
        "从而改变失败集合（而非消除失败）—— 这正说明失败点随组件行为而转移，属语义差异。"
    ),
    "required_action": (
        "必须**逐用例人工判断**：对每一项决定「组件对（改测试断言）」还是「组件错（改组件）」，"
        "并给出定性依据。**无法自动化**（三轮已充分证明）。"
    ),
    "stop_loss_recommendation": (
        "排期已 47/47 完成，本批 22 项属**既有**技术债（非本次工作引入，最早基线即存在）。"
        "建议：保持 open 状态按需推进（每次处理 1 个用例、留定性依据），"
        "不为清零而放宽标准或做批量迁就式修改。"
    ),
    "scripts": [
        ".workbuddy/mining/_fix_missing_mocks.py（动态补齐，已回滚）",
        ".workbuddy/mining/_rollback_missing_mocks.py（精确回滚）",
    ],
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.debt.TD-FE-RED-2 第 3 轮结论已登记")

report = ROOT / "docs/前端既有红定性报告.md"
report.open("a", encoding="utf-8").write("""

---

## 9. TD-FE-RED-2 第 3 轮：补全量 mock 反而恶化 → 三轮定论（2026-09-19）

### 9.1 动作
改为**动态扫描**（不硬编码方法名）：从组件源码提取 `api.<method>(` 得到调用集，
与测试 mock 定义集做差集。结果发现 mock **大面积缺失**：

| 文件 | 组件调用 | mock 缺失 |
|---|---|---|
| `FactorModelPage.test.tsx` | 18 个方法 | **15 个** |
| `FactorModelSettings.test.tsx` | 12 个 | **9 个** |
| `ExternalDataSync.test.tsx` | 14 个 | 3 个 |
| `FactorModelPage.collections / t13-supplement` | — | 各 2 个 |
| **合计** | | **31 个** |

补齐（`list*`→分页对象、其余→空对象；写后 esbuild 预检全过）后：

> **失败数恶化：22 → 24（+2）** → 立即精确回滚 → **验证恢复 22 failed / 37 passed**

### 9.2 三轮尝试汇总（全部失败，两次已回滚）

| 轮次 | 假设 | 动作 | 结果 |
|---|---|---|---|
| 1 | 断言写法过时 | 批量把 key 断言改为真实译文 | **12 → 14 失败**，回滚 |
| 2 | 个别 mock 缺失 | 补 2 个缺失方法 | 22 → 22，**无效** |
| 3 | mock 大面积缺失 | 动态补齐 31 个方法 | **22 → 24 失败**，回滚 |

### 9.3 决定性定性
**本批红的根因不是测试基础设施**（既非断言写法、亦非 mock 缺失），而是
**测试期望的数据结构/交互契约 与 组件当前实现（P1.1 Settings 镜像适配层改造后）不匹配**。

判据：补齐 mock 后组件**进入另一条渲染路径**——失败集合被**改变**而非消除
（22→24）。这说明失败点随组件行为转移，属**语义差异**。

### 9.4 必要动作（人工，不可自动化）
逐用例判断：**组件对→改测试断言** ／ **组件错→改组件**，每项留定性依据。
三轮自动化已充分证明此路不可自动化。

### 9.5 止损建议
排期已 **47/47**；本批 22 项属**既有**技术债（最早基线即存在，非本次引入）。
建议保持 open、按需推进（一次一个用例），**不为清零而放宽标准或做批量迁就式修改**。
""")
print("报告已追加 §9")
