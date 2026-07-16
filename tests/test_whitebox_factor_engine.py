from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("duckdb")

from app.services.factors.factor_engine import (
    calculate_stock_factors,
    winsorize_cross_section,
    zscore_cross_section,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def _bar(
    symbol: str,
    trade_date: date,
    row_id: int,
    *,
    amount: float = 1000,
    turnover_rate: float = 1,
):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "adjust": "qfq",
        "business_symbol_id": None,
        "universe_symbol_id": row_id,
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "volume": 100,
        "amount": amount,
        "turnover_rate": turnover_rate,
        "source": "test",
        "source_origin": "universe_daily_bars",
        "source_row_id": row_id,
        "source_updated_at": datetime(2026, 7, 1),
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "bars-test",
    }


def _valuation(symbol: str, trade_date: date, pe: float, pb: float):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "pe_ttm": pe,
        "pb": pb,
        "dividend_yield": None,
        "total_market_cap": None,
        "circulating_market_cap": None,
        "source": "test",
        "source_hash": f"{symbol}-{trade_date}",
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "valuation-test",
    }


def _flow(symbol: str, trade_date: date, value: float):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "main_net_inflow": value,
        "main_net_inflow_pct": None,
        "super_large_net_inflow": None,
        "large_net_inflow": None,
        "medium_net_inflow": None,
        "small_net_inflow": None,
        "source": "test",
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "flow-test",
    }


def _financial(
    symbol: str,
    report_period: date,
    announcement_date: date,
    roe_ttm: float,
):
    return {
        "symbol": symbol,
        "report_period": report_period,
        "announcement_date": announcement_date,
        "report_type": "financial_analysis",
        "roe_ttm": roe_ttm,
        "net_profit": None,
        "revenue": None,
        "net_profit_yoy": None,
        "revenue_yoy": None,
        "source": "test",
        "source_hash": (
            f"{symbol}-{report_period}-{announcement_date}-{roe_ttm}"
        ),
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "financial-test",
    }


def _sentiment(symbol: str, trade_date: date, institution_net: float):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "hot_rank": None,
        "hot_rank_total": None,
        "hot_rank_pct": None,
        "has_lhb": True,
        "lhb_institution_net": institution_net,
        "source": "test",
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "sentiment-test",
    }


def _hot_rank(
    symbol: str, trade_date: date, rank: int, total: int = 100
):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "hot_rank": rank,
        "hot_rank_total": total,
        "hot_rank_pct": rank / total * 100,
        "has_lhb": False,
        "lhb_institution_net": None,
        "source": "hot-rank-test",
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "hot-rank-test",
    }


def _tail_proxy(symbol: str, trade_date: date, proxy_score: float):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "minute_count": 241,
        "tail_minute_count": 31,
        "day_amount": 10_000,
        "tail_amount": 2_000,
        "tail_amount_share": 0.2,
        "tail_activity_ratio": 1.5,
        "tail_return": 0.01,
        "close_location": 0.8,
        "proxy_score": proxy_score,
        "source": "tail-test",
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "tail-test",
    }


