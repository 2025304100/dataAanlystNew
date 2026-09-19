"""T05 开工前的排期修正 + 占位。

两件事
------
1. **补 T05 的写权限**：方言归一必须挂到编译入口 `factor_compiler.compile()` 才生效
   （需求 §6.0：「归一仅在公式内生效」）。但 T05 的 `writes` 只列了
   `dsl/dialect.py` + `factor_executor.py` + 测试 —— 缺 `factor_compiler.py`，
   照原卡片做**方言归一根本接不上**。
   注：`high_conflict_files` 里早已登记 `factor_compiler.py -> [T03,T04,T05,T06]`，
   说明排期本意就包含 T05，只是 `writes` 漏写（第三处同类缺口，前两处见
   `_migrate_writes_v2.py` / `_migrate_registry_sites_v3.py`）。

2. **把 T05 置为 in_progress**。这次**先检查是否已有同名占位**，再插入 ——
   2026-09-16 的重复键事故（T04 记录被静默覆盖）就是这么来的。

脚本自身也带重复键预检：写回前先扫，发现重复立刻中止。
"""
from __future__ import annotations

import json
import pathlib

TASKS = pathlib.Path(".workbuddy/mining/tasks.json")
PROGRESS = pathlib.Path(".workbuddy/mining/PROGRESS.json")
CATALOG = "app/services/factors/factor_compiler.py"


class DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise DuplicateKeyError(key)
        seen.add(key)
    return dict(pairs)


def assert_no_duplicate_keys(path: pathlib.Path) -> None:
    """写回前预检：JSON 重复键是静默的（取后者），必须先挡住。"""
    try:
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except DuplicateKeyError as exc:
        raise SystemExit(f"[ABORT] {path.name} 含重复键 {exc.key!r}，先修再写") from exc


def main() -> int:
    assert_no_duplicate_keys(TASKS)
    assert_no_duplicate_keys(PROGRESS)

    # ── 1. T05 写权限补 factor_compiler.py ──────────────────
    tasks_doc = json.loads(TASKS.read_text(encoding="utf-8"))
    t05 = next(t for t in tasks_doc["tasks"] if t["id"] == "T05")
    if CATALOG not in t05["writes"]:
        t05["writes"].append(CATALOG)
        print(f"  T05 writes += {CATALOG}")
    else:
        print("  T05 writes 已含 factor_compiler.py（幂等）")
    t05["writes"].sort(key=lambda p: (p.startswith("tests/"), p))
    print(f"  T05 writes = {t05['writes']}")
    TASKS.write_text(
        json.dumps(tasks_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ── 2. PROGRESS 占位（先查重）─────────────────────────
    progress = json.loads(PROGRESS.read_text(encoding="utf-8"))
    tasks = progress["tasks"]
    existing = tasks.get("T05")
    if existing and existing.get("status") not in (None, "pending"):
        print(f"  [WARN] T05 已有非 pending 记录：{existing.get('status')} —— 不覆盖")
    else:
        tasks["T05"] = {
            "status": "in_progress",
            "agent": "agent-senior-dev",
            "started_at": "2026-09-16T16:00:00+08:00",
            "artifacts": [
                "app/services/factors/dsl/dialect.py",
                "app/services/factors/factor_executor.py",
                "app/services/factors/factor_compiler.py",
                "tests/services/factors/mining/test_dialect_and_executor.py",
            ],
        }
        print("  T05 -> in_progress（先查过无同名占位）")
    progress["updated_at"] = "2026-09-16T16:00:00+08:00"
    PROGRESS.write_text(
        json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ── 3. 写回后再验一次，确保没引入重复键 ─────────────────
    assert_no_duplicate_keys(TASKS)
    assert_no_duplicate_keys(PROGRESS)
    print("  写回后重复键复检：OK")
    print()
    print("=== tasks 状态 ===")
    for tid, entry in progress["tasks"].items():
        print(f"  {tid}: {entry['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
