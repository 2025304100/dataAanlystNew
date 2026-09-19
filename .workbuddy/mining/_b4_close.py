# -*- coding: utf-8 -*-
"""C 类重写批次 4（阶段）：ExternalDataSync 修复；FactorEvaluationLab 部分校准后恢复 skip。"""
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
M = ROOT / ".workbuddy/mining"
NOW = datetime.now().astimezone().isoformat(timespec="seconds")


def read(name: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "",
                  (M / name).read_bytes().decode("utf-8-sig", errors="replace"))


def stats(name: str) -> dict:
    txt = read(name)
    m = re.search(r"EXIT=(\d+)", txt)
    return {"exit": int(m.group(1)) if m else -1,
            "passed": int((re.findall(r"(\d+) passed", txt) or [-1])[-1]),
            "failed": int((re.findall(r"(\d+) failed", txt) or [0])[-1]),
            "skipped": int((re.findall(r"(\d+) skipped", txt) or [0])[-1])}


ext = stats("b4_ext2.txt")
full = stats("b4_sweep.txt")
print("ExternalDataSync:", ext["exit"], ext["passed"], "passed /", ext["failed"], "failed")
print("全量            :", full["exit"], full["passed"], "passed /", full["failed"],
      "failed /", full["skipped"], "skipped")
assert ext["exit"] == 0 and ext["failed"] == 0, "ExternalDataSync 未全绿"
assert full["failed"] == 0, "全量出现失败"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["rewrite_progress"]["batch4_partial"] = {
    "at": NOW,
    "fixed": ["src/components/__tests__/ExternalDataSync.test.tsx（1 项）"],
    "partial": ["src/components/__tests__/FactorEvaluationLab.test.tsx（2 项：已校准部分断言，仍恢复 skip）"],
    "remaining_skipped": full["skipped"],
    "result": (f"ExternalDataSync **{ext['passed']} passed / 0 failed, exit {ext['exit']}**；"
               f"全量 **{full['passed']} passed / {full['failed']} failed / "
               f"{full['skipped']} skipped, exit {full['exit']}**"),
    "what_changed": [
        "ExternalDataSync：按钮文案过时（组件 P2-4 已把「更新因子评分」改为「刷新评分特征」）；"
        "方法名过时（`createFactorPipelineTask` → `scoringCreateTask`，**参数完全一致**）；"
        "顺带补了 `scoringCreateTask`/`scoringGetTask`/`importTailProxyMinutes` 三个 mock。",
        "FactorEvaluationLab（部分）：把重复元素断言由 `getByText` 改为 `getAllByText`"
        "（`-0.6707`/`-9.1783`/`98.29%`/`404479`），并补 `getEvaluationTaskHeartbeat` mock"
        "（组件轮询走 heartbeat，仅终态后才调 `getEvaluationTask`）。",
    ],
    "key_lessons": [
        "✅ **「重复元素」类失败的正确处置**：值在页面多处渲染（指标卡 + 摘要）时，"
        "`getByText` 必失败 → 用 `getAllByText(...).length).toBeGreaterThan(0)`。",
        "🚨 **轮询类组件的 mock 要跟着轮询方法走**：组件轮询用 `getEvaluationTaskHeartbeat`，"
        "只 mock `getEvaluationTask` 会导致「spy 0 次调用」的假象。",
        "⚠️ FactorEvaluationLab 未完成项：覆盖率**显示格式**已变（断言 `98.29%` 已不存在于页面）——"
        "下批需先从组件确认覆盖率渲染格式（该组件为硬编码渲染，非 t() 包裹）。",
    ],
    "remaining": {
        "FactorEvaluationLab.test.tsx": "2 项（覆盖率显示格式、轮询终态断言）",
        "FactorModelSettings.test.tsx": "3 项（找不到 ridge-20260714 / 无法定位运行流水线按钮 / 仓库初始化 spy 0 次）",
    },
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"\n已登记 batch4_partial：修复 1 项，剩 {full['skipped']} 项 skipped")
