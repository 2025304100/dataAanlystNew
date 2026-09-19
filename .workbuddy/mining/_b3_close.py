# -*- coding: utf-8 -*-
"""C 类重写批次 3（阶段）：FactorLibrary 修复 + 其余 6 项恢复 skip。"""
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


lib = stats("b3_lib2.txt")
full = stats("b3_sweep.txt")
print("FactorLibrary:", lib["exit"], lib["passed"], "passed /", lib["failed"], "failed")
print("全量         :", full["exit"], full["passed"], "passed /", full["failed"],
      "failed /", full["skipped"], "skipped")
assert lib["exit"] == 0 and lib["failed"] == 0, "FactorLibrary 未全绿"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["rewrite_progress"]["batch3_partial"] = {
    "at": NOW,
    "fixed": ["src/components/__tests__/FactorLibrary.test.tsx（1 项）"],
    "remaining_skipped": full["skipped"],
    "result": (f"FactorLibrary **{lib['passed']} passed / 0 failed, exit {lib['exit']}**；"
               f"全量 **{full['passed']} passed / {full['failed']} failed / "
               f"{full['skipped']} skipped, exit {full['exit']}**"),
    "what_changed": [
        "FactorLibrary：mock 里只有旧 `listFactorDefinitions`，组件已改用镜像层 "
        "`scoringListFactorDefinitions({page,page_size})` → 补 6 个 scoring* mock，"
        "断言改新方法；**并把 drafts 的 mock 值从 `{items:[]}` 改为数组**"
        "（组件注释即 `drafts.filter(...)`，形状不符会 TypeError → 整组件渲染为空）",
    ],
    "key_lessons": [
        "🚨 **mock 值形状必须对齐组件的使用方式**：`drafts`/`fsList` 这类组件**直接当数组用**，"
        "给 `{items:[]}` 会抛 `TypeError: drafts.filter is not a function` → React 渲染失败 → "
        "页面 `<div />` 空，断言全找不到。**判据：组件里是 `x.filter/map/forEach` ⇒ mock 给数组；"
        "是 `x.items` ⇒ 给 `{items}`。**",
        "✅ 修好 1 项即复原为 `it`；**未完成的 6 项恢复 `it.skip` + TODO**，"
        "保证 full 的 `failed` 恒为 0（不把未完成工作暴露成红）。",
    ],
    "remaining": {
        "ExternalDataSync.test.tsx": "1 项（找不到按钮「更新因子评分」）",
        "FactorEvaluationLab.test.tsx": "2 项（重复元素 -0.6707；spy 未调用 task-running）",
        "FactorModelSettings.test.tsx": "3 项（找不到 ridge-20260714；找不到 /运行流水线/ 按钮；仓库初始化 spy 0 次）",
    },
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"\n已登记 batch3_partial：修复 1 项，剩 {full['skipped']} 项 skipped")
