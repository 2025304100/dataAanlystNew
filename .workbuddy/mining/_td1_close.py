#!/usr/bin/env python3
"""TD1 收工登记（幂等）：
1. PROGRESS.json：tasks["TD1"] → done（finished_at/artifacts/evidence）+ observation
2. 自检 _selfcheck_conflict.py
重跑安全：TD1 已 done 时只刷新 evidence 字段不重复追加。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")

prog_path = HERE / "PROGRESS.json"
prog = json.loads(prog_path.read_text(encoding="utf-8"))
tasks = prog.setdefault("tasks", {})
td1 = tasks.setdefault("TD1", {})

td1.update({
    "status": "done",
    "agent": "agent-senior-dev",
    "finished_at": now,
    "artifacts": [
        "alembic/versions/2026_09_18_0056_wps_0023_054_factor_audit_model_run_fk.py"
        "（回填 FK；幂等按列名探测 + 孤儿前置体检 + ibfk 命名 + sqlite 跳过，范式照 0054）",
        "alembic/versions/2026_09_02_0050_wps_0023_048_factor_audit_logs.py"
        "（原地修订：create_table 补 mysql_engine=InnoDB，堵全新安装 MyISAM 吞 FK 路径；"
        "T01 修 0053 同款先例）",
        ".workbuddy/mining/_verify_audit_model_run_fk.py（行为验证：孤儿 1452 + SET NULL，"
        "ORM 表对象插入、前后双清扫可重跑）",
        ".workbuddy/mining/_probe_td1_fk_state.py（只读结构探针）",
        ".workbuddy/mining/_td1_open.py（开工登记：DoD ② 收窄 + pitfalls 补充）",
        ".workbuddy/mining/_td1_drift_baseline.txt（全量 drift 基线 48/127，TD5 的现成输入）",
    ],
    "evidence": [
        "DoD① {PY} .workbuddy/mining/_verify_audit_model_run_fk.py → exit 0："
        "FK factor_audit_logs_ibfk_1（-> factor_model_runs.id, SET NULL）已存在；"
        "孤儿插入被拒（MySQL 1452）；删父行后子行 model_run_id 置 NULL；重跑幂等通过",
        "DoD② verify_schema_drift.py --tables factor_audit_logs → exit 0："
        "索引6 列10 外键1，与 ORM 零漂移（含外键与 ondelete=SET NULL）",
        "alembic 链：单头 wps_0023_054（head），upgrade 0053_pool_id → 0054_model_run_fk 成功，"
        "real 库 current=0056",
        "开工探针：factor_runtime_state / factor_model_audit_logs 的同指向 FK 已存在（InnoDB），"
        "真实库仅 factor_audit_logs 缺；三表孤儿检查全 0，补 FK 无需数据清洗",
        "新发现（已入 pitfalls/observation）：server 默认引擎仍为 MyISAM（5.7.26），"
        "全新安装会再吞 0050 声明的 FK → 已原地修 0050（mysql_engine=InnoDB）",
        "DoD② 修正：全量 drift 基线 48/127 张表既有漂移（TD4/TD5 标的）挡路，"
        "收窄为 --tables factor_audit_logs，全量基线存档 _td1_drift_baseline.txt",
    ],
})

obs = prog.setdefault("observations", [])
obs_note = (
    "TD1 完成：factor_audit_logs.model_run_id 外键已回填并行为验证（1452 + SET NULL）。"
    "注意：全新安装路径靠 0050 原地补 mysql_engine=InnoDB 兜住；真实库其余 47 张漂移表"
    "（全量基线 48/127）仍挂 TD4/TD5。downgrade 路径未在真实库演练（项目规则：只在 scratch 库验 downgrade）。"
)
if isinstance(obs, list) and not any(obs_note[:40] in o for o in obs):
    obs.append(obs_note)
    print("[PROGRESS] observation 已追加")

prog["updated_at"] = now
prog["updated_by"] = "agent-senior-dev"
prog_path.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"[PROGRESS] TD1 → done（{now}）")

r = subprocess.run(
    [sys.executable, str(HERE / "_selfcheck_conflict.py")],
    capture_output=True, text=True, encoding="utf-8",
)
print("\n".join(r.stdout.strip().splitlines()[-3:]))
if r.returncode != 0:
    sys.exit(f"[FATAL] 自检失败\n{r.stdout}\n{r.stderr}")
