# -*- coding: utf-8 -*-
"""向 2026-09-18.md 追加 T30 节（append-only）。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-18.md"

text = """

---

## T30 · Step3 字段与校验 UI ✅（23:38，42/47）

### 交付（step3/ 四文件 + i18n）
- `MiningFieldStep.tsx`（DoD 主体）：5 组字段勾选（行情/估值/财报/资金流/事件）；
  **blocked 字段不可勾选**（字段存在 ≠ 可用于挖掘）；发起校验 → **5s 轮询**
  （not_do：不引入 WebSocket），终态自动停表；字段列表 props 注入
- `FieldValidationPanel.tsx`：分片进度、通过态、**阻断详情**（逐字段代码/中文名/
  问题类型/当前值/要求值）；**阻断只给两个出口**（重新选择字段 / 去修复数据），
  出口按钮在阻断项**之外**、阻断项内**零按钮**（not_do：不提供逐字段自动修复）
- `fieldTypes.ts` + `fieldApi.ts`（校验四接口，契约对齐 T14，**该组接口已实现**）
- i18n 两份新增 15 key；**9 passed**；前端全量 **705 passed / 44 failed**（零引入）
- 收工三连 done @23:38 → selfcheck ALL OK → 看板 697 行

### 关键事实（与 T29 同类，值得批量记）
**后端接口就绪度分档**（本卡再确认）：
- ✅ 已实现：校验四接口（T14 `factor_mining_wizard.py`）、候选池 19 条（T08/T09/T11）
- ❌ 缺失：**挖掘字段目录接口**（现仅候选池 `filter-fields` 与
  `/external-data/quality-snapshots/{field}`）、切分预算预览（T29 发现）
→ 前端卡遇缺接口时统一策略：**props 注入 + 无数据不臆造**，待接口补齐父级传入，前端零改动。

### not_do 的两种固化手法（本卡新增第二种，可复用）
1. **反向断言**（T29 用过）：「无 X 时不渲染 Y」；
2. **间谍断言**（本卡）：not_do 说不用 WebSocket → `vi.spyOn(window, "WebSocket")`
   断言**从未被构造**；not_do 说不提供逐字段修复 → 断言阻断项内 `button` 数为 0。

### 复用记录
- 前端卡 writes 漏 i18n 第四次（T27 有 / T28/T29/T30 连补三张）→ 建议把这层
  修正**固化进开卡脚本**（检测 writes 含 `frontend/src/components/` 但无 i18n 时自动补）。
- selfcheck 一律带 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 已成惯例。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended T30 section ->", p.name)
