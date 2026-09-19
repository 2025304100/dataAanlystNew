# -*- coding: utf-8 -*-
"""登记/更新技术债条目 TD-FE-RED（PROGRESS.json）+ 追加当日 memory。

不新增排期卡（避免污染 47 张卡的进度分母），在 PROGRESS 顶层 debt 段登记。
"""
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
debt = prog.setdefault("debt", {})
debt["TD-FE-RED"] = {
    "title": "前端既有红收口（api mock 未同步 + 行为断言不一致）",
    "opened_at": "2026-09-19",
    "status": "partially_fixed",
    "report": "docs/前端既有红定性报告.md",
    "class_a": {
        "root_cause": "测试 api mock 未随 api/client.ts 演进同步（缺 scoringGetOverviewAsFactor / "
                      "scoringGetOverview / scoringGetFactorDefinition / "
                      "scoringGetFactorModelListAsFactor / getEvaluationTaskHeartbeat）",
        "fixed": True,
        "fixed_files": 9,
        "untouched_files": ["src/components/__tests__/FactorLibrary.test.tsx（无安全插入锚点）"],
        "quantified": {
            "before": "44 failed / 762 passed / 10 failed files（A 类 10 文件整文件崩 no tests）",
            "after": "22 failed / 784 passed / 7 failed files",
        },
        "scripts": [".workbuddy/mining/_fix_fe_red.py",
                    ".workbuddy/mining/_fix_fe_red2.py",
                    ".workbuddy/mining/_fix_fe_red_comma.py"],
    },
    "class_c": {
        "note": "剩余 22 项为组件行为与断言不一致（表头数组、tooltip 文案、元素结构），"
                "性质未定（测试过时 vs 组件回归）——按项目纪律**不擅自修改**，待裁决",
        "remaining_failed": 22,
        "failed_files": [
            "src/components/__tests__/ExternalDataSync.test.tsx",
            "src/components/__tests__/FactorEvaluationLab.test.tsx",
            "src/components/__tests__/FactorLibrary.test.tsx",
            "src/components/__tests__/FactorModelPage.test.tsx",
            "src/components/__tests__/FactorModelSettings.test.tsx",
            "src/components/factors/__tests__/FactorModelPage.collections.test.tsx",
            "src/components/factors/__tests__/FactorModelPage.t13-supplement.test.tsx",
        ],
        "options": [
            "① 单独立技术债卡逐文件定性（推荐）",
            "② 改 T39 的 DoD 为「不新增失败（基线 22）」",
            "③ 仅修「测试过时」部分，组件回归项上报",
        ],
        "blocks": "T39（拆分 Settings.tsx）DoD 要求 npm test 全绿，当前不可达",
    },
    "updated_at": NOW,
}
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.debt.TD-FE-RED 已登记（status=partially_fixed）")

# ── 当日 memory 追加 ────────────────────────────────────────────────
log = ROOT / ".workbuddy/memory/2026-09-19.md"
text = """

---

## 技术债收口 · TD-FE-RED（01:12）—— 前端既有红分两类，A 类已修

### 做了什么
用户授权「按建议来」→ 先固化两件事并修复既有红：
1. **i18n 写权限自动补**（`_card_i18n_autofix.py`）：检测 writes 含 `frontend/src/components/`
   但缺 i18n 两份的卡并自动补（一次性根治 T28~T37 连补 7 张的重复劳动）；
   已顺带补齐 T39。支持 `--check` 供门禁使用。
2. **既有红定性 + A 类修复**：见 `docs/前端既有红定性报告.md`

### 结论（量化）
| 时点 | Tests | 失败文件 |
|---|---|---|
| 修复前 | 44 failed / 762 passed | 10（A 类整文件崩，`no tests`） |
| 修复后 | **22 failed / 784 passed** | **7** |

- **A 类**（api mock 未随 `client.ts` 同步，5 个方法缺失）→ **已修 9 个文件**，
  纯加 mock、不改断言；顺带**让 54 个原本根本没跑的测试恢复运行**（通过数 +22）。
- **C 类**（22 项组件行为/断言不一致：表头数组、tooltip 文案、元素结构）→
  性质未定（测试过时 vs 组件回归），按项目纪律**不擅自修改**，已登记 PROGRESS.debt 待裁决。

### 本轮两次返工（教训，务必记住）
- 🚨 **往 JS/TS 对象字面量插入成员必须带尾逗号**：首轮插入漏 `,` → 5 个文件
  esbuild 报 `Expected "}" but found "activateModel"`，整批从「测试失败」恶化成「语法崩」。
  补救用**整行字符串精确匹配**补逗号（正则版本因括号计数写错过一次）。
- 🚨 **预检命令要先在单文件验证**：`--loader=tsx` 应为 `--loader:.tsx=tsx`，
  写错导致批量预检**全部误报失败**，反而掩盖真实语法状态。
- 🚨 本机 bash 无 `head/tr/cut`、`npx` 在 bash 下走 wsl 被沙箱拦 →
  **前端命令一律 PowerShell**（再次验证 MEMORY 既有结论）。

### 待裁决（影响收尾）
1. C 类 22 项处置路径（①单立技术债卡 / ②改 T39 DoD 为「不新增失败」/ ③仅修测试过时项）
2. G5（5 步向导可点通）仍缺**容器接线**（`MiningShell.tsx` 属 T27 写权限）
3. T39 / T40 尚未开工
"""
with log.open("a", encoding="utf-8") as f:
    f.write(text)
print("memory 追加完成 ->", log.name)
