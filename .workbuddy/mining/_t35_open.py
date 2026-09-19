# -*- coding: utf-8 -*-
"""T35 开工登记：requirements.txt 注释补 M2 用途 + PROGRESS.json 立 in_progress。

writes 3 项均 ≤8 无需 C12 豁免；scipy>=1.13 已由 M1a 显式声明（2026-09-16），
本卡仅在注释中补 M2 统计层 8 方法用途，满足 writes 声明的合法写入。
"""
import io
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime.now().astimezone().isoformat(timespec="seconds")

# ── 1. requirements.txt：scipy 注释补 M2 统计层用途（值不变） ─────────
rpath = ROOT / "requirements.txt"
text = rpath.read_text(encoding="utf-8")
old_line = "# scipy 此前仅作为 scikit-learn 的传递依赖存在（环境内已装 1.18.0），"
if "M2 统计层 8 方法" not in text:
    assert old_line in text, "requirements.txt 注释锚点缺失"
    text = text.replace(
        old_line,
        "# scipy 此前仅作为 scikit-learn 的传递依赖存在（环境内已装 1.18.0），\n"
        "# M2 统计层 8 方法（t 检验/Bootstrap/置换/DSR 偏度峰度）为其直接消费方，",
    )
    rpath.write_text(text, encoding="utf-8")
    print("requirements.txt: scipy 注释补 M2 用途")
else:
    print("requirements.txt: 已含 M2 用途说明，跳过")

# ── 2. tasks.json：核对 T35 writes（只读校验） ────────────────────────
tpath = ROOT / ".workbuddy/mining/tasks.json"
tasks = json.loads(tpath.read_text(encoding="utf-8"))
t35 = next(t for t in tasks["tasks"] if t["id"] == "T35")
print("tasks.json T35 card OK, writes =", t35["writes"])

# ── 3. PROGRESS.json：立 T35 in_progress 条目 ─────────────────────────
ppath = ROOT / ".workbuddy/mining/PROGRESS.json"
prog = json.loads(ppath.read_text(encoding="utf-8"))
tasks_sec = prog["tasks"]
assert "T35" not in tasks_sec, "T35 已有条目，拒绝重复登记"
tasks_sec["T35"] = {
    "status": "in_progress",
    "agent": "agent-senior-dev",
    "started_at": NOW,
    "artifacts": [],
    "evidence": [],
}
prog["updated_at"] = NOW
ppath.write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("PROGRESS.json T35 ->", tasks_sec["T35"]["status"], "@", NOW)
