# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 T39 节（排期收口 47/47）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## T39 · 拆分 Settings.tsx ✅（10:56）—— **排期收口 47/47（100%）**

### 交付（纯重构，行为不变）
- **新建 `settings/SettingsNav.tsx`**（156 行）：侧边导航从 `Settings.tsx` 剥离为独立组件 ——
  **16 个导航项数据驱动**（NAV_ITEMS），导出 `SettingsSection` 联合类型（16 值）供壳层复用；
  **等价性契约原样保留**：`nav.settings-sidebar` + `aria-label`、`button.settings-nav-item`
  （含 `active` 类与 `aria-current="page"`）、`span.settings-nav-icon/copy`、项顺序逐项一致；
- `Settings.tsx`：**698 → 554 行（-144）**，`<nav>` 块替换为 `<SettingsNav .../>`，缩进/位置不变；
  **未改动任何设置项的行为、默认值与 localStorage 键**。

### 验证（pitfalls「先留存基线」已执行）
| 项 | 拆分前 | 拆分后 |
|---|---|---|
| `Settings.test.tsx` | **6 passed** | **6 passed** ✅ 行为不变 |
| 全量 | 22 failed / 790 passed / 7 文件 | **22 failed / 790 passed / 7 文件** ✅ 零新增 |

收工三连 done @10:56 → selfcheck ALL OK → 看板 694 行。

### 两个必须留痕的判断
1. **DoD 口径调整**（经用户拍板采纳推荐方案 A）：原 DoD 要求 `npm test` exit 0，
   实测 exit 1 —— 唯一原因是仓库既有的 **C 类 22 项红**（TD-FE-RED-2，与本卡无关且非脚本可批量清除）。
   口径改为「**不新增失败（基线 22 / 7 文件）**」，已写入 `PROGRESS.tasks.T39.dod_note`。
   **本卡自身零新增失败**，不构成质量放宽。
2. **主动不拆 rules 区**：rules 区（原 135 行 JSX）虽是最大块，但其**无测试覆盖**
   （Settings.test.tsx 只覆盖 nav 与分区切换）→ 机械搬运的错误**无法被验证**。
   为守住「行为不变」的**可验证性**，本卡不拆；留待补测试覆盖后再拆。**这是取舍，不是遗漏。**

### 排期总览（47/47）
- PH1~PH7 全部完成；门禁 G5（5 步向导可点通）已闭环、G6（M2 算法）抽样通过、G7（i18n 一致）达成；
- 遗留技术债：**TD-FE-RED-2（C 类 22 项前端红，open）** —— 已定性为「组件调用契约/渲染条件
  与测试期望不一致」，需逐用例读组件对照，非脚本可批量清除（两轮尝试已证明）。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T39 section ->", p.name)
