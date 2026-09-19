# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T27 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T27 · Settings 6 处 + MiningShell + i18n ✅（20:52，36/47）

### 交付
- `Settings.tsx` 六处（设计 §9.1）：L34 联合类型 / L38 localStorage / **L76
  settings:navigate 白名单** / nav 按钮 / 内容块 `data-settings-content=
  "settings-factor-mining"` / import；`not_do`（不新增 App 顶级 Tab）守成立
- 新建 `MiningShell.tsx`（壳：子页签 向导/批次列表；5 步步骤条 + 待开放占位；
  批次列表**三态容错**——后端 listRuns 仍 NotImplementedError(M5)，不可让 501 崩页）
- 新建 `types/mining.ts`（对齐后端 MiningRunCreate/Page DTO + 向导 §2 五步常量）
- 新建 `api/factorMining.ts`（dbConfig.ts 范式、requestJson 单一入口；
  文件头标注后端实现进度：已实现仅 locks/status + submit/batch-review）
- i18n 两份新增 15 个 key 严格同步（translations 门禁）
- 新建 `Settings.test.tsx` **6 passed**（DoD-1）；translations **3 passed**（DoD-2）
- 防波及：前端全量 **671 passed / 44 failed（67 文件）**，失败 10 文件全为既有
  因子域测试，与本卡 3 目标文件**零交集**
- 后端实际状态记录：`app/api/routes/factor_mining.py` 22 条路由中仅 3 条已实现，
  其余 M5/M7/M9/M10/M12/M13 全部 `raise NotImplementedError`

### 既有红（未修，已定性上报）
`npm test -- Settings` 聚合 EXIT=1，3 项失败全在 `FactorModelSettings.test.tsx`：
组件 `FactorModelSettings.tsx:308` 调 `api.scoringGetOverviewAsFactor()`，而该测试
mock 未定义（同目录另 3 个测试文件都已 mock）。本卡未触碰该组件/api/测试（均在
writes 外），按「勿顺手修他人债 + 不越 writes」纪律未改。
**建议立技术债卡：补一行 `scoringGetOverviewAsFactor: vi.fn(...)` 即可绿。**
同类既有红还有 9 个因子域测试文件（unifiedErrorNetworkMessage / mock 缺口）。

### 坑（新增，值得记住）
- 🚨 **npm 在 bash shim 下被沙箱拦**（走 wsl.exe，PROGRAM BLOCKED）→ 前端命令一律
  用 PowerShell 工具跑；其输出是 UTF-16 → 落盘后用 `utf-8-sig` 解码并剥 ANSI 转义再读。
- 🚨 vitest：`vi.mock` 工厂被提升到顶部，**工厂内不得引用外层变量**（TDZ，
  `Cannot access 'stub' before initialization`）→ 逐条内联。
- 🚨 测试里原生 `window.dispatchEvent` **必须包 `act()`**，否则 React 状态不 flush、
  DOM/localStorage 断言看不到结果（`fireEvent.click` 自带 act，易误判为"点击通、
  派发不通"的实现 bug）。
- 🚨 i18n 差集别用正则扫源码：zh-CN 有 key 缩进 5 空格会被漏判 → 用运行时
  `Object.keys` 差集探针（临时测试文件，跑完删）。
- 收工脚本解析 pytest/vitest 汇总要用 `Tests X failed | Y passed (Z)` 全量行正则，
  否则误抓单文件行数（本次把 44 failed 记成 3，已用 _t27_fix_evidence.py 纠正）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T27 section ->", p.name)
