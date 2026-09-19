#!/usr/bin/env python3
"""TD1 开工探针：真实库中三张含 model_run_id FK 声明的表的现状。（只读）

用 SQLAlchemy inspector 取 FK（与 verify_schema_drift.py 同一路径、同口径）。
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "alembic"))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402


def main() -> int:
    cfg = load_db_config()
    url = build_mysql_url(cfg)
    eng = create_engine(url, pool_pre_ping=True)
    insp = inspect(eng)
    with eng.connect() as conn:
        has_ver = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name='alembic_version'"
        )).scalar()
        if has_ver:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            print(f"alembic current : {rev}")
        else:
            print("alembic current : (无 alembic_version 表)")

        tables = ["factor_runtime_state", "factor_model_audit_logs", "factor_audit_logs"]
        for t in tables:
            print(f"\n===== {t} =====")
            row = conn.execute(text(
                "SELECT engine, table_collation FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=:t"
            ), {"t": t}).first()
            if row is None:
                print("  [!] 表不存在")
                continue
            print(f"  engine={row[0]}  collation={row[1]}")

            for fk in insp.get_foreign_keys(t):
                opts = fk.get("options") or {}
                print(f"  FK {fk.get('name')}: {fk.get('constrained_columns')} -> "
                      f"{fk.get('referred_table')}.{fk.get('referred_columns')} "
                      f"ondelete={opts.get('ondelete')}")

            cols = conn.execute(text(
                "SELECT column_name, column_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name=:t "
                "  AND column_name IN ('model_run_id','active_model_run_id','id') "
                "ORDER BY column_name"
            ), {"t": t}).all()
            for c in cols:
                print(f"  col {c[0]}: {c[1]}  nullable={c[2]}")

            cnt = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            print(f"  rows: {cnt}")

        print("\n===== factor_model_runs =====")
        row = conn.execute(text(
            "SELECT engine, table_collation FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name='factor_model_runs'"
        )).first()
        print(f"  engine={row[0] if row else '(表不存在)'}  collation={row[1] if row else '-'}")
        cnt = conn.execute(text("SELECT COUNT(*) FROM factor_model_runs")).scalar()
        print(f"  rows: {cnt}")
        sample = conn.execute(text(
            "SELECT id, CHAR_LENGTH(id) FROM factor_model_runs LIMIT 3"
        )).all()
        for s in sample:
            print(f"  id 样本: len={s[1]}  {s[0][:24]}...")

        # 被引用计数：三张子表里 model_run_id 当前值是否都能在父表找到（孤儿检查）
        print("\n===== 孤儿检查（FK 补建前提：无孤儿行）=====")
        for t, col in [
            ("factor_runtime_state", "active_model_run_id"),
            ("factor_model_audit_logs", "model_run_id"),
            ("factor_audit_logs", "model_run_id"),
        ]:
            orphan = conn.execute(text(
                f"SELECT COUNT(*) FROM `{t}` x LEFT JOIN factor_model_runs p "
                f"ON x.`{col}` = p.id "
                f"WHERE x.`{col}` IS NOT NULL AND p.id IS NULL"
            )).scalar()
            print(f"  {t}.{col}: 孤儿 {orphan}")
    eng.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
