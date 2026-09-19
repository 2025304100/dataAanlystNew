# -*- coding: utf-8 -*-
"""T38 侦查：只读查询真实库 factor_mining_generations 的探针数据现状。

只读、不写、不改结构；用于判断 G1 决策是否有数据支撑（not_do：
「不在无探针数据支撑时盲目实现并行」）。
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402

cfg = load_db_config()
url = build_mysql_url(cfg)
print("db host:", cfg.get("host"), "| db:", cfg.get("database") or cfg.get("name"))
engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)

PROBE_COLS = [
    "probe_data_load_ms", "probe_ast_eval_ms", "probe_subexpr_compute_ms",
    "probe_factor_assemble_ms", "probe_metric_calc_ms", "probe_db_write_ms",
    "probe_subexpr_total", "probe_subexpr_unique", "probe_g2_hit_rate",
    "evaluation_duration_ms", "population_size", "generation", "run_id",
]

with engine.connect() as conn:
    # 1) 表是否存在
    row = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = 'factor_mining_generations'"
    )).scalar()
    print("generations 表存在:", bool(row))

    if not row:
        print("NO_TABLE —— 无探针数据")
        sys.exit(0)

    # 2) 行数与 run 数
    total = conn.execute(text("SELECT COUNT(*) FROM factor_mining_generations")).scalar()
    runs = conn.execute(
        text("SELECT COUNT(DISTINCT run_id) FROM factor_mining_generations")).scalar()
    print(f"generations 行数: {total} | 去重 run_id: {runs}")

    if total == 0:
        print("NO_DATA —— 表存在但无数据（挖掘从未真实跑完一代）")
        sys.exit(0)

    # 3) 各探针列聚合
    print("\n== 探针列聚合（AVG / MAX / 非空行数）==")
    for col in PROBE_COLS:
        try:
            r = conn.execute(text(
                f"SELECT AVG(`{col}`), MAX(`{col}`), COUNT(`{col}`) "
                f"FROM factor_mining_generations")).fetchone()
            avg, mx, cnt = r[0], r[1], r[2]
            favg = float(avg) if avg is not None else 0.0
            fmax = float(mx) if mx is not None else 0.0
            print(f"  {col:<28} avg={favg:>12.2f}  max={fmax:>12.2f}  n={cnt}")
        except Exception as exc:
            print(f"  {col:<28} ERROR: {str(exc)[:80]}")

    # 4) 最近 10 代明细（按 run/generation）
    print("\n== 最近 10 行明细 ==")
    rows = conn.execute(text(
        "SELECT run_id, generation, population_size, evaluation_duration_ms, "
        "probe_data_load_ms, probe_ast_eval_ms, probe_subexpr_compute_ms, "
        "probe_factor_assemble_ms, probe_metric_calc_ms, probe_db_write_ms, "
        "probe_subexpr_total, probe_subexpr_unique, probe_g2_hit_rate "
        "FROM factor_mining_generations ORDER BY id DESC LIMIT 10")).fetchall()
    for r in rows:
        print("  ", dict(r._mapping))
