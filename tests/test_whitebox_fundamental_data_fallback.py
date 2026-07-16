from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from app.models.symbol import Symbol
from app.services.fundamental_data import sync_symbol_valuation


pytestmark = pytest.mark.whitebox


def test_valuation_sync_falls_back_to_stock_value_history(db_session):
    symbol = Symbol(
        symbol="600519.SH",
        name="贵州茅台",
        asset_type="stock",
        market="sh",
    )
    db_session.add(symbol)
    db_session.flush()
    frame = pd.DataFrame(
        [
            {
                "数据日期": "2026-07-15",
                "PE(TTM)": 18.9,
                "市净率": 5.7,
                "总市值": 1_500_000,
                "流通市值": 1_400_000,
            }
        ]
    )
    with patch(
        "app.services.fundamental_data._fetch_spot_valuation",
        return_value={},
    ), patch(
        "app.services.fundamental_data._fetch_individual_info",
        return_value={},
    ), patch(
        "app.services.fundamental_data.call_akshare_with_retry",
        return_value=frame,
    ) as fetch:
        row = sync_symbol_valuation(
            db_session, symbol, date(2026, 7, 15)
        )

    assert row is not None
    assert row.trade_date == date(2026, 7, 15)
    assert row.pe_ttm == pytest.approx(18.9)
    assert row.pb == pytest.approx(5.7)
    assert row.source == "akshare:stock_value_em"
    assert fetch.call_args.kwargs["api_key"] == "stock_value_em"
