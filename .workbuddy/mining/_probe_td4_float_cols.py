#!/usr/bin/env python3
"""TD4 开工探针（只读）：
1. 6 张账务表 ENGINE / 行数
2. 8 个目标列的 DATA_TYPE 现状（期望现状=float，目标=double）
3. 同表其余 float 列清单（确认只动 8 列，其余留给 TD5 裁决）
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "alembic"))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402

TARGETS: dict[str, list[str]] = {
    "cash_ledger": ["amount", "balance_after"],
    "portfolios": ["total_capital"],
    "positions": ["market_value"],
    "portfolio_equity_snapshots": ["market_value", "cash_balance"],
    "sim_orders": ["filled_amount"],
    "backtest_runs": ["initial_capital"],
}


def main() -> int:
    eng = create_engine(build_mysql_url(load_db_config()), pool_pre_ping=True)
    with eng.connect() as conn:
        for t, cols in TARGETS.items():
            row = conn.execute(text(
                "SELECT engine, table_collation FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=:t"
            ), {"t": t}).first()
            if row is None:
                print(f"===== {t} =====\n  [!] 表不存在")
                continue
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            print(f"===== {t} =====  engine={row[0]}  collation={row[1]}  rows={cnt}")

            all_cols = conn.execute(text(
                "SELECT column_name, column_type FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name=:t AND data_type='float' "
                "ORDER BY ordinal_position"
            ), {"t": t}).all()
            float_names = {x[0] for x in all_cols}
            for c in all_cols:
                tag = "  <-- 目标列" if c[0] in cols else ""
                print(f"  float 列 {c[0]}: {c[1]}{tag}")

            # 目标列若不是 float，报出实际类型
            for c in cols:
                if c not in float_names:
                    r = conn.execute(text(
                        "SELECT column_type FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() AND table_name=:t "
                        "  AND column_name=:c"
                    ), {"t": t, "c": c}).scalar()
                    print(f"  [!] 目标列 {c} 不是 float 型，实际: {r}")
    eng.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