def _seed_factor_panel(warehouse: FactorWarehouse):
    dates = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(20)]
    specs = {
        "000001": (10, 1, 10),
        "000002": (20, 2, 20),
        "600000": (40, 4, 30),
        "600519": (100, 20, 100),
    }
    bars = []
    valuations = []
    flows = []
    financials = []
    sentiments = []
    hot_ranks = []
    tail_proxies = []
    row_id = 0
    for rank, (symbol, (pe, pb, inflow)) in enumerate(
        specs.items(), start=1
    ):
        valuations.append(_valuation(symbol, dates[0], pe, pb))
        financials.extend(
            [
                _financial(
                    symbol,
                    date(2024, 12, 31),
                    date(2025, 3, 30),
                    float(pe),
                ),
                _financial(
                    symbol,
                    date(2025, 12, 31),
                    date(2026, 3, 30),
                    float(pe + inflow),
                ),
            ]
        )
        sentiments.append(_sentiment(symbol, dates[-1], inflow * 10))
        hot_ranks.append(_hot_rank(symbol, dates[-1], rank))
        tail_proxies.append(
            _tail_proxy(symbol, dates[-1], rank / 10)
        )
        for index, trade_date in enumerate(dates, start=1):
            row_id += 1
            bars.append(
                _bar(
                    symbol,
                    trade_date,
                    row_id,
                    turnover_rate=float(index),
                )
            )
            flows.append(_flow(symbol, trade_date, inflow))
    warehouse.upsert_daily_bars(
        bars, source_key="test.bars", watermark=row_id
    )
    warehouse.upsert_records("raw_valuation_snapshots", valuations)
    warehouse.upsert_records("raw_fund_flows", flows)
    warehouse.upsert_records("raw_financial_reports", financials)
    warehouse.upsert_records("raw_sentiment", sentiments)
    warehouse.upsert_records("raw_sentiment", hot_ranks)
    warehouse.upsert_records("raw_tail_proxy", tail_proxies)
    return dates


def test_calculate_stock_factors_formulas_scaling_and_idempotency(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    dates = _seed_factor_panel(warehouse)

    first = calculate_stock_factors(
        warehouse,
        start_date=dates[-1],
        end_date=dates[-1],
        calc_batch_id="calc-test",
        valuation_max_age_days=30,
    )
    second = calculate_stock_factors(
        warehouse,
        start_date=dates[-1],
        end_date=dates[-1],
        calc_batch_id="calc-test",
        valuation_max_age_days=30,
    )

    assert first.rows_written == 32
    assert first.eligible_rows == 32
    assert first.symbol_count == 4
    assert first.trade_date_count == 1
    assert set(first.coverage_by_factor.values()) == {1.0}
    assert second.rows_written == 32

    with warehouse.connection(read_only=True) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM factor_values "
            "WHERE calc_batch_id = 'calc-test'"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT symbol, factor_code, raw_value, winsorized_value, "
            "normalized_value, eligible FROM factor_values "
            "WHERE calc_batch_id = 'calc-test' "
            "ORDER BY factor_code, symbol"
        ).fetchdf()

    assert count == 32
    ep = rows[
        (rows["symbol"] == "000001") & (rows["factor_code"] == "ep_ttm")
    ].iloc[0]
    pb_outlier = rows[
        (rows["symbol"] == "600519")
        & (rows["factor_code"] == "negative_pb")
    ].iloc[0]
    flow = rows[
        (rows["symbol"] == "000001")
        & (rows["factor_code"] == "main_inflow_5d_ratio")
    ].iloc[0]
    lhb = rows[
        (rows["symbol"] == "000001")
        & (rows["factor_code"] == "lhb_institution_net_ratio")
    ].iloc[0]
    hot = rows[
        (rows["symbol"] == "000001")
        & (rows["factor_code"] == "hot_rank_attention")
    ].iloc[0]
    tail = rows[
        (rows["symbol"] == "000001")
        & (rows["factor_code"] == "tail_accumulation_proxy")
    ].iloc[0]

    assert ep["raw_value"] == pytest.approx(0.1)
    assert flow["raw_value"] == pytest.approx(0.01)
    assert lhb["raw_value"] == pytest.approx(0.1)
    assert hot["raw_value"] == pytest.approx(0.99)
    assert tail["raw_value"] == pytest.approx(0.1)
    assert pb_outlier["raw_value"] == -20
    assert pb_outlier["winsorized_value"] > -20
    assert np.isfinite(rows["normalized_value"]).all()
    means = rows.groupby("factor_code")["normalized_value"].mean()
    assert np.allclose(means.to_numpy(), 0, atol=1e-7)


