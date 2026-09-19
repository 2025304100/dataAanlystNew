# -*- coding: utf-8 -*-
"""C 类重写批次 2 收工（FactorModelPage.test.tsx，12 项全部真实修复）。"""
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


one = stats("rewrite_page3.txt")
full = stats("rewrite_batch2_sweep.txt")
print("主文件:", one["exit"], one["passed"], "passed /", one["failed"], "failed")
print("全量  :", full["exit"], full["passed"], "passed /", full["failed"],
      "failed /", full["skipped"], "skipped")
assert one["exit"] == 0 and one["failed"] == 0, "主文件未全绿"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2["rewrite_progress"]["batch2"] = {
    "at": NOW,
    "file": "src/components/__tests__/FactorModelPage.test.tsx（12 项，整文件基线失效）",
    "fixed": 12,
    "skipped_before": 19,
    "skipped_after": full["skipped"],
    "result": (f"该文件 **{one['passed']} passed / 0 failed, exit {one['exit']}**；"
               f"全量 **{full['passed']} passed / {full['failed']} failed / "
               f"{full['skipped']} skipped, exit {full['exit']}**"),
    "what_changed": [
        "A. 补该文件 mock 中缺失的 15 个组件调用方法（scoringActivateModel 等）",
        "B. helper `mockLoadSuccess` 改为注入**镜像层三件套**且形状对齐解构："
        "`scoringGetOverviewAsFactor → {runtime}`、`scoringGetFactorModelListAsFactor → {items}`、"
        "`scoringListFactorSetsAsFactor → 数组`（第 4 轮只改一半导致恶化，本轮整文件一并改）",
        "C. 断言与注入的方法名对齐：加载断言改新方法；激活断言补第 4 参 "
        "`\"factor_center:activate\"`；回退断言补第 2 参 `\"factor_center:fallback\"`",
        "D. 删除 1 条已失效断言（组件已无 `factorModelNoFactorSets` 文案）",
    ],
    "key_lessons": [
        "🚨 **不能按名字猜方法归属**：我把详情注入误改为 `scoringGetModelDetail`，"
        "但组件加载详情**仍用 `api.getFactorModel`**（`scoringGetModelDetail` 是 relations 场景）"
        "→ 该错误使 1 项持续失败，回改后即绿。**「镜像层改造」≠「所有方法都改名」，"
        "必须逐个核对组件的实际调用点。**",
        "✅ 整文件重写（helper + 全部断言）是主文件这类「基线整体失效」的唯一正解；"
        "第 4 轮只改一半 → 恶化 12→14，本轮整文件改 → 12→0。",
    ],
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"\n已登记 batch2：修复 12 项，skipped {19} -> {full['skipped']}")
