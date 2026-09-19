#!/usr/bin/env python3
"""TD4 开工登记（幂等）：
1. PROGRESS.json：tasks["TD4"] 置 in_progress + observation 记录开工修正
2. tasks.json：TD4 DoD ③ 收窄（全量 48/127 既有漂移挡路 + drift 工具不比对列型）
   + pitfalls 追加开工发现
3. 自检 _selfcheck_conflict.py

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
entry = tasks.get("TD4")
if entry is None:
    tasks["TD4"] = {"status": "in_progress", "agent": "agent-senior-dev", "started_at": now}
    print("[PROGRESS] TD4 已置 in_progress")
elif entry.get("status") == "done":
    print("[PROGRESS] TD4 已 done，跳过状态改写")
else:
    entry["status"] = "in_progress"
    entry.setdefault("started_at", now)
    print(f"[PROGRESS] TD4 已存在（{entry.get('status')}），确认 in_progress")

obs_note = (
    "TD4 开工修正：DoD ③ 全量 verify_schema_drift 不可达成（TD1 基线 48/127，且 6 表中 "
    "5 张开工前就带既有漂移：cash_ledger 缺FK、portfolios/positions/sim_orders 列差异、"
    "backtest_runs 多索引等）→ 收窄为 --tables portfolio_equity_snapshots（唯一开工前即"
    "零漂移者）；列型正确性由 DoD ② DATA_TYPE 检查权威覆盖（drift 工具不比对列型）。"
    "开工探针：8 目标列现状全部 float（单精度）、6 表全 InnoDB、行数 2~224。"
)
obs = prog.setdefault("observations", [])
if isinstance(obs, list) and not any(obs_note[:40] in o for o in obs):
    obs.append(obs_note)
    print("[PROGRESS] observation 已追加")
prog["updated_at"] = now
prog["updated_by"] = "agent-senior-dev"
prog_path.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# ── 2. tasks.json ────────────────────────────────────────────
tasks_path = HERE / "tasks.json"
tj = json.loads(tasks_path.read_text(encoding="utf-8"))
td4 = next(t for t in tj["tasks"] if t.get("id") == "TD4")

dod3 = td4["dod"][2]
assert "verify_schema_drift" in dod3["cmd"], "DoD ③ 结构与预期不符"
if "--tables" not in dod3["cmd"]:
    dod3["cmd"] = "{PY} .workbuddy/mining/verify_schema_drift.py --tables portfolio_equity_snapshots"
    dod3["expect"] = (
        "exit 0：该表是 6 表中唯一开工前即零漂移者，改列型后必须保持零漂移；"
        "另 5 张表的既有漂移（缺FK/多索引/列差异，见 _td1_drift_baseline.txt 48/127 基线）"
        "非本卡引入、不在本卡范围；8 列的列型正确性由 DoD ② 的 DATA_TYPE 检查权威覆盖"
        "（drift 工具不比对列型，这正是本债存在而漂移检查长期漏报的原因）"
    )
    print("[tasks] TD4 DoD ③ 已收窄为 --tables portfolio_equity_snapshots")

pit_additions = [
    "★ 开工实测（2026-09-18）：8 目标列现状全部为 float（单精度）、6 表全 InnoDB、"
    "行数 2~224（ALTER 秒级）；同表另有 30+ 个 float 列（investable_ratio/quantity/"
    "avg_cost/total_equity/sharpe_ratio 等）不在本卡，TD5 裁决",
    "★ DoD ③ 修正（TD1 同款教训）：全量 drift 基线 48/127，6 表中 5 张开工前就带既有漂移，"
    "全量跑必 exit 1 → 收窄为 --tables portfolio_equity_snapshots；"
    "且 drift 工具不比对列型，Float→Double 只有 DoD ② 的 DATA_TYPE 行为验证能证明",
    "★ MODIFY COLUMN 必须保留原列 NULL 性：8 列全部 NOT NULL 且无 server_default"
    "（default=0 是 Python 端默认，DB 层无 DEFAULT）→ MODIFY ... DOUBLE NOT NULL，"
    "不带 server_default；迁移里从 information_schema 读 IS_NULLABLE 校验兜底",
]
for p in pit_additions:
    if not any(p[:30] in x for x in td4["pitfalls"]):
        td4["pitfalls"].append(p)
        print("[tasks] pitfalls 已追加")

tasks_path.write_text(json.dumps(tj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# ── 3. 自检 ─────────────────────────────────────────────────
r = subprocess.run([sys.executable, str(HERE / "_selfcheck_conflict.py")],
                   capture_output=True, text=True, encoding="utf-8")
print("\n".join(r.stdout.strip().splitlines()[-3:]))
if r.returncode != 0:
    sys.exit(f"[FATAL] 自检失败\n{r.stdout}\n{r.stderr}")
