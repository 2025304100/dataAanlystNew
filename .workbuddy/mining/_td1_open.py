#!/usr/bin/env python3
"""TD1 开工登记（幂等）：
1. PROGRESS.json：tasks["TD1"] 置 in_progress + observation 记录开工修正
2. tasks.json：TD1 DoD ② 收窄为 --tables factor_audit_logs（全量 48/127 既有漂移挡路）
   + pitfalls 追加两条开工发现
3. 自检：_selfcheck_conflict.py（C19 重复键等）

重跑安全：所有写入前判重。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")

# ── 1. PROGRESS.json ─────────────────────────────────────────
prog_path = HERE / "PROGRESS.json"
prog = json.loads(prog_path.read_text(encoding="utf-8"))

tasks = prog.setdefault("tasks", {})
entry = tasks.get("TD1")
if entry is None:
    tasks["TD1"] = {
        "status": "in_progress",
        "agent": "agent-senior-dev",
        "started_at": now,
    }
    print("[PROGRESS] TD1 已置 in_progress")
else:
    if entry.get("status") != "done":
        entry["status"] = "in_progress"
        entry.setdefault("started_at", now)
        print(f"[PROGRESS] TD1 已存在（{entry.get('status')}），确认 in_progress")
    else:
        print("[PROGRESS] TD1 已 done，跳过状态改写")

obs_note = (
    "TD1 开工修正：DoD ② 全量 verify_schema_drift 不可达成 —— 真实库全量基线实测 "
    "48/127 张表漂移（TD4 账务列/TD5 全库核对的既有标的），收窄为 "
    "--tables factor_audit_logs；全量基线已存档 .workbuddy/mining/_td1_drift_baseline.txt。"
    "另确认：server 默认引擎仍为 MyISAM（MySQL 5.7.26），全新安装路径上 0050 建表"
    "会再次吞 FK → 原地补 mysql_engine=InnoDB（T01 修 0053 同款先例）。"
)
obs = prog.setdefault("observations", [])
if isinstance(obs, list) and not any(obs_note[:40] in o for o in obs):
    obs.append(obs_note)
    print("[PROGRESS] observation 已追加")

prog["updated_at"] = now
prog["updated_by"] = "agent-senior-dev"

prog_path.write_text(
    json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

# ── 2. tasks.json：TD1 DoD ② 收窄 + pitfalls ─────────────────
tasks_path = HERE / "tasks.json"
tj = json.loads(tasks_path.read_text(encoding="utf-8"))
td1 = next(t for t in tj["tasks"] if t.get("id") == "TD1")

new_cmd = "{PY} .workbuddy/mining/verify_schema_drift.py --tables factor_audit_logs"
dod2 = td1["dod"][1]
assert "verify_schema_drift" in dod2["cmd"], "DoD ② 结构与预期不符"
if "--tables" not in dod2["cmd"]:
    dod2["cmd"] = new_cmd
    dod2["expect"] = (
        "exit 0：factor_audit_logs 与 ORM 零漂移（列/索引/外键/SET NULL 全一致）；"
        "全量跑另有 48/127 既有漂移（TD4/TD5 标的），不属本卡"
    )
    print("[tasks] TD1 DoD ② 已收窄为 --tables factor_audit_logs")

pit_additions = [
    "★ 开工修正（2026-09-18）：真实库全量 drift 基线 48/127 张表漂移（TD4/TD5 的既有标的）"
    "挡路，DoD ② 收窄为 --tables factor_audit_logs；基线存档 _td1_drift_baseline.txt",
    "★ 全新安装路径同样会丢 FK：server 默认引擎实测仍为 MyISAM（MySQL 5.7.26），"
    "0050 建表没声明 mysql_engine → 原地补 InnoDB（T01 修 0053 同款先例）；"
    "0056 负责既有库回填，两条路径都要成立「迁移才是对的」",
]
for p in pit_additions:
    if not any(p[:30] in x for x in td1["pitfalls"]):
        td1["pitfalls"].append(p)
        print("[tasks] pitfalls 已追加")

tasks_path.write_text(
    json.dumps(tj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

# ── 3. 自检 ─────────────────────────────────────────────────
r = subprocess.run(
    [sys.executable, str(HERE / "_selfcheck_conflict.py")],
    capture_output=True, text=True, encoding="utf-8",
)
tail = "\n".join(r.stdout.strip().splitlines()[-3:])
print(tail)
if r.returncode != 0:
    sys.exit(f"[FATAL] 自检失败（exit {r.returncode}）\n{r.stdout}\n{r.stderr}")
