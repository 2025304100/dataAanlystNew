# -*- coding: utf-8 -*-
"""向 2026-09-19.md 追加 TD-FE-RED-2 第 1 轮记录。"""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
p = ROOT / ".workbuddy/memory/2026-09-19.md"

text = """

---

## TD-FE-RED-2 第 1 轮：C 类 22 项定性（08:55）—— 定性基本完成，处置须逐项

### 做了什么
1. **结构化提取 22 项** → `.workbuddy/mining/td_fe_red2_items.json`（文件/用例/错误首行），
   按错误形态聚为 5 类（key 字面量文本缺失 / 数据 id 缺失 / 按钮名缺失 / spy 调用不符 /
   重复元素与禁用态）。
2. **定位根因**：这批测试**刻意 mock 了 i18n**（注释原文「t(key) 返回 key，便于按 key 断言」），
   断言写的是 **key 字面量**；但实测该 mock **部分生效** —— 失败用例的实际 DOM 里
   同时有 key 形态与**真实中文译文**（「步骤 1」「因子库」），说明页面另有文本来自
   未被 mock 的入口。
3. **一次失败尝试（已回滚留档）**：把 30 处断言统一改为取真实语言包
   `getByText(zhCN.x)` → **失败数 12 → 14（引入新失败）** → 用反向替换脚本
   `_rollback_c_key_assert2.py` 精确回滚，并跑该文件验证**恢复 12 failed / 3 passed**。

### 教训（本轮最重要）
- 🚨 **「批量改断言」在这类问题上必然翻车**：必须先逐用例跑单测 + 读实际 DOM，
  判定期望值该是 key 形态还是译文形态，再**单点**修正。
  这正好验证了本卡 guardrail「禁止批量把断言改成迁就现状」的正确性。
- ✅ **护栏有效**：我第一时间发现失败数上升并**立即回滚 + 验证恢复**，仓库未停在更差状态。
  （这正是「不反复小修小补」纪律的价值：宁可回滚也不叠加补丁。）
- 记录留档：`PROGRESS.debt.TD-FE-RED-2.round1_findings` + 报告 §6。

### 状态
- TD-FE-RED-2 = **in_progress**（定性完成、处置未开始）；
- 排期卡 **46/47**，仅剩 **T39**，其 DoD（`npm test` 全绿）**仍被这 22 项阻塞**；
- 下一步：按文件分批逐用例处置，每批后跑全量确认「失败数单调下降 + 零新增」。
"""

with p.open("a", encoding="utf-8") as f:
    f.write(text)
print("appended TD-FE-RED-2 round1 ->", p.name)
