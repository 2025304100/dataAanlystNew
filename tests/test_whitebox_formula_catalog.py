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

        def execute(self, sql: str):
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
