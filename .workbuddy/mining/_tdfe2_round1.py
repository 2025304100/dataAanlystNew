# -*- coding: utf-8 -*-
"""TD-FE-RED-2 第 1 轮：登记定性进展 + 失败尝试复盘 + 报告追加 + memory。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

# ── 1) PROGRESS.debt 更新 ───────────────────────────────────────────
ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["status"] = "in_progress"
d2["round1_at"] = NOW
d2["round1_findings"] = {
    "items_extracted": 22,
    "artifact": ".workbuddy/mining/td_fe_red2_items.json（文件/用例/错误首行）",
    "key_insight": (
        "这批测试**刻意 mock 了 i18n**（源码注释原文：「i18n mock：t(key) 返回 key，"
        "便于按 key 断言」），断言写的是 key 字面量（getByText(\"factorModelShadow\")）。"
        "但实测该 mock **部分生效**：失败用例实际 DOM 里既有 key 形式、也有真实中文译文"
        "（如「步骤 1」「因子库」），说明页面另有文本来自未被 mock 的入口。"
    ),
    "failed_attempt": (
        "尝试①：把 30 处断言统一改为真实语言包取值 getByText(zhCN.x) —— "
        "**失败数 12 → 14（引入新失败）**，已用反向替换脚本精确回滚，"
        "并跑该文件验证恢复 12 failed / 3 passed。"
    ),
    "conclusion": (
        "**不能批量改断言**。必须逐用例跑单测 → 读实际 DOM → 判定该断言期望值"
        "（key 形态 or 译文形态）→ 单点修正；这也印证了本卡 guardrail"
        "「禁止批量把断言改成迁就现状」。"
    ),
    "attempt_scripts": [
        ".workbuddy/mining/_fix_c_key_assertions.py（干跑：报告 47 处 key 字面量断言）",
        ".workbuddy/mining/_fix_c_key_assert2.py（尝试①，已回滚）",
        ".workbuddy/mining/_rollback_c_key_assert2.py（反向回滚）",
        ".workbuddy/mining/_extract_c_items.py（结构化提取）",
    ],
    "next": (
        "逐用例推进：建议按文件分批（FactorModelPage 12 项 → Settings 3 → collections 2 → "
        "t13 1 → 其余 4），每批结束后跑全量确认失败数单调下降且零新增。"
    ),
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.debt.TD-FE-RED-2 已更新（in_progress + 第 1 轮发现）")

# ── 2) 报告追加 ────────────────────────────────────────────────────
report = ROOT / "docs/前端既有红定性报告.md"
report.open("a", encoding="utf-8").write("""

---

## 6. TD-FE-RED-2 第 1 轮：C 类 22 项定性（2026-09-19 上午）

### 6.1 已完成：结构化提取（22 项 / 7 文件）
`_extract_c_items.py` → `.workbuddy/mining/td_fe_red2_items.json`，逐项含
「文件 / 用例名 / 错误首行」。错误可聚为 4 类：

| 类 | 形态 | 代表项数 |
|---|---|---|
| 文本缺失 | `Unable to find an element with the text: factorModelXxx`（**i18n key 字面量**） | 7+ |
| 文本缺失（数据 id） | `... the text: model-001-abc` / `fs-set-3126b70f` | 3 |
| 按钮名缺失 | `Unable to find role="button" and name "更新因子评分"/"初始化仓库"/运行流水线` | 3 |
| 断言/调用不符 | `expected "spy" to be called with ...`、`Found multiple elements with text: -0.6707`、表头数组不符 | 6+ |
| 其他 | `toBeDisabled()` 失败等 | 3 |

### 6.2 关键发现：测试刻意 mock 了 i18n，但 mock **部分生效**
测试源码注释原文：*「i18n mock：t(key) 返回 key，便于按 key 断言」*，
`vi.mock("../../i18n", () => ({ t: (k) => k, template: (k) => k }))`。
经核对，**mock 路径与组件 import 解析到同一模块**（都指向 `src/i18n`），
理应生效；但失败用例的**实际 DOM 打印**显示页面里同时存在：
- `t` 被 mock 后的 key 形态，以及
- **真实中文译文**（「步骤 1」「因子库」「影子运行」等）

→ 判定：**mock 只覆盖了 `t`/`template` 两个导出，页面另有文本来自未被 mock 的入口**
（或组件内部走了其它取词路径）。这正是「断言 key 找不到」的直接原因。

### 6.3 一次失败的尝试（已回滚，留档）
**尝试**：把 30 处 `getByText("factorModelXxx")` 统一改为 `getByText(zhCN.factorModelXxx)`
（取真实语言包值）。
**结果**：失败数 **12 → 14（引入新失败）**。
**处置**：`_rollback_c_key_assert2.py` 反向替换回滚（30 处断言 + 1 处 import），
并跑该文件验证**恢复 12 failed / 3 passed**（与此前一致）。
**教训**：在本项目当前的 i18n mock 形态下，**批量改断言是错误策略** ——
必须逐用例看实际 DOM 才能判定期望值形态。这也正是本卡 guardrail 预判的坑。

### 6.4 结论与下一步
- **定性基本完成**（根因 = 测试与「i18n mock 形态」不匹配 + 少量真实行为/契约差异），
  但**处置必须逐项**，不能批处理；
- 建议按文件分批推进，每批后跑全量确认「失败数单调下降 + 零新增」；
- T39（DoD 要求 `npm test` 全绿）仍**被这 22 项阻塞**。
""")
print("报告已追加 §6")
