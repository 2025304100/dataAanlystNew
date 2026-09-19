#!/usr/bin/env python3
"""TD4 收工登记（幂等）：
1. PROGRESS.json：tasks["TD4"] → done（finished_at/artifacts/evidence）+ observation
2. 自检 _selfcheck_conflict.py
重跑安全：TD4 已 done 时只刷新 evidence/artifacts，不重复追加 observation。
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
td4 = prog.setdefault("tasks", {}).setdefault("TD4", {})

td4.update({
    "status": "done",
    "agent": "agent-senior-dev",
    "finished_at": now,
    "artifacts": [
        "alembic/versions/2026_09_18_0057_wps_0023_055_accounting_float_to_double.py"
        "（幂等按 DATA_TYPE 探测；MODIFY 保留 NULL 性/注释/默认值；意外列型中止；"
        "downgrade 有损守卫 ALEMBIC_ALLOW_LOSSY_DOWNGRADE）",
        "app/models/sim_account.py、portfolio.py、portfolio_equity_snapshot.py、backtest.py"
        "（8 列 Float→Double 声明 + 注记；Float import 保留——其余列仍在用）",
        "tests/test_whitebox_numeric_columns.py"
        "（10 用例：8 列 Double+NOT NULL 参数化、迁移 _TARGETS 一致性、防走样守卫）",
        ".workbuddy/mining/_verify_accounting_double.py"
        "（DoD② 行为验证：8 列 DATA_TYPE + 两表 1.2e9+0.01 写入读回，ORM 插入前后双清扫）",
        ".workbuddy/mining/_td4_open.py（开工登记：DoD③ 收窄 + pitfalls 补充）",
        ".workbuddy/mining/_td4_drift_baseline.txt（6 表开工 drift 对照：5/6 既有漂移，全为缺FK/多索引/列差异）",
    ],
    "evidence": [
        f"DoD① pytest tests/test_whitebox_numeric_columns.py -q → 10 passed（exit 0）",
        "DoD② {PY} .workbuddy/mining/_verify_accounting_double.py → exit 0："
        "8 列 DATA_TYPE=double；backtest_runs.initial_capital 与 cash_ledger.amount/balance_after "
        "写入 1,200,000,000.01 读回精确一致（单精度分辨率 128 下必失败）；重跑幂等通过",
        "DoD③ verify_schema_drift.py --tables portfolio_equity_snapshots → exit 0（索引3 列11 外键1）",
        "alembic 单头 wps_0023_055（head）；upgrade 054→055 成功：8 列 float→DOUBLE NOT NULL；"
        "真实库 current=0057",
        "跨域回归（迁移链24项+组合/快照/回测/自动交易 5 文件 126 项）：123 passed + 2 failed；"
        "2 个失败（backtest_apply 的 portfolio_not_found / missing_symbol）经 Float 基线对照实证为"
        "**既有失败**（基线版同红，症状 SQLite FK 强制 vs 测试故意插不存在父行），与列型改动无关；"
        "TD4 相关断言（迁移链/列型/精度/防走样）全部绿",
        "开工探针：8 目标列原为 float（单精度）、6 表全 InnoDB、行数 2~224；"
        "同表另有 30+ float 列（investable_ratio/quantity/sharpe_ratio 等）留给 TD5",
    ],
})

obs = prog.setdefault("observations", [])
obs_note = (
    "TD4 完成：账务 8 列（6 表）float→double 拓宽完成并行为验证（1.2e9+0.01 精确读回）。"
    "drift 工具不比对列型——列型回归只能靠 _verify_accounting_double.py 这类 DATA_TYPE 检查，"
    "TD5 报告需注明该盲区。其余 30+ float 列与 5 张表的既有漂移（缺FK/多索引/列差异）仍挂 TD5。"
    "downgrade 有损守卫：默认拒绝（DOUBLE→FLOAT 量化丢精度），需显式 ALEMBIC_ALLOW_LOSSY_DOWNGRADE=1。"
    "【既有失败挂账】test_whitebox_backtest_apply.py 2 用例（portfolio_not_found_raises / "
    "missing_symbol_skipped）在 Float 基线对照下同红——测试故意插不存在父行（portfolio_id=1 / "
    "symbol_id=99999）但 SQLite PRAGMA foreign_keys=ON 使 ORM flush 即撞 FK，测不到业务错误分支；"
    "非 TD4 引入，修复方向属测试基线/fixture 策略（与 TD2 integration 污染同类），上报裁决。"
)
if isinstance(obs, list) and not any(obs_note[:40] in o for o in obs):
    obs.append(obs_note)
    print("[PROGRESS] observation 已追加")

prog["updated_at"] = now
prog["updated_by"] = "agent-senior-dev"
prog_path.write_text(json.dumps(prog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"[PROGRESS] TD4 → done（{now}）")

r = subprocess.run([sys.executable, str(HERE / "_selfcheck_conflict.py")],
                   capture_output=True, text=True, encoding="utf-8")
print("\n".join(r.stdout.strip().splitlines()[-3:]))
if r.returncode != 0:
    sys.exit(f"[FATAL] 自检失败\n{r.stdout}\n{r.stderr}")
