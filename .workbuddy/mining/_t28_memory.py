# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T28 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T28 · Step1 候选池 UI ✅（22:16，39/47）

### 交付（step1/ 六文件 + 测试 + i18n）
- `MiningPoolStep.tsx`（主编排）：入口 Tab 互斥（切换前确认清空）、**300ms 防抖预览**
  （预览只展示不落快照）、生成物料按钮状态机（生成→分析中→查看看板）、
  锁定态筛选/导入/批量删除**全置灰**、重新选择二次确认后删快照解锁、
  批量删除二次确认（未选中时禁用）、**<50 硬阻断**（MIN_POOL_SIZE）
- `poolApi.ts`（13 个 API 封装，走 requestJson 单一入口）、
  `PoolFilterPanel` / `PoolImportPanel` / `PoolMemberTable` /
  `PoolAnalysisModal`（**只读，不渲染任何表单控件**）/ `PoolLockBanner`
- i18n 两份新增 **45 个 `miningPool*` key** 严格同步
- `MiningPoolStep.test.tsx` **12 passed**；translations 3 passed
- 防波及：前端全量 **683 passed / 44 failed（10 文件）**——与 T27 基线（44/10）
  **持平，零引入**；后端 mining 全量 1062 passed（本卡零后端改动）
- 收工三连 done @22:16 → selfcheck ALL OK → 看板 702 行

### ⚠️ writes 补登记（值得记住的排期层动作）
原卡 writes 仅 `step1/` 目录，但前端卡文案必须走 `t()`、且 translations 要求
两份 key 集合相等 → **不写 i18n 无法合规交付**。按 T27 同款口径**补登记**
`i18n/zh-CN.ts` + `i18n/en-US.ts` 进 tasks.json（脚本内注明理由），
补登记后 selfcheck 仍 ALL OK。同类前端卡开工前应**先检查 writes 是否漏 i18n**。

### 4 轮修复（**全部测试侧问题，实现零 bug**）
1. `vi.mock('./poolApi')` **未导出常量 `MIN_POOL_SIZE`** → 组件 import 时抛
   "No MIN_POOL_SIZE export is defined" → 渲染崩、元素为 null（极像组件 bug，
   实为 mock 不完整）→ mock 工厂必须补常量。
2. 批量删除在**未选中成员时 disabled**（实现防误删语义）→ 测试改为
   `poolId="pool-1"` + fetchMembers 返回 2 条 + 先勾选再删
   （为此给实现补了挂载拉成员的 useEffect）。
3. <50 阻断用例用 fake timers 时 `waitFor` **不推进时间** → 直接
   `Test timed out in 5000ms` → 改走真实定时器 + waitFor(timeout 3000)。
4. 防抖用例 `advanceTimersByTime` 后 promise 链未 flush → 补
   `await act(async () => { await Promise.resolve(); })`，并把 it 回调改 async
   （否则 esbuild 报 "await can only be used inside an async function"）。

### 关键事实
后端候选池域（T08/T09/T11）**已完整实现 19 条路由**（与挖掘域大多 NotImplementedError
不同）→ Step1 UI 有真实后端支撑，不需要像 T27 那样做"接口待实现"容错。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T28 section ->", p.name)
