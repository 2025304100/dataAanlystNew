"""一次性迁移：把 DoD 里引用的测试文件补进 tasks.json 的 writes。

背景（2026-09-16，T03 开工前发现）
--------------------------------
排期生成时，各任务的 `writes` 只登记了**实现文件**，漏了 DoD 里要求新建的**测试文件**。
实测 27 个任务存在该缺口，其中：

  - T36 与 T37 **共用** `tests/services/factors/mining/test_grading.py`
  - T35 与 T37 **共用** `tests/services/factors/mining/test_statistical_tests.py`
  - T25 一个任务要写 2 个测试文件

后果有两层：

1. **写权限冲突检测失效**：`next_task.py` 靠 `writes` 判重。测试文件不在 `writes` 里，
   T36 ‖ T37 并行时会被判「无冲突」→ 两个 Agent 同时写 `test_grading.py` 互相覆盖。
2. **任务卡自相矛盾**：DoD 命令要求创建某个文件，而任务的写权限白名单里没有它。
   Agent 要么越权（违反 protocol），要么卡住（违反 DoD）。

做法
----
1. 从每个任务的 `dod[].cmd` 里正则提取 `tests/**.py` 路径，补进该任务 `writes`（去重、追加在末尾）。
2. 把被 ≥2 个任务共用的测试文件登记进 `high_conflict_files`，作为额外兜底。
3. 幂等：已存在的引用不重复添加；重跑无副作用。

回滚
----
本脚本只做「追加」，不做删除。若需回滚，删掉 `writes` 末尾由本脚本追加的 `tests/` 项即可
（对照 git diff 即可辨识）。执行完毕后本文件可安全删除。
"""
from __future__ import annotations

import collections
import json
import pathlib
import re

TASKS = pathlib.Path(".workbuddy/mining/tasks.json")

#: 只认 DoD 命令里出现的、以 tests/ 开头的 .py 路径
_TEST_PATH = re.compile(r"(tests/[\w/.\-]+\.py)")

#: 粗体等 markdown 残留不该出现在路径里，做个防御性过滤
def _clean(path: str) -> str:
    return path.strip().strip("*`").strip()


def test_files_in_dod(task: dict) -> list[str]:
    """按出现顺序返回该任务 DoD 引用的测试文件（去重）。"""
    out: list[str] = []
    for item in task.get("dod") or []:
        for m in _TEST_PATH.finditer(item.get("cmd", "")):
            f = _clean(m.group(1))
            if f not in out:
                out.append(f)
    return out


def main() -> int:
    raw = TASKS.read_text(encoding="utf-8")
    doc = json.loads(raw)

    added: list[tuple[str, str]] = []
    for task in doc["tasks"]:
        writes = task.setdefault("writes", [])
        for dep in test_files_in_dod(task):
            if dep not in writes:
                writes.append(dep)
                added.append((task["id"], dep))

    # 共享测试文件 → high_conflict_files 兜底
    shared: dict[str, list[str]] = collections.defaultdict(list)
    for task in doc["tasks"]:
        for f in task.get("writes") or []:
            if f.startswith("tests/"):
                shared[f].append(task["id"])

    hcf = doc.setdefault("high_conflict_files", {})
    added_hcf: list[tuple[str, list[str]]] = []
    for path, ids in sorted(shared.items()):
        if len(ids) > 1 and path not in hcf:
            hcf[path] = sorted(ids)
            added_hcf.append((path, sorted(ids)))

    new = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    TASKS.write_text(new, encoding="utf-8")

    print(f"tasks.json 已更新：{len(raw)} -> {len(new)} bytes")
    print()
    print(f"=== 补进 writes 的测试文件（{len(added)} 条）===")
    for tid, f in added:
        print(f"  {tid:4s} + {f}")
    print()
    print(f"=== 新登记的高冲突共享测试文件（{len(added_hcf)} 项）===")
    for path, ids in added_hcf:
        print(f"  {path}")
        print(f"      共用任务: {ids}")
    print()
    print("=== 各任务 writes 数量（上限 8，T01 为显式豁免）===")
    for task in doc["tasks"]:
        n = len(task.get("writes") or [])
        if n > 8:
            exempt = "  [豁免]" if task.get("granularity_exempt") else "  <-- 超限！"
            print(f"  {task['id']:4s} {n} 项{exempt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
