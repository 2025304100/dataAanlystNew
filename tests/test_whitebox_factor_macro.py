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


def _asset_row(
    symbol: str,
    *,
    asset_type: str,
    region: str,
    row_id: int,
):
    return {
        "symbol": symbol,
        "name": symbol,
        "asset_type": asset_type,
        "market": "sh",
        "region": region,
        "is_active": True,
        "source": "test",
        "source_row_id": row_id,
        "updated_at": datetime(2026, 7, 1),
    }


def _bar_row(
    symbol: str,
    period: date,
    *,
    close: float,
    amount: float,
    row_id: int,
):
    return {
        "symbol": symbol,
        "trade_date": period,
        "adjust": "qfq",
        "open": close - 0.1,
        "high": close + 0.1,
        "low": close - 0.2,
        "close": close,
        "volume": amount / close,
        "amount": amount,
        "turnover_rate": 1.0,
        "source": "test",
        "source_origin": "test",
        "source_row_id": row_id,
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "liquidity-test",
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


def test_enhanced_market_liquidity_uses_cn_stocks_only(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    warehouse.upsert_records(
        "raw_asset_universe",
        [
            _asset_row("600001", asset_type="stock", region="cn", row_id=1),
            _asset_row("000001", asset_type="stock", region="cn", row_id=2),
            _asset_row("510300", asset_type="etf", region="cn", row_id=3),
            _asset_row("AAPL", asset_type="stock", region="us", row_id=4),
        ],
    )
    start = date(2026, 6, 1)
    bar_rows = []
    row_id = 1
    for offset in range(26):
        period = start + timedelta(days=offset)
        stock_total = 1000 if offset == 25 else 200 + offset * 10
        for symbol in ("600001", "000001"):
            bar_rows.append(
                _bar_row(
                    symbol,
                    period,
                    close=10 + offset,
                    amount=stock_total / 2,
                    row_id=row_id,
                )
            )
            row_id += 1
        for symbol in ("510300", "AAPL"):
            bar_rows.append(
                _bar_row(
                    symbol,
                    period,
                    close=100 - offset,
                    amount=100000 if offset == 25 else 1,
                    row_id=row_id,
                )
            )
            row_id += 1
    warehouse.upsert_daily_bars(
        bar_rows,
        source_key="test.liquidity.bars",
        watermark=row_id,
    )
    macro_rows = []
    for offset in range(6):
        period = start + timedelta(days=20 + offset)
        macro_rows.extend(
            [
                _macro_row("cn_10y_yield", period, 1.5 - offset * 0.03),
                _macro_row("us_10y_yield", period, 4.0 - offset * 0.04),
                _macro_row("cn_margin_sh", period, 100 + offset),
                _macro_row("cn_margin_sz", period, 100 + offset),
            ]
        )
    warehouse.upsert_records("raw_macro", macro_rows)

    state = calculate_macro_regime(
        warehouse,
        as_of=start + timedelta(days=25),
        lookback=5,
    )

    assert state.regime == "risk_on"
    assert state.liquidity_available is True
    assert state.liquidity_score is not None and state.liquidity_score >= 60
    assert state.market_amount_change_ratio == pytest.approx(1.5)
    assert state.market_amount_z20 is not None and state.market_amount_z20 > 2
    assert state.advancing_ratio == pytest.approx(1.0)
    assert state.margin_amount_divergence == pytest.approx(-1.45)
