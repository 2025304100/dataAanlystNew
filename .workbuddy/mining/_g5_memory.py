# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 G5 收口 + TD-FE-RED-2 立项记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## G5 收口 + TD-FE-RED-2 立项（01:00，用户逐条拍板后执行）

### 用户拍板
1. C 类 22 项 → **方案①：单独立技术债卡逐项定性**
2. G5 容器接线 → **授权修改 MiningShell.tsx**
3. T39/T40 → 按计划推进

### 交付一：G5 闭环 ✅（「5 步向导可点通到结果页」）
`MiningShell.tsx` 接线（T27 之外写权限，**已获授权并留痕** `PROGRESS.debt.G5-WIRING`）：
- 步骤条改为可点击跳转（`data-mining-step-jump`）
- 底部「上一步/下一步」（`data-mining-prev/next`，首尾步禁用）
- 按 `currentStep` 渲染 step1~step5；**step5 需运行数据，无 `runProgress` 时显示空态（不臆造进度）**
- i18n 新增 3 key（`miningWizardPrev/Next/NoRun`，两份同步）
- 新增测试 `wizard/MiningWizardFlow.test.tsx` **6 passed**（默认步、连续推进到 5 个面板、
  首尾禁用、点击跳转、step5 空态、step5 有数据渲染 RunTrack）
- 全量回归：**22 failed / 790 passed (812)** —— 失败数与基线一致（**零引入**），
  通过数 +6（本卡新增用例）；translations 3 passed

### 交付二：TD-FE-RED-2 立项（C 类独立技术债卡）
登记于 `PROGRESS.debt.TD-FE-RED-2`（status=open），**刻意不计入 47 张排期卡分母**：
- scope：22 项 / 7 文件（清单在条目内）
- method：逐文件收集失败断言 → 定性三选一（测试过时→改断言 / 组件回归→修组件 / 口径未定→上报）
- guardrails：**禁止批量把断言改成「迁就现状」**（会掩盖真实回归）；每项须留「定性依据」一句
- 完成标准：22 项全部定性并处置；它同时是 T39 的前置（T39 DoD 要求 `npm test` 全绿）

### 本轮我自己的两次小返工（都是路径/拼写级，已即时修）
- G5 测试里 `import "./MiningShell"` 写成同级 → 实际在上一级（`../MiningShell`）；
  同类还有 `vi.mock("./wizard/step1/poolApi")` → 应为 `./step1/poolApi`
  （**测试文件的 mock 路径要按「被测模块的解析路径」写，不是按自己所在目录的相对描述**）。
- 均一次性修正，未影响结论。

### 进度
- 排期卡：**45/47**（T39 待 C 类卡；T40 可做）
- 技术债：TD-FE-RED（A 类，partially_fixed→A 类已完成）/ **TD-FE-RED-2（open）** /
  G5-WIRING（done）
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended G5 section ->", p.name)
