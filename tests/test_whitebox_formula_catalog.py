"""Formula catalog readiness contract tests."""
from __future__ import annotations

import pytest

from app.services.factors.formula_catalog import (
    _coverage_summary,
    _field_capability,
    _query_cross_section_coverage,
    _trailing_trading_streak,
)


@pytest.mark.whitebox
def test_prev_close_is_a_derived_ready_field():
    capability = _field_capability(
        "prev_close",
        {
            "table_rows": 100,
            "nonnull_rows": 100,
            "distinct_dates": 20,
            "distinct_symbols": 5,
            "derived": True,
        },
    )

    assert capability["availability"] == "available"
    assert capability["derived"] is True
    assert capability["derived_from"] == ["close"]
    assert capability["evaluation_enabled"] is True


@pytest.mark.whitebox
def test_dividend_yield_reports_blocked_while_its_column_is_empty():
    """`dividend_yield` 物理列存在但整列为 NULL（未采集）。

    需求 §2.3 的「数据已存在（308 万行）」说的是**表行数**，不是该列的非空行数。
    能力检测必须据此判 blocked，否则又是一个「可编译但产出全 NaN」的字段。
    同时**不得**写死 `data_mode=blocked`：那会在补数后仍永久阻断。
    """
    from app.services.factors.formula_catalog import _FIELD_DATA_POLICIES

    empty = _field_capability(
        "dividend_yield",
        {"table_rows": 3_079_528, "nonnull_rows": 0, "distinct_dates": 100},
    )
    assert empty["availability"] == "blocked"
    assert empty["evaluation_enabled"] is False

    filled = _field_capability(
        "dividend_yield",
        {"table_rows": 3_079_528, "nonnull_rows": 3_079_528, "distinct_dates": 100},
    )
    assert filled["availability"] == "limited"

    assert _FIELD_DATA_POLICIES["dividend_yield"]["data_mode"] != "blocked"


@pytest.mark.whitebox
@pytest.mark.parametrize(
    ("field", "expected_status"),
    [
        ("main_net_inflow", "blocked"),
        ("proxy_score", "blocked"),
    ],
)
def test_empty_external_field_is_blocked(field: str, expected_status: str):
    capability = _field_capability(field, {})
    assert capability["availability"] == expected_status
    assert capability["preview_enabled"] is False
    assert capability["evaluation_enabled"] is False


@pytest.mark.whitebox
def test_event_and_snapshot_fields_are_not_continuous_evaluation_inputs():
    event = _field_capability(
        "lhb_institution_net",
        {"table_rows": 20, "nonnull_rows": 20, "distinct_dates": 10},
    )
    snapshot = _field_capability(
        "hot_rank_pct",
        {"table_rows": 100, "nonnull_rows": 100, "distinct_dates": 1},
    )

    assert event["availability"] == "event"
    assert event["preview_enabled"] is True
    assert event["evaluation_enabled"] is False
    assert snapshot["availability"] == "snapshot"
    assert snapshot["preview_enabled"] is True
    assert snapshot["evaluation_enabled"] is False


@pytest.mark.whitebox
def test_fund_flow_requires_an_uninterrupted_trading_day_streak():
    capability = _field_capability(
        "main_net_inflow",
        {
            "table_rows": 80,
            "nonnull_rows": 80,
            "distinct_dates": 80,
            "continuity_days": 12,
        },
    )
    assert capability["availability"] == "limited"
    assert capability["evaluation_enabled"] is False
    assert capability["continuity_days"] == 12


@pytest.mark.whitebox
def test_trailing_streak_breaks_on_a_missing_trading_day():
    from datetime import date

    calendar = [date(2026, 8, day) for day in (5, 4, 3, 2, 1)]
    covered = {date(2026, 8, day) for day in (5, 4, 2, 1)}
    assert _trailing_trading_streak(calendar, covered) == 2


@pytest.mark.whitebox
def test_coverage_summary_requires_each_recent_day_to_meet_threshold():
    summary = _coverage_summary(
        [
            ("2026-08-05", 100, 75),
            ("2026-08-04", 100, 70),
            ("2026-08-03", 100, 69),
            ("2026-08-02", 100, 100),
        ],
        minimum_coverage=0.70,
    )
    assert summary["trailing_coverage_days"] == 2
    assert summary["latest_daily_coverage"] == 0.75
    assert summary["daily_coverage_p50"] == 0.75


@pytest.mark.whitebox
def test_cross_section_coverage_uses_only_active_cn_stocks():
    class Result:
        @staticmethod
        def fetchall():
            return []

    class Connection:
        sql = ""

        def execute(self, sql: str, params=None):
            self.sql = sql
            return Result()

    conn = Connection()
    summary = _query_cross_section_coverage(
        conn,
        table="raw_capital_flow",
        field="main_net_inflow",
        minimum_coverage=0.70,
    )

    normalized_sql = " ".join(conn.sql.lower().split())
    assert "join raw_asset_universe as universe" in normalized_sql
    assert "lower(universe.asset_type) = 'stock'" in normalized_sql
    assert "lower(universe.region) = 'cn'" in normalized_sql
    assert "universe.is_active = true" in normalized_sql
    assert summary["trailing_coverage_days"] == 0
    assert summary["latest_daily_coverage"] is None


