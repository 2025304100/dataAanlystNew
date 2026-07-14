from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from app.services.factors.macro import calculate_macro_regime
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def _macro_row(key: str, period: date, value: float):
    return {
        "indicator_key": key,
        "period": period,
        "value": value,
        "previous_value": None,
        "source": "test",
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "macro-test",
    }


def test_macro_regime_is_market_state_not_cross_section(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    start = date(2026, 7, 1)
    rows = []
    for offset in range(6):
        period = start + timedelta(days=offset)
        rows.extend(
            [
                _macro_row("cn_10y_yield", period, 1.5 + offset * 0.04),
                _macro_row("us_10y_yield", period, 4.0 + offset * 0.06),
                _macro_row("cn_margin_sh", period, 100 - offset * 2),
                _macro_row("cn_margin_sz", period, 100 - offset * 2),
            ]
        )
    warehouse.upsert_records("raw_macro", rows)

    state = calculate_macro_regime(
        warehouse, as_of=start + timedelta(days=5), lookback=5
    )

    assert state.available is True
    assert state.regime == "defensive"
    assert state.position_multiplier == 0.70
    assert state.cn_10y_change == pytest.approx(0.20)
    assert state.us_10y_change == pytest.approx(0.30)
    assert state.margin_change_ratio == pytest.approx(-0.10)
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM factor_values"
        ).fetchone()[0] == 0


def test_macro_regime_respects_as_of_cutoff(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    cutoff = date(2026, 7, 5)
    rows = []
    for offset in range(2):
        period = cutoff - timedelta(days=1 - offset)
        rows.extend(
            [
                _macro_row("cn_10y_yield", period, 1.5 + offset * 0.2),
                _macro_row("us_10y_yield", period, 4.0 + offset * 0.3),
                _macro_row("cn_margin_sh", period, 100 - offset * 10),
                _macro_row("cn_margin_sz", period, 100 - offset * 10),
            ]
        )
    future = cutoff + timedelta(days=1)
    rows.extend(
        [
            _macro_row("cn_10y_yield", future, 1.0),
            _macro_row("us_10y_yield", future, 3.0),
            _macro_row("cn_margin_sh", future, 130),
            _macro_row("cn_margin_sz", future, 130),
        ]
    )
    warehouse.upsert_records("raw_macro", rows)

    state = calculate_macro_regime(
        warehouse, as_of=cutoff, lookback=5
    )

    assert state.regime == "defensive"
    assert state.cn_10y_change == pytest.approx(0.2)
    assert state.margin_change_ratio == pytest.approx(-0.1)


def test_macro_regime_handles_missing_data_and_validates_lookback(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    empty = calculate_macro_regime(warehouse)

    assert empty.available is False
    assert empty.regime == "neutral"
    assert empty.position_multiplier == 1.0
    with pytest.raises(ValueError, match="lookback"):
        calculate_macro_regime(warehouse, lookback=0)
