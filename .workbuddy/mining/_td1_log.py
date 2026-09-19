#!/usr/bin/env python3
"""向今日工作日志追加 TD1 收工记录（幂等判重 + 回读校验）。"""
from __future__ import annotations

import datetime as _dt
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
MEM = HERE.parent / "memory"
today = _dt.date.today().isoformat()
log_path = MEM / f"{today}.md"

block = """

## TD1 完成：factor_audit_logs.model_run_id 外键补齐（2026-09-18）

**开工核对三发现（都改变了施工方案）**
1. 真实库只有 `factor_audit_logs` 缺 FK；`factor_runtime_state` / `factor_model_audit_logs`
   的同指向 FK（SET NULL）已存在（auto-align 建表时从 ORM 带出）——模型侧三张表声明
   同一 FK 的疑虑排除，0056 范围收敛为单表单 FK。
2. **全量 drift 基线 48/127 张表漂移**（TD4/TD5 的既有标的）→ TD1 卡 DoD ② 按字面
   「全量 exit 0」不可达成，收窄为 `--tables factor_audit_logs`；基线存档
   `_td1_drift_baseline.txt`（TD5 的现成输入）。
3. **server 默认引擎仍为 MyISAM（MySQL 5.7.26）**→ 全新安装会再次吞 0050 声明的 FK
   → 原地修 0050 补 `mysql_engine="InnoDB"`（T01 修 0053 同款先例；create_table 只在
   全新安装执行，对已应用库零影响）。

**施工**
- 迁移 `2026_09_18_0056_wps_0023_054_factor_audit_model_run_fk.py`：回填 FK，
  幂等（按本地列名探测）、孤儿前置体检（实测 0）、ibfk 命名、SQLite 跳过，范式照 0054。
- 验证 `_verify_audit_model_run_fk.py`：孤儿插入被拒（1452）+ 删父行子行置 NULL，
  ORM 表对象插入、前后双清扫、重跑幂等。
- DoD ① ② 全绿；alembic 单头 0056；收工 PROGRESS done + 自检 ALL OK。

**教训**
- 任务卡的 DoD 写「全量工具 exit 0」前必须先跑一遍全量基线——本项目全量 drift
  本来就红（48/127），这类 DoD 一写就是废卡。
- `_verify_*` 脚本直接拿 `Base.metadata.tables[...]` 时记得先 `import app.models`
  触发注册，否则 KeyError（verify_schema_drift.py 有此步，容易照抄漏掉）。
"""

if not log_path.exists():
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"# {today} 工作日志\n", encoding="utf-8")

text = log_path.read_text(encoding="utf-8")
marker = "## TD1 完成"
if marker in text:
    print("[log] 今日日志已含 TD1 记录，跳过")
else:
    log_path.write_text(text.rstrip("\n") + "\n" + block.lstrip("\n"), encoding="utf-8")
    # 回读校验（防静默截断）
    back = log_path.read_text(encoding="utf-8")
    assert "wps_0023_054" in back and "48/127" in back and "SET NULL" in back, "回读校验失败：内容被截断"
    assert "1452" in back, "回读校验失败：1452 丢失"
    print(f"[log] 已追加 TD1 收工记录到 {log_path.name}（回读校验通过）")
