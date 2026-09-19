# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T32 节（跨零点，append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T32 · Step5 运行跟踪 + 结果页 ✅（00:03 收工，44/47）

### 交付（step5/ 2 文件 + result/ 2 文件 + i18n）
- `step5/FactorMiningRunTrack.tsx`：进度总览（代数/收敛/多样性/ETA）、**多样性<30%
  早熟告警**、ICIR 曲线（**收敛参考线来自配置阈值，测试断言「传 0.05 出现 0.05 且不出
  现 0.08」——§8.3.2 明令不得写死门禁**）、收敛提示「建议停止」但**不自动停**、
  三操作（中断→继续 / 提前停止确认含「前 X 代 + Top N」/ 放弃确认含「不可恢复」）、
  最终验证进度单独展示
- `result/FactorMiningResult.tsx`：**顶部研究声明固定为第一个区块**、等级 chip 筛选、
  **D 级默认不勾选** + 含 D 级批量二次确认、**跳转因子模型页恰携 4 参数**（缺项禁用）、
  **不自动激活因子/模型**（not_do，测试断言两个回调均未被调用）
- 测试 2 文件 **19 passed**；translations 3 passed；前端全量 **738 passed / 44 failed**
  （零引入）；收工三连 done @00:03 → 看板 700 行

### ⚠️ G5 门禁未闭环（重要，需后续动作）
G5 = 「5 步向导可点通到结果页」。5 步组件（step1~step5）与结果页**均已交付并各有测试**，
但向导容器 `MiningShell.tsx`（**T27 写权限，不在 T32 writes 内**）目前只有步骤条
`data-mining-step-bar`（5 步键位齐全、`data-mining-step`），**尚未接线到各 step 组件**。
本卡按 C17 写权限边界**未越权修改容器** → G5 验收需**容器接线**（建议 T27 补充卡或独立收口卡）。
> 教训：卡片按「组件目录」拆 writes 时，**跨卡装配点（容器/路由）容易成为孤儿**；
> 排期时应显式指定装配卡，或在 gate 卡里写明由谁接线。

### 关键事实
`GET /runs/{id}`、`/generations`、`/candidates` 均 M7 未实现 → 本卡 props 注入 + 空态容错，
后端补齐后父级接入即可。

### 复用记录
- 前端卡 writes 漏 i18n **第六张**（T27 有 / T28~T32 连补五张）→ 见下文「待办」。
- 本会话（22:00~00:03）连续闭环 **T34→T38→T28→T36→T29→T30→T31→T32** 八张卡，
  固定套路：侦查接口就绪度 → 补登记 writes(i18n) → 测试先行（含反向/黑名单断言）→
  实现 → DoD + translations + 前端全量对比基线 → 收工三连（PROGRESS/selfcheck/看板）→ memory。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T32 section ->", p.name)
