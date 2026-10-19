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


def test_daily_valuation_fetches_selected_symbols_only_once(db_session, monkeypatch):
    monkeypatch.setattr("app.services.market_session.is_cn_trading_day", lambda db, day: True)
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

    def fake_call(db, codes, start, end, **kwargs):
        calls.append((codes, start, end))
        return [{"symbol": str(row["代码"]), "trade_date": start,
                 "pe_ttm": row["市盈率-动态"], "pb": row["市净率"],
                 "total_market_cap": row["总市值"], "circulating_market_cap": row["流通市值"]}
                for _, row in frame.iterrows()]

    monkeypatch.setattr("app.services.valuation_provider.fetch_valuation_rows", fake_call)

    synced = sync_market_valuation_snapshot(
        db_session, symbols, trade_date=date.today()
    )
    db_session.commit()

    assert synced == {symbols[0].id, symbols[1].id}
    assert len(calls) == 1
    rows = db_session.query(StockValuation).order_by(StockValuation.symbol_id).all()
    assert len(rows) == 2
    assert rows[0].source == "eastmoney:valuation_daily"
    assert calls[0] == (["600000", "000001"], date.today(), date.today())


def test_valuation_sync_uses_bounded_history_for_requested_day(db_session):
    symbol = Symbol(symbol="600519.SH", name="test", asset_type="stock", market="sh")
    db_session.add(symbol); db_session.flush()
    with patch("app.services.valuation_provider.fetch_valuation_rows", return_value=[{
        "symbol": "600519", "trade_date": date(2026, 7, 15), "pe_ttm": 18.9, "pb": 5.7,
        "total_market_cap": 1500000, "circulating_market_cap": 1400000,
    }]) as fetch:
        row = sync_symbol_valuation(db_session, symbol, date(2026, 7, 15))
    assert row.trade_date == date(2026, 7, 15) and row.pe_ttm == 18.9
    assert fetch.call_args.args[1:] == (["600519"], date(2026, 7, 15), date(2026, 7, 15))




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
            {"TRADE_DATE": "2026-07-13", "PE_TTM": 21.0, "PB": 4.1},
        ]
    )
    monkeypatch.setattr(
        "app.services.fundamental_data._fetch_individual_info",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.valuation_provider.fetch_valuation_rows",
        lambda *args, **kwargs: [{"symbol": "600519", "trade_date": date.fromisoformat(row["TRADE_DATE"]),
            "pe_ttm": row["PE_TTM"], "pb": row["PB"], "total_market_cap": None,
            "circulating_market_cap": None} for _, row in frame.iterrows()],
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
        date(2026, 7, 13),
    }
    assert {row.source for row in rows} == {"akshare:stock_value_em"}
