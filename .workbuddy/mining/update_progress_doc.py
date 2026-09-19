#!/usr/bin/env python3
"""从 PROGRESS.json 生成人可读的开发进度看板。

为什么是"生成"而不是"手填"
--------------------------
多 Agent 并行时，如果进度同时存在于 `PROGRESS.json`（机器读）和一份手写文档（人读），
两者必然发散——这正是多 Agent 协作最典型的失败模式。所以：
  - **唯一事实来源**：`.workbuddy/mining/PROGRESS.json`
  - 本脚本把它渲染成 `docs/因子挖掘-开发进度看板.md`
  - **不要手工编辑那份 md**，会被下次生成覆盖

用法：
    python update_progress_doc.py                 # 生成 + 写入 docs/
    python update_progress_doc.py --stdout        # 只打印，不写文件
    python update_progress_doc.py --out <路径>     # 写到指定路径
    python update_progress_doc.py --changelog 30  # 变更日志最多显示 30 条（默认 20）

纯标准库实现。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
TASKS_FILE = HERE / "tasks.json"
PROGRESS_FILE = HERE / "PROGRESS.json"
DEFAULT_OUT = REPO_ROOT / "docs" / "因子挖掘-开发进度看板.md"

PHASE_NAMES = {
    "PH0": "校准与哨兵",
    "PH1": "DSL 与数据地基",
    "PH2": "候选池域",
    "PH3": "任务与锁",
    "PH4": "进化闭环",
    "PH5": "前端骨架",
    "PH6": "M2 算法",
    "PH7": "M3 收尾",
    "TDB": "技术债修复",
}

STATUS_GLYPH = {
    "done": "✅",
    "in_progress": "🔄",
    "blocked": "⛔",
    "pending": "⬜",
    "skipped": "⏭️",
}

STATUS_LABEL = {
    "done": "已完成",
    "in_progress": "进行中",
    "blocked": "已阻塞",
    "pending": "未开始",
    "skipped": "已跳过",
}


# ══════════════════════════════════════════════════════════
# 载入与派生
# ══════════════════════════════════════════════════════════


def load():
    tasks_doc = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    idx = {t["id"]: t for t in tasks_doc["tasks"]}
    return tasks_doc, progress, idx


def st_of(tid: str, progress: dict) -> str:
    entry = (progress.get("tasks") or {}).get(tid) or {}
    value = entry.get("status") or "pending"
    return value if value in STATUS_GLYPH else "pending"


def ent_of(tid: str, progress: dict) -> dict:
    return (progress.get("tasks") or {}).get(tid) or {}


def bar(done: int, total: int, width: int = 28) -> str:
    if total <= 0:
        return "`" + "-" * width + "`"
    filled = int(round(width * done / total))
    return "`" + "█" * filled + "░" * (width - filled) + "`"


def pct(done: int, total: int) -> str:
    return f"{done / total * 100:.1f}%" if total else "0.0%"


def esc(text: str) -> str:
    """转义 markdown 表格里会破坏结构的字符。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def short(text: str, n: int) -> str:
    text = esc(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def ev_text(item: object) -> str:
    """把一条验收证据规整成字符串。

    `evidence` / `decisions` 允许两种写法（两种都要支持，否则账本格式一升级看板就崩）：

    - **字符串**：`"93 passed / exit 0"`
    - **结构化字典**：`{"cmd": "...", "exit": 0, "summary": "..."}`
      —— 更利于机器核对（能看出「跑了哪条命令、退出码多少」）。

    ⚠️ 历史事故（2026-09-16，T06）：生成器原先直接对元素调 `.replace()`，
    于是 T06 首次使用字典形式写 evidence 时，整个看板**渲染崩溃**（`AttributeError:
    'dict' object has no attribute 'replace'`）。**账本格式的演进必须兼容旧数据。**
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        cmd = str(item.get("cmd") or "").strip()
        exit_code = item.get("exit")
        summary = str(item.get("summary") or "").strip()
        head = f"`{cmd}`" if cmd else ""
        if exit_code is not None:
            head = f"{head} exit={exit_code}".strip()
        parts = [p for p in (head, summary) if p]
        return " — ".join(parts) if parts else str(item)
    return str(item)


# ══════════════════════════════════════════════════════════
# 渲染各段
# ══════════════════════════════════════════════════════════


def render_header(progress: dict, now: str) -> list[str]:
    out = [
        "# 因子挖掘 · 开发进度看板",
        "",
        "> ⚠️ **本文件由脚本自动生成，请勿手工编辑**（下次生成会覆盖）。",
        "> 唯一事实来源是 `.workbuddy/mining/PROGRESS.json`；要更新进度就改它，然后执行：",
        "> ```",
        "> .venv/Scripts/python.exe .workbuddy/mining/update_progress_doc.py",
        "> ```",
        f"> 生成时间：{now}　｜　账本更新人：`{progress.get('updated_by', '未知')}`"
        f"　｜　账本更新时间：`{progress.get('updated_at', '未知')}`",
    ]
    # 只有真的登记了裁决项才提示，否则会指向一个并不存在的 §9
    decision_count = len(progress.get("global_decisions") or [])
    if decision_count:
        out.append(
            f"> 已裁决项 {decision_count} 项，见 §9（**已拍板，不要再重开讨论**）"
        )
    out.append("")
    return out


def render_overview(tasks_doc, progress, idx) -> list[str]:
    total = len(idx)
    counts = {k: 0 for k in STATUS_GLYPH}
    for tid in idx:
        counts[st_of(tid, progress)] += 1
    done = counts["done"]

    out = ["## 1. 总览", ""]
    out.append(f"**进度 {done}/{total}（{pct(done, total)}）**")
    out.append("")
    out.append(bar(done, total))
    out.append("")
    out.append("| 状态 | 数量 |")
    out.append("|------|------|")
    for key in ("done", "in_progress", "blocked", "pending", "skipped"):
        if counts[key] or key in ("done", "in_progress", "blocked", "pending"):
            out.append(f"| {STATUS_GLYPH[key]} {STATUS_LABEL[key]} | {counts[key]} |")
    out.append("")

    if done == 0 and counts["in_progress"] == 0:
        out.append("> 尚未开工。第一个任务请用 `next_task.py` 获取，不要自行挑选。")
        out.append("")  # 必须留空行，否则下一行会被并入上面的引用块
    else:
        inprog = [t for t in sorted(idx) if st_of(t, progress) == "in_progress"]
        if inprog:
            out.append("**当前进行中**：" + "、".join(
                f"`{t}` {short(idx[t]['title'], 24)}" for t in inprog[:5]
            ))
        blocked = [t for t in sorted(idx) if st_of(t, progress) == "blocked"]
        if blocked:
            out.append("")
            out.append(f"**⚠️ 阻塞 {len(blocked)} 个**：" + "、".join(f"`{t}`" for t in blocked[:8]))
        out.append("")

    out.append(f"**当前批次**：`{progress.get('current_batch', '—')}`")
    out.append("")
    return out


def render_gates(tasks_doc, progress) -> list[str]:
    out = ["## 2. 门禁状态", ""]
    out.append("> 门禁是一条**必须 exit 0** 的命令，不是评审会。未过不得进入下一阶段。")
    out.append("")
    out.append("| 门禁 | 名称 | 状态 | 关联任务 | 含义 |")
    out.append("|------|------|------|---------|------|")

    passed_n = 0
    gates = tasks_doc.get("gates") or {}
    for gid, g in gates.items():
        members = [m.strip() for m in str(g.get("task", "")).split(",") if m.strip()]
        ok = bool(members) and all(st_of(m, progress) == "done" for m in members)
        any_running = any(st_of(m, progress) in ("in_progress", "done") for m in members)
        if ok:
            mark = "✅ 已通过"
            passed_n += 1
        elif any_running:
            mark = "🔄 进行中"
        else:
            mark = "⬜ 未开始"
        out.append(
            f"| **{gid}** | {esc(g.get('name', ''))} | {mark} | "
            f"{', '.join('`' + m + '`' for m in members)} | {esc(g.get('desc', ''))} |"
        )
    out.append("")
    out.append(f"**已通过 {passed_n}/{len(gates)} 个门禁**")
    out.append("")
    return out


def render_phases(tasks_doc, progress, idx) -> list[str]:
    out = ["## 3. 阶段进度", ""]
    order = list(PHASE_NAMES.keys())

    for phase in order:
        members = [t for t in tasks_doc["tasks"] if t.get("phase") == phase]
        if not members:
            continue
        ids = [t["id"] for t in members]
        done = sum(1 for i in ids if st_of(i, progress) == "done")
        blocked = sum(1 for i in ids if st_of(i, progress) == "blocked")
        running = sum(1 for i in ids if st_of(i, progress) == "in_progress")

        flag = ""
        if blocked:
            flag = f"  ⛔ {blocked}"
        elif running:
            flag = f"  🔄 {running}"

        out.append(f"**{phase} · {PHASE_NAMES[phase]}** — {done}/{len(ids)}（{pct(done, len(ids))}）{flag}")
        out.append("")
        out.append(bar(done, len(ids), width=20))
        out.append("")
        for t in members:
            tid = t["id"]
            st = st_of(tid, progress)
            gate = f" `{t['gate']}`" if t.get("gate") else ""
            out.append(f"- {STATUS_GLYPH[st]} `{tid}` {esc(t['title'])}{gate}")
        out.append("")
    return out


def render_detail_table(tasks_doc, progress, idx) -> list[str]:
    out = ["## 4. 任务明细", ""]
    out.append("> 「决策」列是接续的关键——下一个 Agent 靠它知道为什么这么做。")
    out.append("")
    out.append("| ID | 阶段 | 任务 | 状态 | 预算 | 门禁 | 执行者 | 完成时间 | 证据 | 决策 |")
    out.append("|----|------|------|------|------|------|--------|---------|------|------|")

    for t in sorted(tasks_doc["tasks"], key=lambda x: x["id"]):
        tid = t["id"]
        st = st_of(tid, progress)
        e = ent_of(tid, progress)

        evidence = "；".join(short(ev_text(x), 40) for x in (e.get("evidence") or [])[:2]) or "—"
        if isinstance(evidence, str) and not evidence:
            evidence = "—"
        decisions = "；".join(short(x, 40) for x in (e.get("decisions") or [])[:2]) or "—"
        if isinstance(decisions, str) and not decisions:
            decisions = "—"

        out.append(
            f"| `{tid}` | {t.get('phase', '')} | {short(t['title'], 30)} | "
            f"{STATUS_GLYPH[st]} {STATUS_LABEL[st]} | {t.get('budget', '')} | "
            f"{t.get('gate') or '—'} | {esc(e.get('agent') or '—')} | "
            f"{esc((e.get('finished_at') or '—')[:19])} | {evidence} | {decisions} |"
        )
    out.append("")
    return out


def render_running(progress, idx) -> list[str]:
    running = [t for t in sorted(idx) if st_of(t, progress) == "in_progress"]
    if not running:
        return []
    out = ["## 5. 进行中", ""]
    for tid in running:
        t, e = idx[tid], ent_of(tid, progress)
        out.append(f"### 🔄 `{tid}` {t['title']}")
        out.append("")
        out.append(f"- 执行者：`{e.get('agent', '未登记')}`")
        out.append(f"- 开始时间：{e.get('started_at', '未登记')}")
        if e.get("notes"):
            out.append(f"- 进度笔记：{e['notes']}")
        out.append(f"- 占用文件（其他任务不得改）：")
        for w in t.get("writes", []):
            out.append(f"  - `{w}`")
        out.append("")
    return out


def render_blocked(progress, idx) -> list[str]:
    blocked = [t for t in sorted(idx) if st_of(t, progress) == "blocked"]
    if not blocked:
        return []
    out = ["## 6. 阻塞项（需要人工裁决）", ""]
    out.append("| ID | 任务 | 阻塞原因 | 需要什么 | 已尝试 | 部分产物 |")
    out.append("|----|------|---------|---------|--------|---------|")
    for tid in blocked:
        t, e = idx[tid], ent_of(tid, progress)
        out.append(
            f"| `{tid}` | {short(t['title'], 24)} | {short(e.get('blocked_reason', '未填写'), 60)} | "
            f"{short(e.get('needs', '—'), 40)} | {short(e.get('attempted', '—'), 30)} | "
            f"{short(', '.join(e.get('partial_artifacts') or []) or '—', 40)} |"
        )
    out.append("")
    return out


def render_changelog(progress, idx, limit: int) -> list[str]:
    done = [(tid, ent_of(tid, progress)) for tid in idx if st_of(tid, progress) == "done"]
    if not done:
        return []
    done.sort(key=lambda kv: kv[1].get("finished_at") or "", reverse=True)

    out = ["## 7. 变更日志（最近完成的）", ""]
    for tid, e in done[:limit]:
        t = idx[tid]
        gate = f" · 门禁 `{t['gate']}`" if t.get("gate") else ""
        out.append(f"### ✅ `{tid}` {t['title']}　`{e.get('finished_at', '时间未登记')[:19]}`{gate}")
        out.append("")
        out.append(f"- 执行者：`{e.get('agent', '未登记')}`")
        if e.get("artifacts"):
            out.append("- 产物：")
            for a in e["artifacts"]:
                out.append(f"  - `{a}`")
        if e.get("evidence"):
            out.append("- 验收证据：")
            for ev in e["evidence"]:
                out.append(f"  - {esc(ev_text(ev))}")
        if e.get("decisions"):
            out.append("- **决策（后续 Agent 不要重新决策）**：")
            for d in e["decisions"]:
                out.append(f"  - {esc(d)}")
        if e.get("notes"):
            out.append(f"- 备注：{esc(e['notes'])}")
        out.append("")
    if len(done) > limit:
        out.append(f"> 另有 {len(done) - limit} 条较早记录，用 `--changelog N` 调整显示条数。")
        out.append("")
    return out


def render_observations(progress) -> list[str]:
    obs = progress.get("observations") or []
    if not obs:
        return []
    out = ["## 8. 观察记录", ""]
    out.append("> Agent 顺手发现但**不应自行修改**的问题，留给人工排期。")
    out.append("")
    for o in obs:
        out.append(f"- {esc(str(o))}")
    out.append("")
    return out


def render_decisions(progress: dict) -> list[str]:
    """渲染已裁决项（**已拍板，不要再重开讨论**）。"""
    items = progress.get("global_decisions") or []
    if not items:
        return []
    out = ["## 9. 已裁决项（**已拍板，不要再重开讨论**）", ""]
    out.append(
        "> 完整版见 SD-v2.0 §12.3 与手册 §3.6。"
        "**若认为需要改动，必须先提出新证据并走一次裁决**，不得以「看起来更合理」为由直接改代码。"
    )
    out.append("")
    out.append("| # | 议题 | **裁决结果** | 依据 |")
    out.append("|---|------|------------|------|")
    for d in items:
        out.append(
            f"| **{esc(str(d.get('id', '')))}** "
            f"| {esc(str(d.get('topic', '')))} "
            f"| **{esc(str(d.get('resolution', '')))}** "
            f"| `{esc(str(d.get('decided_at', '')))}` {esc(str(d.get('decided_by', '')))} |"
        )
    out.append("")
    for d in items:
        enforcement = str(d.get("enforcement") or "").strip()
        if enforcement:
            out.append(f"- **{esc(str(d.get('id', '')))} 遵守要求**：{esc(enforcement)}")
    out.append("")
    out.append(f"> 裁决来源：{esc(str(items[0].get('source', '')))}" if items else "")
    out.append("")
    return out

def render_footer(progress, idx) -> list[str]:
    total = len(idx)
    done = sum(1 for t in idx if st_of(t, progress) == "done")
    out = ["---", ""]
    out.append("## 附：如何更新本文件")
    out.append("")
    out.append("1. 只改 `.workbuddy/mining/PROGRESS.json`（**不要改本 md**）")
    out.append("2. 执行 `.venv/Scripts/python.exe .workbuddy/mining/update_progress_doc.py`")
    out.append("3. 本文件自动刷新")
    out.append("")
    out.append("Agent 的收工仪式（手册 §2.4）：跑完 DoD → 更新 PROGRESS.json → 跑本脚本 → 结束。")
    out.append("")
    if total and done == total:
        out.append("🎉 **全部任务已完成。**")
    elif done:
        out.append(f"剩余 {total - done} 个任务。用 `next_task.py` 查看下一个可开工任务。")
    else:
        out.append("尚未开工。用 `next_task.py` 查看第一个可开工任务。")
    out.append("")
    return out


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════


def build(changelog_limit: int = 20) -> str:
    tasks_doc, progress, idx = load()
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    lines: list[str] = []
    lines += render_header(progress, now)
    lines += render_overview(tasks_doc, progress, idx)
    lines += render_gates(tasks_doc, progress)
    lines += render_phases(tasks_doc, progress, idx)
    lines += render_detail_table(tasks_doc, progress, idx)
    lines += render_running(progress, idx)
    lines += render_blocked(progress, idx)
    lines += render_changelog(progress, idx, changelog_limit)
    lines += render_observations(progress)
    lines += render_decisions(progress)
    lines += render_footer(progress, idx)
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="update_progress_doc.py",
        description="从 PROGRESS.json 生成人可读的开发进度看板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    ap.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    ap.add_argument("--changelog", type=int, default=20, help="变更日志最多显示条数")
    args = ap.parse_args()

    try:
        content = build(args.changelog)
    except FileNotFoundError as exc:
        print(f"[FATAL] 缺少文件：{exc.filename}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"[FATAL] JSON 解析失败：{exc}\n请先修复 PROGRESS.json / tasks.json 语法。")
        return 1

    if args.stdout:
        print(content)
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"已生成 {out_path}  （{len(content.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
