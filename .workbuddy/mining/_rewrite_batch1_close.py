# -*- coding: utf-8 -*-
"""TD-FE-RED-2 重写第 1 批（collections + t13-supplement，3 项）收工记录。

成功要点（供后续批次复用）：
1. **同文件其它用例通过 = mock/helper 健康**，属「局部断言问题」→ 可逐用例修（≠ 主文件的整体脱节）；
2. 组件新引入 `localizedLabel(key, 中文兜底)`：mock 的 t 返回 key 时渲染**中文兜底**
   → 断言 key 必然失败 → 改为断言中文兜底文案；
3. runtime 注入位置变了：组件从 `scoringGetOverviewAsFactor()` 取 runtime
   （不再从 `scoringGetFactorModelListAsFactor` 的返回里取）→ 测试 helper 需补该注入。
"""
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
    p = re.findall(r"(\d+) passed", txt)
    f = re.findall(r"(\d+) failed", txt)
    s = re.findall(r"(\d+) skipped", txt)
    return {"exit": int(m.group(1)) if m else -1,
            "passed": int(p[-1]) if p else -1,
            "failed": int(f[-1]) if f else 0,
            "skipped": int(s[-1]) if s else 0}


two = stats("rewrite_3.txt")
full = stats("rewrite_fe_sweep.txt")
print("两文件  :", two["exit"], two["passed"], "passed /", two["failed"], "failed")
print("全量    :", full["exit"], full["passed"], "passed /", full["failed"],
      "failed /", full["skipped"], "skipped")
assert two["exit"] == 0 and two["failed"] == 0, "两文件未全绿"

ppath = M / "PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
d2 = prog["debt"]["TD-FE-RED-2"]
d2.setdefault("rewrite_progress", {})
d2["rewrite_progress"]["batch1"] = {
    "at": NOW,
    "files": [
        "src/components/factors/__tests__/FactorModelPage.collections.test.tsx（2 项）",
        "src/components/factors/__tests__/FactorModelPage.t13-supplement.test.tsx（1 项）",
    ],
    "fixed": 3,
    "skipped_before": 22,
    "skipped_after": full["skipped"],
    "result": (f"两文件 **{two['passed']} passed / 0 failed, exit {two['exit']}**；"
               f"全量 **{full['passed']} passed / {full['failed']} failed / "
               f"{full['skipped']} skipped, exit {full['exit']}**"),
    "key_learnings": [
        "**同文件其它用例通过 ⇒ mock/helper 健康**，属局部断言问题，可逐用例修"
        "（与 FactorModelPage 主文件的整体脱节不同）",
        "组件新增 `localizedLabel(key, 中文兜底)`：mock 的 t 返回 key 时渲染中文兜底 → "
        "断言应改为中文（collections 的 12 处表头断言）",
        "runtime 取值位置变了：组件从 `scoringGetOverviewAsFactor()` 取 runtime（P1.1 镜像层），"
        "测试 helper 需单独注入该方法（t13 的 seedSuccess）",
    ],
}
d2["updated_at"] = NOW
prog["updated_at"] = NOW
ppath.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"\n已登记 rewrite batch1：修复 3 项，skipped {22} -> {full['skipped']}")