def test_calculation_does_not_use_future_valuation(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    signal_date = date(2026, 7, 10)
    warehouse.upsert_daily_bars(
        [_bar("000001", signal_date, 1)],
        source_key="test.bars",
        watermark=1,
    )
    warehouse.upsert_records(
        "raw_valuation_snapshots",
        [_valuation("000001", signal_date + timedelta(days=1), 10, 1)],
    )

    calculate_stock_factors(
        warehouse,
        start_date=signal_date,
        end_date=signal_date,
        calc_batch_id="future-check",
    )

    with warehouse.connection(read_only=True) as conn:
        ep = conn.execute(
            "SELECT raw_value, eligible FROM factor_values "
            "WHERE calc_batch_id = 'future-check' "
            "AND factor_code = 'ep_ttm'"
        ).fetchone()
    assert ep == (None, False)


def test_roe_yoy_growth_respects_announcement_date(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    before = date(2025, 4, 29)
    announced = date(2025, 4, 30)
    warehouse.upsert_daily_bars(
        [
            _bar("000001", before, 1),
            _bar("000001", announced, 2),
        ],
        source_key="test.bars",
        watermark=2,
    )
    warehouse.upsert_records(
        "raw_financial_reports",
        [
            _financial(
                "000001",
                date(2024, 3, 31),
                date(2024, 4, 30),
                10,
            ),
            _financial(
                "000001",
                date(2025, 3, 31),
                announced,
                12,
            ),
        ],
    )

    calculate_stock_factors(
        warehouse,
        start_date=before,
        end_date=announced,
        calc_batch_id="roe-announcement-check",
    )

    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute(
            "SELECT trade_date, raw_value, eligible FROM factor_values "
            "WHERE calc_batch_id = 'roe-announcement-check' "
            "AND factor_code = 'roe_yoy_growth' ORDER BY trade_date"
        ).fetchall()
    assert rows == [(before, None, False), (announced, 2.0, True)]


def test_roe_yoy_growth_uses_visible_revision_and_requires_prior_year(
    tmp_path,
):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    first_date = date(2025, 5, 1)
    revision_date = date(2025, 5, 15)
    bars = [
        _bar("000001", first_date, 1),
        _bar("000001", revision_date, 2),
        _bar("000002", revision_date, 3),
    ]
    warehouse.upsert_daily_bars(
        bars, source_key="test.bars", watermark=3
    )
    warehouse.upsert_records(
        "raw_financial_reports",
        [
            _financial(
                "000001",
                date(2024, 3, 31),
                date(2024, 4, 30),
                10,
            ),
            _financial(
                "000001",
                date(2025, 3, 31),
                date(2025, 4, 30),
                12,
            ),
            _financial(
                "000001",
                date(2025, 3, 31),
                revision_date,
                15,
            ),
            _financial(
                "000002",
                date(2025, 3, 31),
                date(2025, 4, 30),
                20,
            ),
        ],
    )

    calculate_stock_factors(
        warehouse,
        start_date=first_date,
        end_date=revision_date,
        calc_batch_id="roe-revision-check",
    )

    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute(
            "SELECT symbol, trade_date, raw_value, eligible "
            "FROM factor_values "
            "WHERE calc_batch_id = 'roe-revision-check' "
            "AND factor_code = 'roe_yoy_growth' "
            "ORDER BY symbol, trade_date"
        ).fetchall()
    assert rows == [
        ("000001", first_date, 2.0, True),
        ("000001", revision_date, 5.0, True),
        ("000002", revision_date, None, False),
    ]


def test_cross_section_guards_zero_mad_zero_std_and_non_finite():
    source = pd.Series([1.0, 1.0, 1.0, 100.0, np.inf, np.nan])

    winsorized = winsorize_cross_section(source)
    normalized = zscore_cross_section(pd.Series([2.0, 2.0, 2.0]))

    assert winsorized.iloc[3] < 100
    assert pd.isna(winsorized.iloc[4])
    assert normalized.tolist() == [0.0, 0.0, 0.0]


def test_calculation_validates_arguments(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    with pytest.raises(ValueError, match="start_date"):
        calculate_stock_factors(
            warehouse,
            start_date=date(2026, 7, 11),
            end_date=date(2026, 7, 10),
        )
    with pytest.raises(ValueError, match="valuation_max_age_days"):
        calculate_stock_factors(warehouse, valuation_max_age_days=-1)
