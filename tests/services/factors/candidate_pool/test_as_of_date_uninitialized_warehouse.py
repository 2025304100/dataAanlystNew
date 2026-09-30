"""白盒：数仓未初始化（表不存在）时，基准日期解析必须降级而不是 500。

起因（拟真/CI 实测）：`tests/services/factors/candidate_pool/test_e2e.py::TestHttpLayer`
两条用例撞 `_duckdb.CatalogException: Table with name raw_valuation_snapshots does not exist`。
根因是建表只发生在 `POST /factors/warehouse/initialize` 与各 job 里，读路径
`wh.connection()` 不惰性建表；而 `analysis.py` 的既有意图写得很清楚：
“解析不出 as_of（数仓空）→ 用今天，让分析自己报告无数据，**而不是在这里崩**”。
实现只覆盖了“表里没数据”，漏了“表根本不存在”。本文件把两件事分开钉住。
"""
from __future__ import annotations

import datetime as dt

import duckdb
import pytest

pytestmark = pytest.mark.whitebox

from app.services.factors.candidate_pool import rules as R  # noqa: E402


@pytest.fixture()
def empty_conn():
    con = duckdb.connect(":memory:")
    try:
        yield con
    finally:
        con.close()


def test_missing_table_degrades_instead_of_raising(empty_conn):
    """表不存在 → 返回“解析不出 as_of”，并在证据里留下可解释的原因。"""
    resolved, adjusted, evidence = R._resolve_as_of_date(
        empty_conn, dt.date(2026, 9, 30), baseline_days=30, completeness=0.9
    )

    assert resolved is None, "数仓未初始化时不该给出 as_of"
    assert adjusted is False
    assert evidence.get("warehouse_uninitialized") is True, (
        f"证据里必须写明是“数仓未建表”，否则调用方无从解释：{evidence}"
    )
    # 与既有“无数据”分支保持同样的键形状，避免上层按键取值时 KeyError
    assert {"candidate_ratios", "median_baseline", "completeness_threshold"} <= set(evidence)


def test_existing_empty_table_branch_unchanged(empty_conn):
    """表存在但没有任何行：仍走原来的优雅分支（不能被新代码改变语义）。"""
    empty_conn.execute(
        "CREATE TABLE raw_valuation_snapshots (trade_date DATE, symbol VARCHAR)"
    )

    resolved, adjusted, evidence = R._resolve_as_of_date(
        empty_conn, dt.date(2026, 9, 30), baseline_days=30, completeness=0.9
    )

    assert resolved is None
    assert adjusted is False
    assert "warehouse_uninitialized" not in evidence, "空表不是“未初始化”，别混为一谈"


def test_other_database_errors_are_not_swallowed(empty_conn):
    """只放行“表不存在”；别的数据库错误必须继续抛（否则真故障会被抹成无数据）。"""
    empty_conn.execute("CREATE TABLE raw_valuation_snapshots (trade_date DATE, symbol VARCHAR)")
    # 造一个必然失败的调用：baseline_days 传非数字 → 不是 “does not exist” 类错误
    with pytest.raises(Exception) as exc:
        R._resolve_as_of_date(empty_conn, None, baseline_days="not-a-number", completeness=0.9)
    assert "does not exist" not in str(exc.value)
