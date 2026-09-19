"""一次性迁移：修正 T03/T04 的注册点缺口（2026-09-16，T03 开工时发现）。

问题
----
任务卡写「注册三处联动：FUNCTION_CATALOG + ALLOWED_FUNCS + FUNCTION_DIRECTION」。
实测**少了一处**：`formula_catalog.py:534` 用 `meta = _FUNCTION_META[key]` **硬索引**遍历
`FUNCTION_CATALOG`。往目录加算子而不加 `_FUNCTION_META` 条目 → `KeyError` →
`build_formula_catalog()` 抛异常 → 因子中心公式目录 API（`app/api/routes/factors.py:390/529`）
500，公式编辑器整页失效。

实证（2026-09-16）：
    >>> fc.FUNCTION_CATALOG['cs_rank'] = fc.FunctionSpec(...)
    >>> fcat.build_formula_catalog(None)
    KeyError: 'cs_rank'

做法
----
1. 把 `app/services/factors/formula_catalog.py` 补进 T03 / T04 的 writes（二者都要加算子）。
2. 把它登记进 `high_conflict_files`（T03 / T04 / T06 共用）。
3. 把 T03 / T04 的 pitfalls 从「注册三处」更正为「注册四处」。

幂等：已存在的项不重复添加；重跑无副作用。执行完毕后本文件可安全删除。
"""
from __future__ import annotations

import json
import pathlib

TASKS = pathlib.Path(".workbuddy/mining/tasks.json")
CATALOG = "app/services/factors/formula_catalog.py"
OWNERS = ["T03", "T04", "T06"]

OLD_PITFALL = "注册三处联动：FUNCTION_CATALOG + indicator_ast_sandbox.ALLOWED_FUNCS + FUNCTION_DIRECTION"
NEW_PITFALL = (
    "★ 注册**四处**联动（任务卡原文写「三处」，实测漏了第 4 处）："
    "① `FUNCTION_CATALOG` ② `indicator_ast_sandbox.ALLOWED_FUNCS` ③ `FUNCTION_DIRECTION` "
    "④ `formula_catalog._FUNCTION_META` —— 第 4 处是**硬索引**"
    "（`formula_catalog.py:534` 的 `meta = _FUNCTION_META[key]`），漏了会 KeyError，"
    "导致 `build_formula_catalog()` 抛异常、因子中心公式目录 API（factors.py:390/529）500。"
    "实证：加 `FUNCTION_CATALOG['cs_rank']` 后调 `build_formula_catalog()` → `KeyError: 'cs_rank'`"
)


def main() -> int:
    raw = TASKS.read_text(encoding="utf-8")
    doc = json.loads(raw)
    by_id = {t["id"]: t for t in doc["tasks"]}
    log: list[str] = []

    # 1) writes 补 formula_catalog.py
    for tid in ("T03", "T04"):
        w = by_id[tid].setdefault("writes", [])
        if CATALOG not in w:
            w.append(CATALOG)
            log.append(f"{tid}: writes += {CATALOG}")

    # 2) high_conflict_files 登记
    hcf = doc.setdefault("high_conflict_files", {})
    if CATALOG not in hcf:
        hcf[CATALOG] = OWNERS
        log.append(f"high_conflict_files[{CATALOG}] = {OWNERS}")

    # 3) pitfalls 更正「三处」→「四处」
    for tid in ("T03", "T04"):
        pits = by_id[tid].setdefault("pitfalls", [])
        for i, p in enumerate(pits):
            if OLD_PITFALL in p:
                pits[i] = p.replace(OLD_PITFALL, NEW_PITFALL)
                log.append(f"{tid}: pitfalls 更正为「四处」")
                break

    new = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    TASKS.write_text(new, encoding="utf-8")

    print(f"tasks.json: {len(raw)} -> {len(new)} bytes")
    print()
    if log:
        print("=== 变更 ===")
        for l in log:
            print("  " + l)
    else:
        print("=== 无变更（幂等）===")
    print()
    print("=== 复核 ===")
    for tid in ("T03", "T04", "T06"):
        print(f"  {tid} writes: {by_id[tid]['writes']}")
    print(f"  high_conflict_files 含 {CATALOG} -> {hcf.get(CATALOG)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
