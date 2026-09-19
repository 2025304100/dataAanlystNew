"""从 ORM metadata 反推外键定义，供迁移 0054 的冻结表使用。

与 0053 的 `_ORM_INDEXES` 同一范式：**不手工转录**，由 metadata 生成后冻结进迁移
（迁移必须是固定的历史描述，不能跟随 ORM 漂移）。
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.models  # noqa: F401,E402  触发模型注册
from app.db.base import Base  # noqa: E402

TABLES = (
    "training_candidate_pools",
    "training_candidate_pool_members",
    "training_candidate_pool_snapshots",
    "factor_mining_runs",
    "factor_mining_candidates",
    "factor_mining_generations",
    "factor_mining_prescreen_fingerprints",
    "factor_training_checkpoints",
    "factor_mining_drafts",
    "factor_data_validation_runs",
    "factor_mining_templates",
    "factor_mining_template_versions",
    "task_locks",
    "factor_formula_templates",
)

rows: list[tuple[str, tuple[str, ...], str, tuple[str, ...], str]] = []
for t in TABLES:
    tb = Base.metadata.tables.get(t)
    if tb is None:
        print(f"  !! metadata 缺表 {t}")
        continue
    for con in tb.constraints:
        if con.__class__.__name__ != "ForeignKeyConstraint":
            continue
        elements = list(con.elements)
        local = tuple(e.parent.name for e in elements)
        remote_table = sorted({e.column.table.name for e in elements})[0]
        remote = tuple(e.column.name for e in elements)
        ondelete = elements[0].ondelete or ""
        rows.append((t, local, remote_table, remote, str(ondelete)))

print(f"共 {len(rows)} 个外键：")
print()
print("#: 外键定义，由 ORM metadata 反推生成（2026-09-16）。**勿手改**；")
print("#: 结构：表名 -> [(本地列, 被引用表, 被引用列, ondelete), ...]")
print("_ORM_FOREIGN_KEYS: dict[str, list[tuple[str, str, str, str]]] = {")
for t, local, rt, remote, od in rows:
    cols = ", ".join(f'"{c}"' for c in local)
    loc = f"({cols},)" if len(local) == 1 else f"({cols})"
    print(f'    "{t}": [({loc}, "{rt}", "{remote[0]}", "{od}")],')
print("}")
print()
for t, local, rt, remote, od in rows:
    print(f"  {t:38s} {local} -> {rt}.{remote}  ondelete={od}")
