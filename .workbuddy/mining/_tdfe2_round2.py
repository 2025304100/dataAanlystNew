# -*- coding: utf-8 -*-
"""TD-FE-RED-2 第 2 轮：登记「补 mock 无效」结论 + 准确定性 + 建议路径。"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["round2_at"] = NOW
d2["round2_findings"] = {
    "action": (
        "从全量日志重新提取缺失 api 方法 → 发现另两个未补：scoringListTasks、"
        "getInboxNotifications → 用 _fix_fe_red2.py（扩展 MOCKS，并修正 esbuild 预检 flag "
        "为 --loader:.tsx=tsx）补入 6 个测试文件"
    ),
    "result": (
        "**失败数完全不变：22 failed / 37 passed（与补前一致）** → 结论："
        "「api mock 缺失」不是 C 类主因（A 类那批已由第 1 轮清除干净）"
    ),
    "kept_because": "补入的 mock 无副作用且提升 mock 完整性（防止未来组件调用时崩溃）",
    "true_root_cause": (
        "实取当前失败详情后定位：主因是**组件调用契约/渲染内容与测试期望不一致**，"
        "证据两条：①断言形如 `expected \"spy\" to be called with arguments: [ undefined, 20 ]` "
        "而 `Received: Number of calls: 0`（组件根本没调用测试期望的 API）；"
        "②失败用例页面的 **DOM 呈现 key 形态**（如 alert 里是 `factorModelCenterGuideTitle`），"
        "证明 i18n mock **确实生效** —— 因此这不是 i18n 问题，也不是 mock 缺失问题。"
    ),
    "implication": (
        "逐项修复需**读组件实现**（FactorModelPage.tsx 约 3800 行）逐条对照调用契约/"
        "渲染条件，判定「测试过时」还是「组件回归」—— 属实打实的工程工作量，"
        "无法靠脚本批量完成（第 1 轮的批量尝试已证明会引入新失败）。"
    ),
    "recommendation": (
        "建议把 T39 的 DoD 从「npm test 全绿」改为「**不新增失败**（基线 22 / 7 文件）」，"
        "使排期收口；C 类 22 项作为独立技术债按需分批推进（每批 1 个文件、逐用例）。"
    ),
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.debt.TD-FE-RED-2 第 2 轮结论已登记")

report = ROOT / "docs/前端既有红定性报告.md"
report.open("a", encoding="utf-8").write("""

---

## 7. TD-FE-RED-2 第 2 轮：补 mock 无效，锁定真实主因（2026-09-19 上午）

### 7.1 动作与结果
从全量日志重新提取缺失 api 方法，发现另有两个未补：
`scoringListTasks`、`getInboxNotifications`（此前第 1 轮只补了 5 个）。
补入 6 个测试文件后：

> **失败数完全不变：22 failed / 37 passed**（补前补后一致）

→ 结论：**「api mock 缺失」不是 C 类主因**。A 类（整文件崩）已在第 1 轮清干净，
C 类是**另一性质**的问题。

（补入的 mock 无副作用且提升完整性，故保留。）

### 7.2 真实主因（实取当前失败详情后定位）
```
AssertionError: expected "spy" to be called with arguments: [ undefined, 20 ]
Received: Number of calls: 0
```
且失败用例页面的 **DOM 呈 key 形态**（alert 文案是 `factorModelCenterGuideTitle`）

两条证据合起来说明：
1. **i18n mock 确实生效**（页面文本是 key，不是译文）→ 第 1 轮「断言 key 找不到」
   的观感有误，真正原因在别处；
2. **组件根本没有调用测试期望的 API**（calls = 0）→ 这是**组件调用契约/渲染条件
   与测试期望不一致**。

### 7.3 处置判断
逐项修复必须**读组件实现**（`FactorModelPage.tsx` 约 3800 行）逐条对照
「调用契约 / 渲染条件」，判定每项属**测试过时**（组件已按后续需求演进）还是
**组件回归**（组件偏离既定口径）。这属于实打实的工程工作量，
**不能靠脚本批量完成**——第 1 轮的批量尝试（12→14 失败）已证明此路不通。

### 7.4 建议路径（供拍板）
| 方案 | 说明 |
|---|---|
| **推荐：改 T39 的 DoD** | 由「`npm test` 全绿」改为「**不新增失败**（基线 22 / 7 文件）」，排期先收口 |
| C 类独立推进 | 保持 TD-FE-RED-2 为 open，按文件分批（每批 1 文件、逐用例、每批验证零新增） |

## 8. 三轮累计小结

| 轮次 | 动作 | 结果 |
|---|---|---|
| A 类（第 1 轮） | 补 5 个缺失 api mock（9 文件） | ✅ **44 → 22 失败**；54 个测试恢复运行 |
| C 类第 1 轮 | 22 项结构化定性 + 一次批量尝试 | 批量改断言**失败**（12→14）→ **已回滚** |
| C 类第 2 轮 | 再补 2 个缺失 mock（6 文件） | **无效**（22→22）→ 锁定真实主因：**契约/渲染不一致** |
""")
print("报告已追加 §7 / §8")
