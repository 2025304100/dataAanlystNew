# -*- coding: utf-8 -*-
"""T26 迁移 0058 双向验证（scratch sqlite，真实库不动）。

upgrade head → 4 表 + 索引齐全 → downgrade -1 → 4 表干净消失。
"""
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent.parent

fd, path = tempfile.mkstemp(suffix=".db", prefix="t26_mig_")
os.close(fd)
url = f"sqlite:///{path}"
env = {**os.environ, "ALEMBIC_DATABASE_URL": url}

PY = str(ROOT / ".venv/Scripts/python.exe")


def run(*args):
    proc = subprocess.run(
        [PY, "-m", "alembic", *args], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


failures = []

code, out = run("upgrade", "head")
print(f"[upgrade head] exit={code}\n{out.strip()[-400:]}")
if code != 0:
    failures.append("upgrade head 失败")

import sqlalchemy as sa

engine = sa.create_engine(url)
insp = sa.inspect(engine)
tables = set(insp.get_table_names())
expect_tables = {
    "factor_experience", "factor_experience_tags",
    "factor_experience_metrics", "factor_experience_field_deps",
}
missing = expect_tables - tables
print(f"[tables after upgrade] missing={sorted(missing) or '无'}")
if missing:
    failures.append(f"upgrade 后缺表: {sorted(missing)}")

expect_idx = {
    "factor_experience": {"ix_factor_experience_fingerprint",
                          "ix_factor_experience_category_status",
                          "ix_factor_experience_source_success_rate"},
    "factor_experience_tags": {"ix_factor_experience_tags_experience_id",
                               "ix_factor_experience_tags_exp_key"},
    "factor_experience_metrics": {"ix_factor_experience_metrics_experience_id"},
    "factor_experience_field_deps": {
        "ix_factor_experience_field_deps_experience_id"},
}
for table, idxs in expect_idx.items():
    have = {i["name"] for i in insp.get_indexes(table)}
    miss = idxs - have
    print(f"[idx {table}] missing={sorted(miss) or '无'}")
    if miss:
        failures.append(f"{table} 缺索引: {sorted(miss)}")

# 主表列数对齐 ORM（19 列）
cols = {c["name"] for c in insp.get_columns("factor_experience")}
expect_cols = {
    "id", "formula_template", "formula_ast", "category", "complexity_json",
    "source", "fingerprint", "param_placeholders_json", "use_count",
    "success_count", "success_rate", "avg_icir", "status",
    "is_negative_sample", "origin_project_id", "schema_version",
    "created_at", "updated_at", "last_used_at",
}
if cols != expect_cols:
    failures.append(
        f"主表列集合不齐 ORM: 多={sorted(cols-expect_cols)} 缺={sorted(expect_cols-cols)}")
else:
    print(f"[columns factor_experience] 19 列与 ORM 对齐 OK")

engine.dispose()

code, out = run("downgrade", "-1")
print(f"[downgrade -1] exit={code}\n{out.strip()[-300:]}")
if code != 0:
    failures.append("downgrade -1 失败")

engine = sa.create_engine(url)
left = expect_tables & set(sa.inspect(engine).get_table_names())
engine.dispose()
print(f"[tables after downgrade] 残留={sorted(left) or '无'}")
if left:
    failures.append(f"downgrade 后残留表: {sorted(left)}")

try:
    os.remove(path)
except OSError:
    pass

print("=" * 60)
if failures:
    print("MIGRATION VERIFY FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("MIGRATION VERIFY OK —— upgrade/downgrade 双向干净")
