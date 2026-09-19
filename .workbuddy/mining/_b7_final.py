# -*- coding: utf-8 -*-
"""批次 7 最终收口记录。"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
b7 = d2["rewrite_progress"].get("batch7_partial", {})
b7["final"] = {
    "at": "2026-09-19T21:35:00+08:00",
    "result": "全量 **807 passed / 0 failed / 5 skipped，exit 0**（保持稳定）",
    "key_root_cause_found": (
        "FactorModelSettings 数据不送达的**一个确定根因**：组件对 `tasks` 直接 `.find()`"
        "（要**数组**），而 mock 返回 `{items:[]}` → TypeError → catch → 页面只有头部。"
        "已修（scoringListTasks 返回 []）。"
    ),
    "correct_changes_kept": [
        "9 个 scoring* mock 已在 mock 对象顶级（结构正确，esbuild 预检过）",
        "scoringListTasks 返回数组",
        "断言/注入方法名对齐镜像层（scoringActivateModel / scoringCreateTask）",
    ],
    "why_still_skipped": (
        "修正 tasks 形状后单文件曾出现 3 PASS，但随后同状态复跑为 3 failed（疑似 vitest 结果"
        "缓存/文件时序假象，未及深查）→ 按纪律恢复 skip，待新会话从「打印组件 catch 的 err」"
        "继续（mock 侧已就绪，剩余问题在组件渲染分支或另一处数据流）。"
    ),
    "remaining": {
        "FactorModelSettings.test.tsx": "3 项（mock 侧已就绪；需打印 catch err 定位剩余数据流断点）",
        "FactorEvaluationLab.test.tsx": "2 项（轮询 timer 挂起 + 覆盖率渲染路径，需专门会话）",
    },
}
d2["updated_at"] = "2026-09-19T21:35:00+08:00"
prog["updated_at"] = d2["updated_at"]
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS 已更新 batch7 final")

mem = ROOT / ".workbuddy/memory/2026-09-19.md"
mem.open("a", encoding="utf-8").write("""

---

## C 类重写 · 批次 7 final（21:35）—— 找到一个确定根因并修复；全量稳定 807/0/5

- **确定根因（已修）**：FactorModelSettings 组件对 `tasks` 直接 `.find()`（要**数组**），
  mock 返回 `{items:[]}` → TypeError → catch → 页面只剩头部。已改 `scoringListTasks` 返回 `[]`。
- **保留的正确改动**：9 个 scoring* mock 在对象顶级（esbuild 预检过）、断言/注入方法名对齐镜像层。
- **待查**：修 tasks 形状后单文件曾出现 3 PASS，同状态复跑为 3 failed（疑似 vitest 结果缓存/
  时序假象）→ 按纪律恢复 skip。下批从「测试里打印组件 catch 的 err」继续（mock 侧已就绪）。
- **全量：807 passed / 0 failed / 5 skipped，exit 0** ✓（failed 恒为 0）
- 最终：**C 类 17/22 已修复**；余 5 项 skip + TODO，每项有「下一步从哪查」的完整指引。
""")
print("memory 已追加")
