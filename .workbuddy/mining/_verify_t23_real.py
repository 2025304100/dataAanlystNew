"""T23 真实库试探：GA 落库层在真实 MySQL 上的写入安全（临时行 + 清理）。"""
from __future__ import annotations

import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402
from app.services.factors.mining import service as SVC  # noqa: E402

RUN_ID = "_verify_t23_tmp"

eng = create_engine(build_mysql_url(load_db_config()), pool_pre_ping=True)
Session = __import__("sqlalchemy").orm.sessionmaker(bind=eng)
db = Session()

print("=" * 72)
print("① 真实库建 run + generation 行（ORM 路径，与生产一致）")
print("=" * 72)
run = SVC.create_run(
    db, run_id=RUN_ID, candidate_pool_snapshot_id="verify-tmp",
    data_cutoff_at=datetime(2026, 9, 17), start_date=datetime(2026, 1, 1),
    end_date=datetime(2026, 9, 1), rebalance_frequency="weekly",
    max_generation=5, random_seed=42)
print("  run 创建:", run.id, "| total_trials =", run.total_trials)

print()
print("=" * 72)
print("② 落一代统计：**含 NaN / ±Inf / 超界值**（P0 规则必须全部接住）")
print("=" * 72)
SVC.persist_generation(db, run_id=RUN_ID, generation=0, record={
    "population_size": 60,
    "best_icir": 0.85,
    "avg_icir": float("nan"),          # ← 应落 None
    "median_icir": float("inf"),       # ← 应落 None
    "elite_count": 12, "mutation_count": 33, "crossover_count": 10,
    "random_count": 5, "total_trials": 60, "stall_count": 0,
    "eliminated_count": 0,
    "probe_data_load_ms": 120, "probe_g2_hit_rate": 0.87,
    "cache_validation_passed": 1, "cache_validation_max_diff": 3e38,
})
row = SVC.load_generation(db, run_id=RUN_ID, generation=0)
print(f"  best_icir={row.best_icir!r}（有限）")
print(f"  avg_icir={row.avg_icir!r}（NaN → None ✅）")
print(f"  median_icir={row.median_icir!r}（Inf → None ✅）")
print(f"  probe_g2_hit_rate={row.probe_g2_hit_rate!r}")
print(f"  cache_validation_max_diff={row.cache_validation_max_diff!r}（哨兵 3e38）")

print()
print("=" * 72)
print("③ 落候选：fitness 里带 NaN / 超界（应落 None / 哨兵）")
print("=" * 72)
ranked = [
    {"formula": "mean(close,20)", "canonical_formula": "mean(close,20)",
     "formula_hash": "verify-h1", "operation": "elite", "category": "trend",
     "fitness": {"icir": 0.5, "coverage": 0.8, "turnover": 0.2,
                 "complexity": 5}},
    {"formula": "cs_rank(pe_ttm)", "canonical_formula": "cs_rank(pe_ttm)",
     "formula_hash": "verify-h2", "operation": "mutation",
     "category": "valuation",
     "fitness": {"icir": float("nan"), "coverage": 0.8, "turnover": 0.2,
                 "complexity": 3}},
]
n = SVC.persist_candidates(db, run_id=RUN_ID, generation=0, ranked=ranked)
print(f"  插入 {n} 行（NaN → None，不失败）")
for h in ("verify-h1", "verify-h2"):
    got = db.execute(text(
        "SELECT generation_icir, generation_coverage FROM factor_mining_candidates "
        "WHERE run_id = :r AND formula_hash = :h"), {"r": RUN_ID, "h": h}).fetchone()
    print(f"  {h}: icir={got[0]!r} coverage={got[1]!r}")

print()
print("=" * 72)
print("④ total_trials 每代累加（DSR 唯一输入源）")
print("=" * 72)
for g, trials in ((1, 60), (2, 120)):
    SVC.persist_generation(db, run_id=RUN_ID, generation=g,
                           record={"population_size": 60})
    SVC.sync_run_progress(db, run_id=RUN_ID, generation=g, total_trials=trials,
                          converged=(g == 2))
run = SVC.load_run(db, RUN_ID)
print(f"  current_generation={run.current_generation} "
      f"total_trials={run.total_trials} converged={run.converged}")
assert run.total_trials == 120 and run.current_generation == 2

print()
print("=" * 72)
print("⑤ 全量校验：探针/统计 float 列全部有限或 NULL")
print("=" * 72)
import math  # noqa: E402

bad = []
for g in range(3):
    row = SVC.load_generation(db, run_id=RUN_ID, generation=g)
    if row is None:
        continue
    for col in ("best_icir", "avg_icir", "median_icir", "probe_g2_hit_rate",
                "cache_validation_max_diff"):
        v = getattr(row, col)
        if v is not None and not math.isfinite(float(v)):
            bad.append((g, col, v))
print("  违规:", bad or "无 ✅")

print()
print("=" * 72)
print("⑥ 清理（FK CASCADE：删 run 带走 generation/candidates）")
print("=" * 72)
db.execute(text("DELETE FROM factor_mining_candidates WHERE run_id = :r"),
           {"r": RUN_ID})
db.execute(text("DELETE FROM factor_mining_generations WHERE run_id = :r"),
           {"r": RUN_ID})
db.execute(text("DELETE FROM factor_mining_runs WHERE id = :r"), {"r": RUN_ID})
db.commit()
left = db.execute(text(
    "SELECT COUNT(*) FROM factor_mining_runs WHERE id = :r"), {"r": RUN_ID}).scalar()
print("  残留 run 行 =", left, "（应为 0）")
db.close()
eng.dispose()
