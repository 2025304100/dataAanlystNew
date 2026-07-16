from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.health import get_factor_health
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


def _seed_factor_panel(warehouse: FactorWarehouse):
    dates = [
        date(2026, 6, 1) + timedelta(days=offset) for offset in range(20)
    ]
    bars = []
    valuations = []
    flows = []
    financials = []
    sentiments = []
    hot_ranks = []
    tail_proxies = []
    row_id = 0
    for rank, (symbol, pe, pb, inflow) in enumerate((
        ("000001", 10, 1, 10),
        ("000002", 20, 2, 20),
        ("600000", 40, 4, 30),
        ("600519", 100, 20, 100),
    ), start=1):
        valuations.append(
            {
                "symbol": symbol,
                "trade_date": dates[0],
                "pe_ttm": pe,
                "pb": pb,
                "source": "test",
                "source_hash": symbol,
                "ingested_at": datetime(2026, 7, 1),
                "batch_id": "health-source",
            }
        )
        financials.extend(
            [
                {
                    "symbol": symbol,
                    "report_period": date(2024, 12, 31),
                    "announcement_date": date(2025, 3, 30),
                    "report_type": "financial_analysis",
                    "roe_ttm": float(pe),
                    "source": "test",
                    "ingested_at": datetime(2026, 7, 1),
                    "batch_id": "health-source",
                },
                {
                    "symbol": symbol,
                    "report_period": date(2025, 12, 31),
                    "announcement_date": date(2026, 3, 30),
                    "report_type": "financial_analysis",
                    "roe_ttm": float(pe + inflow),
                    "source": "test",
                    "ingested_at": datetime(2026, 7, 1),
                    "batch_id": "health-source",
                },
            ]
        )
        sentiments.append(
            {
                "symbol": symbol,
                "trade_date": dates[-1],
                "has_lhb": True,
                "lhb_institution_net": float(inflow * 10),
                "source": "test",
                "ingested_at": datetime(2026, 7, 1),
                "batch_id": "health-source",
            }
        )
        hot_ranks.append(
            {
                "symbol": symbol,
                "trade_date": dates[-1],
                "hot_rank": rank,
                "hot_rank_total": 100,
                "hot_rank_pct": float(rank),
                "has_lhb": False,
                "source": "hot-rank-test",
                "ingested_at": datetime(2026, 7, 1),
                "batch_id": "health-source",
            }
        )
        tail_proxies.append(
            {
                "symbol": symbol,
                "trade_date": dates[-1],
                "minute_count": 241,
                "tail_minute_count": 31,
                "day_amount": 10_000,
                "tail_amount": 2_000,
                "tail_amount_share": 0.2,
                "tail_activity_ratio": 1.5,
                "tail_return": 0.01,
                "close_location": 0.8,
                "proxy_score": rank / 10,
                "source": "tail-test",
                "ingested_at": datetime(2026, 7, 1),
                "batch_id": "health-source",
            }
        )
        for index, trade_date in enumerate(dates, start=1):
            row_id += 1
            bars.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "adjust": "qfq",
                    "universe_symbol_id": row_id,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 1000,
                    "turnover_rate": index,
                    "source": "test",
                    "source_origin": "universe_daily_bars",
                    "source_row_id": row_id,
                    "ingested_at": datetime(2026, 7, 1),
                    "batch_id": "health-source",
                }
            )
            flows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "main_net_inflow": inflow,
                    "source": "test",
                    "ingested_at": datetime(2026, 7, 1),
                    "batch_id": "health-source",
                }
            )
    warehouse.upsert_daily_bars(
        bars, source_key="health.bars", watermark=row_id
    )
    warehouse.upsert_records("raw_valuation_snapshots", valuations)
    warehouse.upsert_records("raw_fund_flows", flows)
    warehouse.upsert_records("raw_financial_reports", financials)
    warehouse.upsert_records("raw_sentiment", sentiments)
    warehouse.upsert_records("raw_sentiment", hot_ranks)
    warehouse.upsert_records("raw_tail_proxy", tail_proxies)
    return dates


def test_factor_health_reports_raw_tables_and_coverage(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    dates = _seed_factor_panel(warehouse)
    calculate_stock_factors(
        warehouse,
        start_date=dates[-1],
        end_date=dates[-1],
        calc_batch_id="health-test",
        valuation_max_age_days=30,
    )

    report = get_factor_health(warehouse, calc_batch_id="health-test")

    assert report.status == "healthy"
    assert report.latest_bar_date == str(dates[-1])
    assert report.calc_batch_id == "health-test"
    assert {item.table for item in report.raw_tables} >= {
        "raw_daily_bars",
        "raw_valuation_snapshots",
        "raw_financial_reports",
        "raw_fund_flows",
        "factor_values",
    }
    assert {item.factor_code for item in report.factors} == {
        "ep_ttm",
        "negative_pb",
        "roe_yoy_growth",
        "main_inflow_5d_ratio",
        "lhb_institution_net_ratio",
        "hot_rank_attention",
        "tail_accumulation_proxy",
        "turnover_z20",
    }
    assert {item.coverage for item in report.factors} == {1.0}


def test_factor_health_degrades_when_batch_or_core_coverage_is_missing(
    tmp_path,
):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    warehouse.initialize()

    missing = get_factor_health(warehouse)
    assert missing.status == "degraded"
    assert missing.reasons == ["factor_batch_missing"]

    dates = _seed_factor_panel(warehouse)
    calculate_stock_factors(
        warehouse,
        start_date=dates[0],
        end_date=dates[0],
        calc_batch_id="warmup",
        valuation_max_age_days=30,
    )
    warmup = get_factor_health(warehouse, calc_batch_id="warmup")

    assert warmup.status == "degraded"
    assert any("main_inflow_5d_ratio:coverage" in item for item in warmup.reasons)
    assert any("turnover_z20:coverage" in item for item in warmup.reasons)


def test_factor_health_reports_uninitialized_warehouse(tmp_path):
    report = get_factor_health(
        FactorWarehouse(tmp_path / "missing.duckdb")
    )

    assert report.status == "failed"
    assert report.warehouse_available is False
    assert report.reasons == ["warehouse_not_initialized"]


def test_factor_health_validates_thresholds(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "factor.duckdb")
    warehouse.initialize()
    with pytest.raises(ValueError, match="thresholds"):
        get_factor_health(
            warehouse, minimum_coverage=0.95, healthy_coverage=0.9
        )
