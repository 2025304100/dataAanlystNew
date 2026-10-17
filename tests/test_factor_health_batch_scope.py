"""Default-factor health must not borrow a newer research batch or version."""
from datetime import timedelta

import pytest

from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.health import get_factor_health
from app.services.factors.store import FactorWarehouse
from tests.test_whitebox_factor_health import _seed_factor_panel

pytestmark = pytest.mark.whitebox


def _full_batch(tmp_path, *, first_day=False):
    warehouse = FactorWarehouse(tmp_path / "health-scope.duckdb")
    dates = _seed_factor_panel(warehouse)
    day = dates[0] if first_day else dates[-1]
    calculate_stock_factors(warehouse, start_date=day, end_date=day,
        calc_batch_id="complete-defaults", valuation_max_age_days=30)
    with warehouse.connection(read_only=True) as conn:
        original = conn.execute("SELECT * FROM factor_values WHERE calc_batch_id=?",
            ["complete-defaults"]).fetchdf()
    return warehouse, original


@pytest.mark.parametrize("research_scope", ["one_v3", "all_wrong_version"])
def test_default_health_keeps_latest_complete_default_versions_after_research_training(
    tmp_path, research_scope,
):
    warehouse, original = _full_batch(tmp_path)
    before = get_factor_health(warehouse)
    newer = original.copy()
    if research_scope == "one_v3":
        newer = newer.loc[newer.factor_code == "turnover_z20"].copy()
    newer["factor_version"] = 3 if research_scope == "one_v3" else 2
    newer["calc_batch_id"] = "newer-research-only"
    newer["created_at"] = newer.created_at + timedelta(seconds=10)
    warehouse.upsert_frame("factor_values", newer)

    after = get_factor_health(warehouse)
    assert before.calc_batch_id == after.calc_batch_id == "complete-defaults"
    assert [row.coverage for row in before.factors] == [row.coverage for row in after.factors]
    assert len(after.factors) == 8


def test_new_complete_default_batch_with_real_missing_inputs_stays_degraded(tmp_path):
    warehouse, newer = _full_batch(tmp_path)
    newer["calc_batch_id"] = "newer-complete-but-incomplete-inputs"
    newer["created_at"] = newer.created_at + timedelta(seconds=10)
    newer.loc[newer.factor_code == "ep_ttm", "eligible"] = False
    warehouse.upsert_frame("factor_values", newer)

    report = get_factor_health(warehouse)
    assert report.calc_batch_id == "newer-complete-but-incomplete-inputs"
    assert report.status == "degraded"
    assert next(row for row in report.factors if row.factor_code == "ep_ttm").coverage == 0.0


def test_default_health_does_not_count_another_version_as_native_coverage(tmp_path):
    warehouse, values = _full_batch(tmp_path, first_day=True)
    newer = values.loc[values.factor_code == "turnover_z20"].copy()
    newer["factor_version"] = 3
    newer["raw_value"] = newer["winsorized_value"] = newer["normalized_value"] = 1.0
    newer["eligible"] = True
    warehouse.upsert_frame("factor_values", newer)

    report = get_factor_health(warehouse, calc_batch_id="complete-defaults")
    turnover = next(row for row in report.factors if row.factor_code == "turnover_z20")
    assert turnover.universe_symbols == 4
    assert turnover.eligible_symbols == 0
    assert turnover.coverage == 0.0  # Native v1 has fewer than twenty observations.


def test_research_only_warehouse_has_no_default_factor_health_batch(tmp_path):
    warehouse, values = _full_batch(tmp_path)
    values = values.loc[values.factor_code == "turnover_z20"].copy()
    values["factor_version"] = 3
    values["calc_batch_id"] = "only-research"
    with warehouse.connection() as conn:
        conn.execute("DELETE FROM factor_values")
    warehouse.upsert_frame("factor_values", values)

    report = get_factor_health(warehouse)
    assert report.calc_batch_id is None
    assert report.status == "degraded"
    assert report.reasons == ["factor_batch_missing"]
    assert report.factors == []
