#!/usr/bin/env python3
"""因子挖掘 · Agent 任务选择器。

读 `tasks.json`（任务索引，人工维护）+ `PROGRESS.json`（状态账本，Agent 读写），
输出"现在可以开工的任务"，并自动排除写权限冲突。

用法：
    python next_task.py                  # 输出前 3 个可开工任务
    python next_task.py --task T23       # 看某任务详情
    python next_task.py --status         # 整体进度
    python next_task.py --blocked        # 阻塞任务与等待对象
    python next_task.py --safe           # 额外过滤写权限冲突（默认已开）
    python next_task.py --list-all       # 全部任务一览

设计意图：把"我该做什么"从 Agent 的主观判断变成脚本的确定性输出，
避免多 Agent 抢同一任务或改同一文件。

纯标准库实现，任何 Python 3.9+ 可跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS_FILE = HERE / "tasks.json"
PROGRESS_FILE = HERE / "PROGRESS.json"

BUDGET_ORDER = {"S": 0, "M": 1, "L": 2}
STATUS_ORDER = {"in_progress": 0, "blocked": 1, "pending": 2, "done": 3, "skipped": 4}

# ── 终端色（无 TTY 时自动降级为纯文本） ──────────────────────
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def red(s: str) -> str:
    return _c(s, "31")


def green(s: str) -> str:
    return _c(s, "32")


def yellow(s: str) -> str:
    return _c(s, "33")


def cyan(s: str) -> str:
    return _c(s, "36")


def bold(s: str) -> str:
    return _c(s, "1")


def dim(s: str) -> str:
    return _c(s, "2")


# ══════════════════════════════════════════════════════════
# 载入
# ══════════════════════════════════════════════════════════


def load() -> tuple[dict, dict]:
    if not TASKS_FILE.exists():
        sys.exit(f"[FATAL] 缺少 {TASKS_FILE}（任务索引）")
    if not PROGRESS_FILE.exists():
        sys.exit(
            f"[FATAL] 缺少 {PROGRESS_FILE}（状态账本）。"
            "本文件是进度唯一事实来源，请先创建后再启动 Agent。"
        )
    try:
        tasks_doc = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        progress_doc = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"[FATAL] JSON 解析失败：{exc}\n请先修复文件语法（常见：尾逗号、缺引号）。")
    return tasks_doc, progress_doc


def index_tasks(tasks_doc: dict) -> dict[str, dict]:
    return {t["id"]: t for t in tasks_doc.get("tasks", [])}


def status_of(task_id: str, progress_doc: dict) -> str:
    """未登记 = pending（无需预先写 40 条）。"""
    entry = (progress_doc.get("tasks") or {}).get(task_id) or {}
    st = entry.get("status") or "pending"
    return st if st in STATUS_ORDER else "pending"


def entry_of(task_id: str, progress_doc: dict) -> dict:
    return (progress_doc.get("tasks") or {}).get(task_id) or {}


# ══════════════════════════════════════════════════════════
# 写权限冲突
# ══════════════════════════════════════════════════════════


def _norm(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.endswith("/"):
        p = p[:-1]
    return p


def _glob_prefix_ok(a: str, b: str) -> bool:
    """处理 'alembic/versions/0054_*.py' 这类带通配的声明。"""
    a_base, b_base = a.split("*")[0], b.split("*")[0]
    return a_base.startswith(b_base) or b_base.startswith(a_base)


def paths_conflict(a: str, b: str) -> bool:
    """两个写路径是否冲突（保守判定：宁可误报，不可漏报）。"""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    # 目录前缀关系
    if a.startswith(b + "/") or b.startswith(a + "/"):
        return True
    # 通配前缀
    if "*" in a or "*" in b:
        return _glob_prefix_ok(a, b)
    return False


def tasks_conflict(t1: dict, t2: dict, tasks_doc: dict) -> bool:
    """两个任务是否写权限冲突。

    检查两处：
      ① 直接路径重叠（writes 数组）
      ② tasks.json 的 high_conflict_files 把两者列在同一键下
         （用于 alembic/versions、i18n 两份这类"目录不同但必须串行"的情形）
    """
    for a in t1.get("writes", []):
        for b in t2.get("writes", []):
            if paths_conflict(a, b):
                return True

    ids1, ids2 = t1["id"], t2["id"]
    for _key, members in (tasks_doc.get("high_conflict_files") or {}).items():
        if ids1 in members and ids2 in members:
            return True
    return False


def in_progress_tasks(tasks_idx: dict[str, dict], progress_doc: dict) -> list[dict]:
    return [t for tid, t in tasks_idx.items() if status_of(tid, progress_doc) == "in_progress"]


# ══════════════════════════════════════════════════════════
# 可开工判定
# ══════════════════════════════════════════════════════════


def unmet_deps(task: dict, progress_doc: dict) -> list[str]:
    return [d for d in task.get("deps", []) if status_of(d, progress_doc) != "done"]


def blocking_writers(task: dict, tasks_idx: dict[str, dict], progress_doc: dict) -> list[str]:
    return [
        other["id"]
        for other in in_progress_tasks(tasks_idx, progress_doc)
        if other["id"] != task["id"] and tasks_conflict(task, other, {"high_conflict_files": _HCF})
    ]


_HCF: dict = {}


def sort_key(task: dict):
    """排序：有门禁优先 → 预算小优先 → ID 小优先。"""
    return (
        0 if task.get("gate") else 1,
        BUDGET_ORDER.get(task.get("budget", "M"), 1),
        task["id"],
    )


def candidates(tasks_doc: dict, tasks_idx: dict[str, dict], progress_doc: dict):
    """返回 (runnable, wait_deps, wait_conflict)。"""
    runnable, wait_deps, wait_conflict = [], [], []
    for task in tasks_idx.values():
        if status_of(task["id"], progress_doc) != "pending":
            continue
        missing = unmet_deps(task, progress_doc)
        if missing:
            wait_deps.append((task, missing))
            continue
        blockers = blocking_writers(task, tasks_idx, progress_doc)
        if blockers:
            wait_conflict.append((task, blockers))
            continue
        runnable.append(task)
    runnable.sort(key=sort_key)
    return runnable, wait_deps, wait_conflict


# ══════════════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════════════


def print_task_card(task: dict, indent: str = "  ") -> None:
    gate = f"  {yellow('门禁 ' + task['gate'])}" if task.get("gate") else ""
    print(f"{indent}{bold(task['id'])}  {task['title']}{gate}")
    print(f"{indent}  阶段 {task['phase']} ｜ 预算 {task['budget']} ｜ 前置 {task.get('deps') or '无'}")
    if task.get("writes"):
        print(f"{indent}  写权限 {len(task['writes'])} 项:")
        for w in task["writes"][:6]:
            print(f"{indent}    - {w}")
        if len(task["writes"]) > 6:
            print(f"{indent}    … 其余 {len(task['writes']) - 6} 项见 tasks.json")
    if task.get("dod"):
        print(f"{indent}  DoD:")
        for d in task["dod"]:
            print(f"{indent}    $ {d.get('cmd')}")
            print(f"{indent}      -> {d.get('expect')}")
    if task.get("pitfalls"):
        print(f"{indent}  {red('坑:')}")
        for p in task["pitfalls"]:
            print(f"{indent}    ! {p}")
    print()


def cmd_default(tasks_doc, tasks_idx, progress_doc, top_n: int = 3) -> int:
    runnable, wait_deps, wait_conflict = candidates(tasks_doc, tasks_idx, progress_doc)

    print(bold("=== 现在可以开工的任务 ==="))
    print()

    if not runnable:
        print(red("  没有可开工的任务。原因如下："))
        print()
        if wait_conflict:
            print(yellow(f"  [写权限冲突] {len(wait_conflict)} 个任务被在跑任务挡住："))
            for task, blockers in wait_conflict[:5]:
                print(f"    {task['id']} {task['title']}  <- 等待 {blockers}")
            print()
        if wait_deps:
            print(yellow(f"  [前置未完成] {len(wait_deps)} 个任务："))
            for task, missing in wait_deps[:5]:
                print(f"    {task['id']}  <- 缺 {missing}")
            print()
        done = sum(1 for t in tasks_idx if status_of(t, progress_doc) == "done")
        if done == len(tasks_idx):
            print(green("  全部任务已完成。"))
        return 1

    for i, task in enumerate(runnable[:top_n], 1):
        print(cyan(f"--- 候选 {i}/{min(top_n, len(runnable))} ---"))
        print_task_card(task)

    if len(runnable) > top_n:
        print(dim(f"  另有 {len(runnable) - top_n} 个可开工任务，用 --list-all 查看。"))
        print()

    print(bold("下一步（按顺序做）:"))
    print(f"  1. 读 {cyan('PROGRESS.json')} 中前置任务的 decisions / notes 字段")
    print(f"  2. 把选中任务在 PROGRESS.json 里置为 {cyan('in_progress')} 并填 agent（先占位再动手）")
    print(f"  3. 只读 tasks.json 里该任务的 reads 白名单")
    print(f"  4. 收工前跑完 DoD 全部命令，记录 cmd + 实际 exit 到 evidence")
    print(f"  详情: python next_task.py --task <ID>")
    return 0


def cmd_task(tasks_doc, tasks_idx, progress_doc, task_id: str) -> int:
    task = tasks_idx.get(task_id)
    if not task:
        print(red(f"未知任务 ID: {task_id}"))
        print(f"可用: {', '.join(sorted(tasks_idx))}")
        return 1

    st = status_of(task_id, progress_doc)
    ent = entry_of(task_id, progress_doc)

    color = {
        "done": green, "in_progress": cyan, "blocked": red, "pending": dim, "skipped": dim,
    }.get(st, str)

    print(bold(f"=== {task['id']} · {task['title']} ==="))
    print(f"  状态: {color(st)}")
    if ent.get("agent"):
        print(f"  执行者: {ent['agent']}")
    if ent.get("blocked_reason"):
        print(f"  {red('阻塞原因')}: {ent['blocked_reason']}")
    if ent.get("needs"):
        print(f"  {yellow('需要')}: {ent['needs']}")
    if ent.get("decisions"):
        print(f"  {yellow('已定决策（不要重新决策）')}:")
        for d in ent["decisions"]:
            print(f"    - {d}")
    if ent.get("notes"):
        print(f"  笔记: {ent['notes']}")
    if ent.get("evidence"):
        print(f"  证据:")
        for e in ent["evidence"]:
            print(f"    - {e}")
    print()

    missing = unmet_deps(task, progress_doc)
    if missing:
        print(red(f"  ! 前置未完成: {missing}"))
    blockers = blocking_writers(task, tasks_idx, progress_doc)
    if blockers:
        print(red(f"  ! 写权限被占: {blockers}"))
    if not missing and not blockers and st == "pending":
        print(green("  ✓ 可立即开工"))
    print()

    print(bold("任务卡:"))
    print_task_card(task, indent="  ")

    if task.get("reads"):
        print("  允许读（不要超范围）:")
        for r in task["reads"]:
            print(f"    - {r}")
        print()
    if task.get("not_do"):
        print("  明确不做:")
        for n in task["not_do"]:
            print(f"    x {n}")
        print()
    if task.get("required_assertions"):
        print("  必测断言:")
        for a in task["required_assertions"]:
            print(f"    * {a}")
        print()
    return 0


def cmd_status(tasks_doc, tasks_idx, progress_doc) -> int:
    counts = {"done": 0, "in_progress": 0, "blocked": 0, "pending": 0, "skipped": 0}
    by_phase: dict[str, list[str]] = {}

    for tid, task in tasks_idx.items():
        st = status_of(tid, progress_doc)
        counts[st] += 1
        by_phase.setdefault(task["phase"], []).append(st)

    total = len(tasks_idx)
    done = counts["done"]
    pct = (done / total * 100) if total else 0

    print(bold("=== 整体进度 ==="))
    bar_width = 40
    filled = int(bar_width * done / total) if total else 0
    print(f"  [{green('#' * filled)}{dim('-' * (bar_width - filled))}] {done}/{total}  {pct:.0f}%")
    print()
    print(f"  {green('done')}         {counts['done']}")
    print(f"  {cyan('in_progress')}  {counts['in_progress']}")
    print(f"  {red('blocked')}      {counts['blocked']}")
    print(f"  {dim('pending')}      {counts['pending']}")
    if counts["skipped"]:
        print(f"  skipped      {counts['skipped']}")
    print()

    print(bold("按阶段:"))
    order = ["PH0", "PH1", "PH2", "PH3", "PH4", "PH5", "PH6", "PH7", "TDB"]
    for phase in order:
        sts = by_phase.get(phase)
        if not sts:
            continue
        d = sts.count("done")
        mark = green("✓") if d == len(sts) else (cyan("~") if "in_progress" in sts else dim("·"))
        print(f"  {mark} {phase}  {d}/{len(sts)}")
    print()

    print(bold("门禁:"))
    for gid, g in (tasks_doc.get("gates") or {}).items():
        members = [m.strip() for m in str(g.get("task", "")).split(",") if m.strip()]
        if not members:
            continue
        ok = all(status_of(m, progress_doc) == "done" for m in members)
        mark = green("PASS") if ok else dim("----")
        print(f"  {mark}  {gid}  {g.get('name')}  ({g.get('desc')})")
    print()
    print(f"  {dim('updated_at: ' + str(progress_doc.get('updated_at')))}")
    return 0


def cmd_blocked(tasks_doc, tasks_idx, progress_doc) -> int:
    blocked = [(t, entry_of(t, progress_doc)) for t in sorted(tasks_idx)
               if status_of(t, progress_doc) == "blocked"]
    inprog = [t for t in sorted(tasks_idx) if status_of(t, progress_doc) == "in_progress"]

    print(bold("=== 阻塞任务 ==="))
    print()
    if not blocked:
        print(green("  无阻塞任务。"))
    for tid, ent in blocked:
        task = tasks_idx[tid]
        print(f"  {red(tid)}  {task['title']}")
        print(f"      原因: {ent.get('blocked_reason', '(未填写)')}")
        if ent.get("needs"):
            print(f"      {yellow('需要')}: {ent['needs']}")
        if ent.get("partial_artifacts"):
            print(f"      已产出: {ent['partial_artifacts']}")
        missing = unmet_deps(task, progress_doc)
        if missing:
            print(f"      等前置: {missing}")
        print()
    if not blocked:
        print()

    print(bold("=== 正在跑的任务（写权限已占用）==="))
    print()
    if not inprog:
        print(dim("  无。"))
    for tid in inprog:
        task = tasks_idx[tid]
        ent = entry_of(tid, progress_doc)
        print(f"  {cyan(tid)}  {task['title']}   agent={ent.get('agent', '?')}")
        print(f"      占用文件: {task.get('writes', [])[:4]}{' …' if len(task.get('writes', [])) > 4 else ''}")
        if ent.get("notes"):
            print(f"      笔记: {ent['notes']}")
        print()

    obs = progress_doc.get("observations") or []
    if obs:
        print(bold("=== 观察记录（Agent 顺手发现但不应自行修的问题）==="))
        for o in obs:
            print(f"  - {o}")
        print()
    return 0


def cmd_list_all(tasks_doc, tasks_idx, progress_doc) -> int:
    print(bold("=== 全部任务 ==="))
    print()
    current_phase = None
    for task in sorted(tasks_idx.values(), key=lambda t: t["id"]):
        if task["phase"] != current_phase:
            current_phase = task["phase"]
            print(bold(f"  [{current_phase}]"))
        st = status_of(task["id"], progress_doc)
        mark = {
            "done": green("done   "),
            "in_progress": cyan("running"),
            "blocked": red("blocked"),
            "skipped": dim("skipped"),
            "pending": dim("pending"),
        }.get(st, dim("pending"))
        gate = f" {yellow('(' + task['gate'] + ')')}" if task.get("gate") else ""
        budget = task.get("budget", "M")
        print(f"    {mark} {task['id']}  [{budget}] {task['title']}{gate}")
    print()
    return 0


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════


def main() -> int:
    global _HCF
    parser = argparse.ArgumentParser(
        prog="next_task.py",
        description="因子挖掘 Agent 任务选择器（读 tasks.json + PROGRESS.json）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--task", metavar="ID", help="查看某任务详情（如 T23）")
    parser.add_argument("--status", action="store_true", help="整体进度 + 门禁状态")
    parser.add_argument("--blocked", action="store_true", help="阻塞任务 + 在跑任务 + 观察记录")
    parser.add_argument("--list-all", action="store_true", help="全部任务一览")
    parser.add_argument("--top", type=int, default=3, help="默认视图输出几个候选（默认 3）")
    parser.add_argument(
        "--safe",
        action="store_true",
        help="（默认已启用）排除写权限冲突的任务；此开关为显式声明，无额外行为",
    )
    args = parser.parse_args()

    tasks_doc, progress_doc = load()
    tasks_idx = index_tasks(tasks_doc)
    if not tasks_idx:
        sys.exit("[FATAL] tasks.json 里没有任务。")

    _HCF = tasks_doc.get("high_conflict_files") or {}

    if args.task:
        return cmd_task(tasks_doc, tasks_idx, progress_doc, args.task)
    if args.status:
        return cmd_status(tasks_doc, tasks_idx, progress_doc)
    if args.blocked:
        return cmd_blocked(tasks_doc, tasks_idx, progress_doc)
    if args.list_all:
        return cmd_list_all(tasks_doc, tasks_idx, progress_doc)
    return cmd_default(tasks_doc, tasks_idx, progress_doc, args.top)


if __name__ == "__main__":
    raise SystemExit(main())
