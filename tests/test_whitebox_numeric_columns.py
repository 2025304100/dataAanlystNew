"""TD4 白盒测试：账务 8 列的 ORM 声明必须是 Double 且与迁移 0057 的列清单一致。

背景（R33~R35）：SQLAlchemy `Float`（无 precision）在 MySQL 编译为单精度 FLOAT，
1.2e9+0.01 级账务金额写入时尾数被量化丢失；SQLite REAL=8 字节 DOUBLE 天然放行，
**单测对精度不设防** —— 因此本测试不测精度（那是
`.workbuddy/mining/_verify_accounting_double.py` 在真实库的行为验证），
只锁定两处**声明一致性**：

1. ORM 侧 8 列的类型类是 `sqlalchemy.Double`（编译为 MySQL DOUBLE）、nullable=False；
2. 迁移 0057 的 `_TARGETS` 与 ORM 期望清单逐表逐列一致（防后续有人改了一边漏另一边）。

纯元数据断言：不连数据库、不触数仓。
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import Double

from app.db.base import Base
from app.models.backtest import BacktestRun  # noqa: F401  （注册表定义）
from app.models.portfolio import Portfolio, Position  # noqa: F401
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot  # noqa: F401
from app.models.sim_account import CashLedger, SimOrder  # noqa: F401

REPO = pathlib.Path(__file__).resolve().parents[1]
MIGRATION_0057 = (
    REPO / "alembic" / "versions"
    / "2026_09_18_0057_wps_0023_055_accounting_float_to_double.py"
)

#: 账务 8 列期望清单（表 -> [列]，与迁移 _TARGETS 逐字同源）
EXPECTED_TARGETS: dict[str, list[str]] = {
    "cash_ledger": ["amount", "balance_after"],
    "portfolios": ["total_capital"],
    "positions": ["market_value"],
    "portfolio_equity_snapshots": ["market_value", "cash_balance"],
    "sim_orders": ["filled_amount"],
    "backtest_runs": ["initial_capital"],
}

_EXPECTED_CASES = [(t, c) for t, cols in sorted(EXPECTED_TARGETS.items()) for c in cols]


def _load_migration_targets() -> dict[str, list[str]]:
    """从迁移文件加载 _TARGETS（文件名非合法标识符，用 importlib 按路径加载）。"""
    assert MIGRATION_0057.exists(), f"迁移文件缺失：{MIGRATION_0057}"
    spec = importlib.util.spec_from_file_location("_td4_migration_0057", MIGRATION_0057)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._TARGETS


@pytest.mark.parametrize(("table", "col"), _EXPECTED_CASES)
def test_orm_accounting_column_is_double(table: str, col: str) -> None:
    """8 列 ORM 声明必须是 Double（MySQL DOUBLE）且 NOT NULL。"""
    tbl = Base.metadata.tables.get(table)
    assert tbl is not None, f"ORM 未注册表 {table}"
    assert col in tbl.columns, f"{table} 缺列 {col}"
    column = tbl.columns[col]
    assert isinstance(column.type, Double), (
        f"{table}.{col} 列型是 {column.type.__class__.__name__}，应为 Double"
        f"（Float 无 precision 在 MySQL=单精度，账务金额会量化丢精度）"
    )
    assert column.nullable is False, f"{table}.{col} 应保持 NOT NULL（MODIFY 勿重置 NULL 性）"


def test_migration_targets_match_orm() -> None:
    """迁移 0057 的列清单必须与 ORM 期望清单逐表逐列一致。"""
    assert _load_migration_targets() == EXPECTED_TARGETS


def test_orm_has_no_stray_double_on_non_accounting_columns() -> None:
    """防走样：这 6 张表里**只有**这 8 列被改成 Double（其余 float 列留 TD5 裁决）。"""
    expected: set[tuple[str, str]] = {(t, c) for t, cols in EXPECTED_TARGETS.items() for c in cols}
    for table in EXPECTED_TARGETS:
        for column in Base.metadata.tables[table].columns:
            if isinstance(column.type, Double):
                assert (table, column.name) in expected, (
                    f"{table}.{column.name} 意外为 Double —— TD4 只允许动 8 列，"
                    f"其余列型的变更须走 TD5 裁决"
                )