def test_catalog_cache_ttl_starts_after_a_slow_query_finishes(monkeypatch):
    from app.services.factors import formula_catalog as catalog
    clock, queries = [0.0], []
    def slow_query(warehouse):
        queries.append(warehouse)
        clock[0] += 40
        return {"close": {"table_rows": 1, "nonnull_rows": 1}}
    monkeypatch.setattr(catalog, "monotonic", lambda: clock[0])
    monkeypatch.setattr(catalog, "_warehouse_cache_key", lambda warehouse: ("slow-query-contract", 1))
    monkeypatch.setattr(catalog, "_query_field_stats", slow_query)
    catalog.invalidate_formula_catalog_cache()
    warehouse = object()
    try:
        catalog.build_formula_catalog(warehouse)
        catalog.build_formula_catalog(warehouse)
        assert len(queries) == 1
    finally:
        catalog.invalidate_formula_catalog_cache()


def _coverage_fixture_conn():
    """Four active stocks; the last session only mirrored bars for two of them.

    The last session is only partially synced: bars and capital-flow rows are
    missing together for symbols 3 and 4. The denominator must still be the
    full active universe (4), otherwise the shrunk universe reports a fake
    100% coverage and defeats the consecutive-gap threshold.
    """
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect()
    conn.execute("CREATE TEMP TABLE coverage_cn_closed_days (trade_date DATE)")
    conn.execute("CREATE TABLE raw_asset_universe (symbol VARCHAR, asset_type VARCHAR, region VARCHAR, is_active BOOLEAN)")
    conn.executemany("INSERT INTO raw_asset_universe VALUES (?, 'stock', 'CN', TRUE)",
                     [(str(i),) for i in (1, 2, 3, 4)])
    conn.execute("CREATE TABLE raw_daily_bars (symbol VARCHAR, trade_date DATE)")
    conn.executemany("INSERT INTO raw_daily_bars VALUES (?, ?)",
                     [(str(s), d) for d in ("2026-08-03", "2026-08-04") for s in (1, 2, 3, 4)]
                     + [(s, "2026-08-05") for s in (1, 2)])
    conn.execute("CREATE TABLE raw_capital_flow (symbol VARCHAR, trade_date DATE, main_net_inflow DOUBLE)")
    conn.executemany("INSERT INTO raw_capital_flow VALUES (?, ?, ?)",
                     [(str(s), d, 1.0) for d in ("2026-08-03", "2026-08-04") for s in (1, 2, 3, 4)]
                     + [(s, "2026-08-05", 1.0) for s in (1, 2)])
    return conn


@pytest.mark.whitebox
def test_sparse_session_does_not_shrink_the_stock_coverage_denominator():
    from app.services.factors.formula_catalog import _prepare_stock_coverage, _query_coverage_counts

    conn = _coverage_fixture_conn()
    try:
        _prepare_stock_coverage(conn)
        counts = _query_coverage_counts(conn, table="raw_capital_flow", fields=["main_net_inflow"])
    finally:
        conn.close()

    daily, longest_gap = counts["main_net_inflow"]
    assert [(str(day), universe, covered) for day, universe, covered in daily] == [
        ("2026-08-05", 4, 2),
        ("2026-08-04", 4, 4),
        ("2026-08-03", 4, 4),
    ]
    assert longest_gap == 1


@pytest.mark.whitebox
def test_cross_section_fallback_keeps_the_full_universe_on_sparse_sessions():
    from app.services.factors.formula_catalog import _query_cross_section_coverage

    conn = _coverage_fixture_conn()
    try:
        summary = _query_cross_section_coverage(
            conn,
            table="raw_capital_flow",
            field="main_net_inflow",
            minimum_coverage=0.70,
            _calendar_ready=True,
        )
    finally:
        conn.close()

    assert summary["latest_daily_coverage"] == 0.5
    assert summary["expected_rows"] == 12
    assert summary["covered_rows"] == 10
    assert summary["trailing_coverage_days"] == 0
    assert summary["max_consecutive_gap_days"] == 1


def test_dataset_quality_catalog_has_separate_cache_from_full_catalog(monkeypatch):
    from app.services.factors import formula_catalog as catalog
    calls=[]
    def query(warehouse, *, field_keys=None):
        calls.append(field_keys)
        return {key: {"table_rows": 1, "nonnull_rows": 1} for key in (field_keys or {"close", "main_net_inflow"})}
    monkeypatch.setattr(catalog, "_warehouse_cache_key", lambda warehouse: ("dataset-cache-contract", 1))
    monkeypatch.setattr(catalog, "_query_field_stats", query)
    catalog.invalidate_formula_catalog_cache()
    try:
        warehouse=object()
        scoped=catalog.build_formula_catalog(warehouse,field_keys={"main_net_inflow"})
        full=catalog.build_formula_catalog(warehouse)
        catalog.build_formula_catalog(warehouse,field_keys={"main_net_inflow"})
        assert [field["key"] for field in scoped["fields"]]==["main_net_inflow"]
        assert len(full["fields"])>1
        assert calls==[{"main_net_inflow"},None]
    finally:
        catalog.invalidate_formula_catalog_cache()
