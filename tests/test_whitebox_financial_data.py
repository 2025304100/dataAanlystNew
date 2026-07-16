from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.financial_report import StockFinancialReport
from app.models.symbol import Symbol
from app.services.financial_data import (
    _akshare_financial_symbol,
    sync_symbol_financial_reports,
)


pytestmark = pytest.mark.whitebox


def test_financial_sync_preserves_versions_and_is_idempotent(db_session):
    symbol = Symbol(
        symbol="600519",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
    )
    db_session.add(symbol)
    db_session.flush()
    frame = pd.DataFrame(
        [
            {
                "REPORT_DATE": "2025-12-31",
                "NOTICE_DATE": "2026-03-30",
                "ROEJQ": "18.2",
                "PARENT_NETPROFIT": 100,
                "TOTAL_OPERATE_INCOME": 200,
                "RAW_MARKER": "initial",
            },
            {
                "REPORT_DATE": "2025-12-31",
                "NOTICE_DATE": "2026-04-15",
                "ROEJQ": "19.1",
                "PARENT_NETPROFIT": 110,
                "TOTAL_OPERATE_INCOME": 210,
                "RAW_MARKER": "revision",
            },
        ]
    )

    with patch(
        "app.services.financial_data.call_akshare_with_retry",
        return_value=frame,
    ) as fetch:
        assert sync_symbol_financial_reports(db_session, symbol) == 2
        db_session.commit()
        assert sync_symbol_financial_reports(db_session, symbol) == 2
        db_session.commit()

    rows = db_session.execute(
        select(StockFinancialReport).order_by(
            StockFinancialReport.announcement_date
        )
    ).scalars().all()
    assert len(rows) == 2
    assert [row.announcement_date for row in rows] == [
        date(2026, 3, 30),
        date(2026, 4, 15),
    ]
    assert [row.roe_ttm for row in rows] == [18.2, 19.1]
    assert "RAW_MARKER" in (rows[0].raw_json or "")
    assert fetch.call_args.kwargs["symbol"] == "600519.SH"
    assert fetch.call_args.kwargs["indicator"] == "按报告期"
    assert fetch.call_args.kwargs["api_key"] == (
        "stock_financial_analysis_indicator_em"
    )


def test_financial_sync_skips_non_cn_stock_without_network(db_session):
    symbol = Symbol(
        symbol="AAPL",
        name="Apple",
        asset_type="stock",
        market="us",
    )
    db_session.add(symbol)
    db_session.flush()
    with patch(
        "app.services.financial_data.call_akshare_with_retry"
    ) as fetch:
        assert sync_symbol_financial_reports(db_session, symbol) == 0
    fetch.assert_not_called()


def test_financial_sync_route_reports_symbol_and_record_counts(db_session):
    from app.api.routes.external_data import sync_financial_reports

    symbol = Symbol(
        symbol="000001",
        name="平安银行",
        asset_type="stock",
        market="sz",
    )
    db_session.add(symbol)
    db_session.commit()
    with patch(
        "app.services.financial_data.sync_symbol_financial_reports",
        return_value=8,
    ) as sync:
        result = sync_financial_reports(source="all", db=db_session)

    assert result.total == 1
    assert result.success == 1
    assert result.records == 8
    assert result.failed == 0
    sync.assert_called_once_with(db_session, symbol)


@pytest.mark.parametrize(
    ("symbol_text", "market", "expected"),
    [
        ("600519", "sh", "600519.SH"),
        ("000001.SZ", "sz", "000001.SZ"),
        ("830799", "bj", "830799.BJ"),
    ],
)
def test_akshare_financial_symbol(symbol_text, market, expected):
    symbol = Symbol(
        symbol=symbol_text,
        name="test",
        asset_type="stock",
        market=market,
    )
    assert _akshare_financial_symbol(symbol) == expected
