"""校验器字段解析 · 回归哨兵（2026-09-22 实测缺陷）。

问题：`_default_field_checker` 只用 `candidate_pool.rules.FIELD_BINDINGS`
（**筛选**字段表）解析字段，而 Step3 提交的是 `factor_compiler.FIELD_CATALOG`
（**DSL/公式**字段）里的字段 —— 两套目录不是同一张表。后果（真实报告）：

    open/high/low/close/volume → verdict=block，reason「字段 open 未注册。」

即：**任何包含行情基础字段的挖掘配置都会被判「阻断」**，而这些字段恰恰是
因子公式最常用的输入。pe_ttm / roe_ttm 之所以能过，只是因为它们恰好在筛选表里。

本哨兵逐条锁死解析规则：
1. DSL 字段（行情）必须能解析到物理绑定，不得报「未注册」
2. DSL 层 C（blocked）字段仍须阻断
3. 虚字段（prev_close，无物理列、由 close 派生投影）不得误判为不可用
4. 真未知字段仍须报「未注册」（别把兜底放宽成静默通过）
"""
from __future__ import annotations

import contextlib

import pytest

from app.services.factors.mining import validation_service as VS


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.sql = ""

    def execute(self, sql: str):
        self.sql = sql
        return self

    def fetchone(self):
        return self._row


class _FakeWarehouse:
    """只读连接替身：返回 (rows, non_null, min_date, max_date)。"""

    def __init__(self, row=(1000, 990, "2020-01-01", "2026-08-21")):
        self._row = row
        self.last_conn: _FakeConn | None = None

    def connection(self, read_only: bool = True):  # noqa: ARG002
        self.last_conn = _FakeConn(self._row)
        return contextlib.nullcontext(self.last_conn)


def _check(field: str, warehouse: _FakeWarehouse | None = None) -> dict:
    shard = VS.ValidationShard(field=field, bucket="all")
    ctx = {"warehouse": warehouse or _FakeWarehouse()}
    return VS._default_field_checker(shard, ctx)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume", "amount", "turnover_rate"])
def test_dsl_price_fields_are_resolvable(field):
    """行情 DSL 字段必须落到 raw_daily_bars，而不是被判「未注册」。"""
    res = _check(field)
    assert res["verdict"] != VS.VERDICT_BLOCK, f"{field} 不应被阻断：{res.get('reason_zh')}"
    assert res["physical_table"] == "raw_daily_bars"
    assert res["physical_column"] == field
    assert res["coverage"] == pytest.approx(0.99, abs=1e-6)


def test_dsl_valuation_field_resolves_to_snapshot_table():
    """pe_ttm 走 DSL 目录后仍应指向估值快照表（不得回退到筛选表语义）。"""
    res = _check("pe_ttm")
    assert res["verdict"] != VS.VERDICT_BLOCK
    assert res["physical_table"] == "raw_valuation_snapshots"


def test_layer_c_field_still_blocked():
    """DSL 层 C（blocked，如 proxy_score）必须继续阻断。"""
    res = _check("proxy_score")
    assert res["verdict"] == VS.VERDICT_BLOCK
    assert res.get("reason_zh")


def test_derived_prev_close_is_not_blocked():
    """prev_close 是**派生虚字段**（无物理列，由 close 投影而来），不得被判不可用。"""
    res = _check("prev_close")
    assert res["verdict"] != VS.VERDICT_BLOCK, "prev_close 是可用虚字段，不该阻断"
    assert res["physical_table"] == "raw_daily_bars"
    assert res["physical_column"] == "close", "派生字段的覆盖度按其依赖列（close）取值"
    assert res.get("derived_from") == "close"


def test_unknown_field_still_reports_unregistered():
    """兜底不得放宽：真未知字段仍须报「未注册」。"""
    res = _check("no_such_field_xyz")
    assert res["verdict"] == VS.VERDICT_BLOCK
    assert "未注册" in (res.get("reason_zh") or "")
