from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("duckdb")

from app.models.capital_flow import CapitalFlow
from app.models.macro_data import MacroIndicatorValue
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services.factors.data_sync import (
    _period_date,
    mirror_factor_inputs,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def test_mirror_factor_inputs_is_local_incremental_and_idempotent(
    db_session, tmp_path
):
    symbol = Symbol(
        symbol="600519.SH",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all(
        [
            StockValuation(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 10),
                pe_ttm=20.0,
                pb=6.0,
                total_market_cap=1_000_000,
                circulating_market_cap=900_000,
                source="akshare",
                raw_json='{"pe_ttm":20}',
            ),
            CapitalFlow(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 10),
                main_net_inflow=100_000,
                main_net_inflow_pct=2.5,
                super_large_net_inflow=60_000,
                large_net_inflow=40_000,
                source="akshare",
            ),
            MacroIndicatorValue(
                region="cn",
                category="risk",
                indicator_key="cn_10y_yield",
                name="China 10Y",
                period="2026-07-10",
                value=1.65,
                previous_value=1.66,
                source="akshare:bond_zh_us_rate",
                score=0,
                status="neutral",
            ),
        ]
    )
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")

    first = mirror_factor_inputs(
        db_session, warehouse=warehouse, batch_size=1
    )
    second = mirror_factor_inputs(
        db_session, warehouse=warehouse, batch_size=1
    )

    assert first.rows_written == 3
    assert second.rows_written == 0
    assert first.valuation_watermark > 0
    assert first.fund_flow_watermark > 0
    assert first.macro_watermark > 0

    with warehouse.connection(read_only=True) as conn:
        valuation = conn.execute(
            "SELECT symbol, pe_ttm, pb, source_hash "
            "FROM raw_valuation_snapshots"
        ).fetchone()
        flow = conn.execute(
            "SELECT symbol, main_net_inflow FROM raw_fund_flows"
        ).fetchone()
        macro = conn.execute(
            "SELECT indicator_key, period, value FROM raw_macro"
        ).fetchone()

    assert valuation[:3] == ("600519", 20.0, 6.0)
    assert len(valuation[3]) == 64
    assert flow == ("600519", 100_000.0)
    assert macro == ("cn_10y_yield", date(2026, 7, 10), 1.65)


def test_date_range_watermark_does_not_skip_full_sync(db_session, tmp_path):
    symbol = Symbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add_all(
        [
            StockValuation(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 9),
                pe_ttm=5,
                pb=0.6,
            ),
            StockValuation(
                symbol_id=symbol.id,
                trade_date=date(2026, 7, 10),
                pe_ttm=6,
                pb=0.7,
            ),
        ]
    )
    db_session.commit()
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")

    ranged = mirror_factor_inputs(
        db_session,
        warehouse=warehouse,
        start_date=date(2026, 7, 10),
        end_date=date(2026, 7, 10),
        include_fund_flows=False,
        include_macro=False,
    )
    full = mirror_factor_inputs(
        db_session,
        warehouse=warehouse,
        include_fund_flows=False,
        include_macro=False,
    )

    assert ranged.rows_written == 1
    assert full.rows_written == 2
    with warehouse.connection(read_only=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM raw_valuation_snapshots"
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-10", date(2026, 7, 10)),
        ("2026年7月", date(2026, 7, 1)),
        ("invalid", None),
    ],
)
def test_period_date(value, expected):
    assert _period_date(value) == expected


def test_mirror_factor_inputs_validates_arguments(db_session, tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    with pytest.raises(ValueError, match="batch_size"):
        mirror_factor_inputs(db_session, warehouse=warehouse, batch_size=0)
    with pytest.raises(ValueError, match="one source"):
        mirror_factor_inputs(
            db_session,
            warehouse=warehouse,
            include_valuations=False,
            include_fund_flows=False,
            include_macro=False,
        )
    with pytest.raises(ValueError, match="start_date"):
        mirror_factor_inputs(
            db_session,
            warehouse=warehouse,
            start_date=date(2026, 7, 11),
            end_date=date(2026, 7, 10),
        )


def test_store_rejects_unregistered_table(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    with pytest.raises(ValueError, match="unsupported"):
        warehouse.upsert_records("unsafe_table", [])
