#!/usr/bin/env python3
"""TD4 DoD ②：账务 8 列 Float→Double 的行为验证（真实库）。

双验证，任一失败即非零退出：
  A. 列型权威核对 —— information_schema.DATA_TYPE：8 列必须全部为 `double`。
     （drift 工具不比对列型，这是本债长期漏报的根源，必须在此独立验证。）
  B. 精度写入读回 —— 真实库写入 1,200,000,000.01（1.2e9+0.01）级金额：
     单精度 float 在该量级的分辨率为 128（尾数不足），0.01 会被量化丢失；
     double 可精确读回。做两张表的行为验证：
       backtest_runs.initial_capital（无业务锁、无唯一约束）
       cash_ledger.amount / balance_after（无 FK/无唯一约束）
     其余 5 列与上述列同批次同机制改性，DATA_TYPE 证据已足够。

安全与幂等：
- 测试行带 `td4_precision_test` 标记（note），脚本开头先清扫残留（可重跑），finally 再清；
- 用 ORM 表对象插入（Python 端默认值生效）；只增删自己的测试行，不碰业务数据。

用法：.venv/Scripts/python.exe .workbuddy/mining/_verify_accounting_double.py
退出码：0 = 双验证通过；1 = 失败（原因见输出）。
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, inspect, select  # noqa: E402

from app.core.config import build_mysql_url, load_db_config  # noqa: E402
import app.models  # noqa: F401, E402  （触发 ORM 注册）
from app.db.base import Base  # noqa: E402

RUNS = Base.metadata.tables["backtest_runs"]
LEDGER = Base.metadata.tables["cash_ledger"]

TARGETS: dict[str, list[str]] = {
    "cash_ledger": ["amount", "balance_after"],
    "portfolios": ["total_capital"],
    "positions": ["market_value"],
    "portfolio_equity_snapshots": ["market_value", "cash_balance"],
    "sim_orders": ["filled_amount"],
    "backtest_runs": ["initial_capital"],
}

MARK = "td4_precision_test"
AMOUNT = 1_200_000_000.01  # 1.2e9 + 0.01：单精度 float 在此量级分辨率为 128，必量化


def _fail(msg: str) -> int:
    print(f"❌ {msg}")
    return 1


def cleanup(engine, why: str) -> None:
    with engine.begin() as conn:
        c1 = conn.execute(LEDGER.delete().where(LEDGER.c.note == MARK)).rowcount
        c2 = conn.execute(RUNS.delete().where(RUNS.c.run_name == MARK)).rowcount
    if c1 or c2:
        print(f"  [cleanup:{why}] 清理测试行 cash_ledger x{c1} backtest_runs x{c2}")


def main() -> int:
    eng = create_engine(build_mysql_url(load_db_config()), pool_pre_ping=True)
    insp = inspect(eng)
    ok = True

    # ── A. 8 列 DATA_TYPE 必须全部为 double ─────────────────────
    print("── A. 列型核对（information_schema）──")
    bad: list[str] = []
    for t, cols in TARGETS.items():
        meta_cols = {c["name"]: c for c in insp.get_columns(t)}
        for c in cols:
            col = meta_cols.get(c)
            if col is None:
                bad.append(f"{t}.{c}: 列不存在")
                continue
            # SQLAlchemy 对 MySQL 反射 DOUBLE 为 Double/DOUBLE，float 为 FLOAT/Float
            type_name = col["type"].__class__.__name__.upper()
            if type_name != "DOUBLE":
                bad.append(f"{t}.{c}: {type_name}")
    if bad:
        return _fail(f"A. 列型不是 double：{bad} —— 请先执行 alembic upgrade head（0057）。")
    print(f"✅ A. 8 列 DATA_TYPE 全部为 double（6 表核对完成）")

    cleanup(eng, "开工清残留")
    today = _dt.date.today()

    try:
        # ── B. 精度写入读回（真实库）────────────────────────────
        print("── B. 精度写入读回（1.2e9+0.01，单精度必量化）──")
        with eng.begin() as conn:
            conn.execute(RUNS.insert().values(
                portfolio_id=conn.execute(
                    select(RUNS.c.portfolio_id).limit(1)
                ).scalar(),
                run_name=MARK,
                symbols_json="[]",
                rule_config_json="{}",
                start_date=today,
                end_date=today,
                initial_capital=AMOUNT,
            ))
        got = eng.connect().execute(
            select(RUNS.c.initial_capital).where(RUNS.c.run_name == MARK)
        ).scalar()
        if got is None or abs(got - AMOUNT) > 1e-6:
            return _fail(
                f"B. backtest_runs.initial_capital 读回 {got!r}，期望 {AMOUNT!r} "
                f"（差值 {None if got is None else got - AMOUNT}）—— 列仍是被量化的单精度！"
            )
        print(f"✅ B1. initial_capital 写入 {AMOUNT} 读回 {got}（精确一致）")

        with eng.begin() as conn:
            conn.execute(LEDGER.insert().values(
                portfolio_id=conn.execute(
                    select(LEDGER.c.portfolio_id).limit(1)
                ).scalar(),
                entry_type="test",
                amount=AMOUNT,
                balance_after=AMOUNT,
                note=MARK,
            ))
        row = eng.connect().execute(
            select(LEDGER.c.amount, LEDGER.c.balance_after).where(LEDGER.c.note == MARK)
        ).first()
        if row is None or abs(row[0] - AMOUNT) > 1e-6 or abs(row[1] - AMOUNT) > 1e-6:
            return _fail(f"B. cash_ledger 读回 {tuple(row) if row else None}，期望 {AMOUNT} —— 列仍是被量化的单精度！")
        print(f"✅ B2. cash_ledger.amount/balance_after 写入读回精确一致（{row[0]}）")
    finally:
        cleanup(eng, "收尾清扫")
        eng.dispose()

    print("\n✅ TD4 行为验证通过：8 列 double + 精度读回一致（单精度下必失败的断言）。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
