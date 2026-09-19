# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 TD-FE-RED-2 第 2 轮记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## TD-FE-RED-2 第 2 轮：补 mock 无效，锁定真实主因（10:55）

### 动作与结果
从全量日志**重新提取**缺失 api 方法 → 发现另有两个未补：
**`scoringListTasks`、`getInboxNotifications`**（第 1 轮只补了 5 个）。
用 `_fix_fe_red2.py`（扩展 MOCKS；**并把 esbuild 预检 flag 修正为 `--loader:.tsx=tsx`**）
补入 6 个测试文件后：

> **失败数完全不变：22 failed / 37 passed**（补前补后一致）

→ **「api mock 缺失」不是 C 类主因**（保留补丁：无副作用且提升 mock 完整性）。

### 真实主因（实取当前失败详情）
```
AssertionError: expected "spy" to be called with arguments: [ undefined, 20 ]
Received: Number of calls: 0
```
且失败用例页面的 **DOM 呈 key 形态**（alert 里是 `factorModelCenterGuideTitle`）：
- → **i18n mock 确实生效**（页面文本是 key 而非译文），第 1 轮「按 key 断言找不到」的
  观感有误；
- → 真正问题是**组件根本没调用测试期望的 API**（calls = 0）：
  **组件调用契约 / 渲染条件与测试期望不一致**。

### 处置判断（重要）
逐项修复必须**读组件实现**（`FactorModelPage.tsx` ~3800 行）逐条对照调用契约与渲染条件，
判定每项属「测试过时」还是「组件回归」——**实打实的工程工作量，不能脚本批量做**
（第 1 轮批量改断言已证明会引入新失败 12→14 并已回滚）。

### 建议（已在报告 §7.4 列出，等你拍板）
- **推荐**：把 T39 的 DoD 由「`npm test` 全绿」改为「**不新增失败**（基线 22 / 7 文件）」，
  排期先收口；
- C 类保持独立技术债，按文件分批推进（每批 1 文件、逐用例、每批验证零新增）。

### 三轮累计
| 轮次 | 动作 | 结果 |
|---|---|---|
| A 类 | 补 5 个 api mock（9 文件） | ✅ **44 → 22**，54 个测试恢复运行 |
| C 类第 1 轮 | 22 项定性 + 批量尝试 | 批量失败（12→14）→ **已回滚** |
| C 类第 2 轮 | 再补 2 个 mock（6 文件） | **无效**（22→22）→ 锁定真实主因 |
"""
with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended TD-FE-RED-2 round2 ->", p.name)
