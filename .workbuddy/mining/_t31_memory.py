# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T31 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T31 · Step4 进化参数 + 资源确认 ✅（23:53，43/47）

### 交付（step4/ 七文件 + i18n）
- `MiningEvoParamStep.tsx`（DoD 主体）：简单/高级切换；**简单模式只出现
  「进化强度/选优偏好/启用 AI 生成」，且不得出现任何专业术语**
  （帕累托/非支配/拥挤度/赛道/锦标赛 —— §6.5.5）；高级模式四折叠区 + 种群/代数
  + 快速试验（100/20 → 50/10）
- `ResourceConfirmModal.tsx`：**锁三态文案**（无冲突 / `mining_domain` 冲突
  显示 ID+代数且**禁用提交** / `duckdb_write` 冲突显示**排队位次**）；
  **ETA 必标「估算」**；资源不足（磁盘<20%/内存<2GB）阻断 + 三条调整建议
- `SelectionConfig`（术语集中区）/`ClassicalBaseConfig`（类别与字段**联动置灰**）/
  `ExplorationConfig`/`ReproductionConfig`（自适应开启时三率显示灰色「自动」+ disabled）
- `evoTypes.ts`（双锁契约，对齐**已实现**的 `locks/status`）
- i18n 两份新增 **47 key**（文案同样避开「并行/进程/worker/CPU」）；**14 passed**
- 前端全量 **719 passed / 44 failed**（零引入）；收工三连 done @23:53 → 看板 697 行

### 强约束的测试固化手法（本卡最值得复用）
把「不该出现什么」写成**黑名单断言**，比正向断言更难被绕过：
1. **术语黑名单**（简单模式）：默认渲染后对 5 个专业术语逐一 `not.toContain`；
   切高级后同一批术语**至少出现一个**（正反对照，防止"什么都没渲染"也算过）。
2. **实现词黑名单**（not_do 不暴露并行度）：整页 textContent 断言不含
   「并行/进程/worker/CPU/cpu」，**含弹窗打开态**。
   → 这条约束还延伸到了 **i18n 文案值**（词一旦进词典就会被渲染）。

### 关键事实
- `GET /factor-mining/locks/status` **已实现**（T21/T23）→ 资源确认弹窗可接真实数据；
- `POST /factor-mining/runs` 仍 NotImplementedError(M5) → 提交按「回调上抛」实现，
  后端补齐后父端接入即可。

### 复用记录
- 前端卡 writes 漏 i18n **第五张**（T27 有 / T28~T31 连补四张）→ 强烈建议把
  「writes 含 `frontend/src/components/` 但无 i18n 两份 → 自动补」固化进开卡脚本。
- Step2~Step4 三卡连续，套路已稳定：**侦查接口就绪度 → 补登记 i18n → 写测试
  （含反向/黑名单断言）→ 实现 → DoD + translations + 前端全量对比基线 → 收工三连**。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T31 section ->", p.name)
