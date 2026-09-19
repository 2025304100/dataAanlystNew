"""Widen 8 accounting columns from FLOAT to DOUBLE (TD4).

Revision ID: wps_0023_055_accounting_float_to_double
Revises: wps_0023_054_factor_audit_model_run_fk

为什么需要（TD4，2026-09-18 开工）
====================================
SQLAlchemy 的 `Float`（不带 precision）在 MySQL 编译为**单精度 FLOAT**（4 字节，
约 7 位有效数字）。账务金额（如 `total_capital=1,200,000,000.01`）写入时被量化，
**分位以下精度静默丢失**（R33~R35 系列结论：MySQL 侧范围/精度问题，SQLite REAL=
8 字节 DOUBLE 天然放行，单测全绿 ≠ 写入安全）。

8 列 6 表（需求方裁决 D-J/TD4）：
  cash_ledger.amount / balance_after
  portfolios.total_capital
  positions.market_value
  portfolio_equity_snapshots.market_value / cash_balance
  sim_orders.filled_amount
  backtest_runs.initial_capital

float → double 是**无损拓宽**（读端无需任何迁移；业务逻辑/计算口径零改动）。
同表其余 30+ 个 float 列（investable_ratio / quantity / sharpe_ratio 等）**不在本卡**，
待 TD5 全库漂移报告统一裁决。

实现要点
========
- 幂等：逐列探 information_schema.DATA_TYPE，已是 double 即跳过；
- **MODIFY 保留原列属性**：从 information_schema 读 IS_NULLABLE / COLUMN_COMMENT /
  COLUMN_DEFAULT 显式回写（MySQL MODIFY 不写 NULL 性会重置为 NULL）。8 列实测
  全部 NOT NULL 且无 server_default（`default=0` 是 Python 端默认，DB 层无 DEFAULT）；
- float→double 原地拓宽，6 表行数 2~224，秒级完成，无需锁表预案；
- SQLite 分支跳过：REAL 已是 8 字节 DOUBLE（R34），无需任何改动。

行为验证（information_schema 有定义 ≠ 精度正确）
================================================
配套 `.workbuddy/mining/_verify_accounting_double.py`：
8 列 DATA_TYPE=double + 1.2e9+0.01 级写入读回一致（真实库，测试行前后双清扫）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "wps_0023_055_accounting_float_to_double"
down_revision = "wps_0023_054_factor_audit_model_run_fk"
branch_labels = None
depends_on = None

#: 表名 -> [账务列]（勿手改；与 ORM 声明一致，tests/test_whitebox_numeric_columns.py 会比对）
_TARGETS: dict[str, list[str]] = {
    "cash_ledger": ["amount", "balance_after"],
    "portfolios": ["total_capital"],
    "positions": ["market_value"],
    "portfolio_equity_snapshots": ["market_value", "cash_balance"],
    "sim_orders": ["filled_amount"],
    "backtest_runs": ["initial_capital"],
}


def _col_meta(bind, table: str, col: str) -> dict | None:
    row = bind.execute(sa.text(
        "SELECT data_type, is_nullable, column_comment, column_default, column_type "
        "FROM information_schema.columns "
        "WHERE table_schema=DATABASE() AND table_name=:t AND column_name=:c"
    ), {"t": table, "c": col}).first()
    if row is None:
        return None
    return {
        "data_type": row[0], "is_nullable": row[1],
        "comment": row[2] or "", "default": row[3], "full_type": row[4],
    }


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        # SQLite 的 REAL 本就是 8 字节 DOUBLE（R34）：单测对精度天然放行，
        # 也正因如此无需任何列型改动。
        print("[0057] dialect=sqlite：REAL 已是 DOUBLE 精度 → 跳过（预期行为）。")
        return

    widened = 0
    for table, cols in _TARGETS.items():
        if not sa.inspect(bind).has_table(table):
            print(f"[0057] 跳过：表 {table} 不存在")
            continue
        for col in cols:
            meta = _col_meta(bind, table, col)
            if meta is None:
                print(f"[0057] 跳过：{table}.{col} 列不存在")
                continue
            if meta["data_type"] == "double":
                print(f"[0057] 已是 double，跳过：{table}.{col}（重跑的正常结果）")
                continue
            if meta["data_type"] != "float":
                # 意外列型（如 decimal）：中止交人工裁决，不静默覆盖
                raise RuntimeError(
                    f"[0057] 中止：{table}.{col} 列型为 {meta['full_type']}，"
                    f"既非 float 也非 double —— 需人工裁决，本迁移不覆盖。"
                )

            null_kw = "NULL" if meta["is_nullable"] == "YES" else "NOT NULL"
            dflt = f" DEFAULT '{meta['default']}'" if meta["default"] is not None else ""
            cmt = f" COMMENT '{meta['comment']}'" if meta["comment"] else ""
            bind.execute(sa.text(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{col}` DOUBLE {null_kw}{dflt}{cmt}"
            ))
            widened += 1
            print(f"[0057] 已拓宽：{table}.{col}  {meta['full_type']} -> DOUBLE {null_kw}")

    print(f"[0057] 本次共拓宽 {widened} 列。")


def downgrade() -> None:
    """回滚 = 收窄回 FLOAT。⚠️ 有损：已写入的 1.2e9+0.01 级值会被量化，不可逆。

    默认拒绝在含高风险数据的库上执行；确认可接受后手动传
    `ALEMBIC_ALLOW_LOSSY_DOWNGRADE=1`。"""
    import os

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        print("[0057] dialect=sqlite：无改动 → 跳过。")
        return
    if not os.getenv("ALEMBIC_ALLOW_LOSSY_DOWNGRADE"):
        print(
            "[0057] downgrade 为有损操作（DOUBLE→FLOAT 量化丢精度），已拒绝执行。"
            "如确需回滚，请先核对 8 列现有数据无分位以下精度，"
            "再设置 ALEMBIC_ALLOW_LOSSY_DOWNGRADE=1 重跑。"
        )
        return

    narrowed = 0
    for table, cols in _TARGETS.items():
        for col in cols:
            meta = _col_meta(bind, table, col)
            if meta is None or meta["data_type"] != "double":
                continue
            null_kw = "NULL" if meta["is_nullable"] == "YES" else "NOT NULL"
            cmt = f" COMMENT '{meta['comment']}'" if meta["comment"] else ""
            bind.execute(sa.text(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{col}` FLOAT {null_kw}{cmt}"
            ))
            narrowed += 1
            print(f"[0057] 已回滚：{table}.{col}  DOUBLE -> FLOAT {null_kw}")
    print(f"[0057] downgrade 完成，回滚 {narrowed} 列。")
