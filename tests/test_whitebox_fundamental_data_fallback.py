from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.services.fundamental_data import (
    sync_market_valuation_snapshot,
    sync_symbol_valuation,
    sync_symbol_valuation_range,
)


pytestmark = pytest.mark.whitebox


def test_market_valuation_snapshot_fetches_full_market_only_once(db_session, monkeypatch):
    symbols = [
        Symbol(symbol="600000", name="A", asset_type="stock", market="sh", industry="Bank"),
        Symbol(symbol="000001", name="B", asset_type="stock", market="sz", industry="Bank"),
    ]
    db_session.add_all(symbols)
    db_session.flush()
    frame = pd.DataFrame([
        {"代码": "600000", "市盈率-动态": 6.5, "市净率": 0.7, "总市值": 1000, "流通市值": 800},
        {"代码": "000001", "市盈率-动态": 7.5, "市净率": 0.8, "总市值": 2000, "流通市值": 1600},
    ])
    calls = []

    def fake_call(*args, **kwargs):
        calls.append((args, kwargs))
        return frame

    monkeypatch.setattr("app.services.fundamental_data.call_akshare_with_retry", fake_call)

    synced = sync_market_valuation_snapshot(
        db_session, symbols, trade_date=date(2026, 8, 15)
    )
    db_session.commit()

    assert synced == {symbols[0].id, symbols[1].id}
    assert len(calls) == 1
    rows = db_session.query(StockValuation).order_by(StockValuation.symbol_id).all()
    assert len(rows) == 2
    assert rows[0].source == "akshare:stock_zh_a_spot_em"


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


def test_valuation_history_range_writes_only_returned_trade_dates(
    db_session, monkeypatch
):
    symbol = Symbol(
        symbol="600519.SH",
        name="Test",
        asset_type="stock",
        market="cn",
    )
    db_session.add(symbol)
    db_session.flush()
    frame = pd.DataFrame(
        [
            {"TRADE_DATE": "2026-07-10", "PE_TTM": 20.0, "PB": 4.0},
            {"TRADE_DATE": "2026-07-12", "PE_TTM": 21.0, "PB": 4.1},
            {"TRADE_DATE": "2026-07-20", "PE_TTM": 22.0, "PB": 4.2},
        ]
    )
    monkeypatch.setattr(
        "app.services.fundamental_data._fetch_individual_info",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.fundamental_data.call_akshare_with_retry",
        lambda *args, **kwargs: frame,
    )

    written = sync_symbol_valuation_range(
        db_session,
        symbol,
        start_date=date(2026, 7, 9),
        end_date=date(2026, 7, 15),
    )
    db_session.commit()
    repeated = sync_symbol_valuation_range(
        db_session,
        symbol,
        start_date=date(2026, 7, 9),
        end_date=date(2026, 7, 15),
    )
    db_session.commit()

    rows = db_session.query(StockValuation).filter(
        StockValuation.symbol_id == symbol.id
    ).all()
    assert written == 2
    assert repeated == 2
    assert {row.trade_date for row in rows} == {
        date(2026, 7, 10),
        date(2026, 7, 12),
    }
    assert {row.source for row in rows} == {"akshare:stock_value_em"}
