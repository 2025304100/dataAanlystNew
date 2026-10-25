"""Cross-source fallback cannot relabel or overwrite an existing history row."""
from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services import market_data, universe_sync

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("layer", ["daily", "universe"])
@pytest.mark.parametrize("turnover", [None, 99.0])
def test_fallback_cannot_erase_or_relabel_existing_primary_data(db_session, monkeypatch, layer, turnover):
    day = date(2026, 10, 19)
    symbol = (Symbol(symbol="600000", name="source", market="sh", asset_type="stock")
              if layer == "daily" else UniverseSymbol(symbol="600000", name="source", market="sh",
                   asset_type="stock", region="cn", is_synced=1, sync_failed=0))
    db_session.add(symbol); db_session.flush()
    model = DailyBar if layer == "daily" else UniverseDailyBar
    key = "symbol_id" if layer == "daily" else "universe_symbol_id"
    row = model(**{key: symbol.id}, trade_date=day, open=10, high=12, low=9, close=11,
                volume=1000, amount=11000, turnover_rate=2,
                source="akshare:em:cn-stock:v1:qfq", created_at=datetime(2026, 10, 20))
    db_session.add(row); db_session.commit()
    before = tuple(db_session.execute(select(model.__table__)).one())
    frame = pd.DataFrame([dict(trade_date=day, open=10, high=12, low=9, close=10.5,
                               volume=900, amount=10000, turnover_rate=turnover)])
    frame.attrs["history_contract"] = dict(provider="tencent", endpoint="stock_zh_a_hist_tx",
        version="cn-stock-shares-v1", volume_unit="share", amount_unit="CNY", turnover_unit="percentage_point")
    frame.attrs["production_price_mode"] = {"requested_adjustment": "qfq"}
    if layer == "daily":
        monkeypatch.setattr(market_data, "_fetch_history", lambda **_: frame)
        call = lambda: market_data.sync_symbol_daily_bars(db_session, symbol, day, day)
    else:
        monkeypatch.setattr(universe_sync, "_fetch_universe_history", lambda *_: frame)
        call = lambda: universe_sync.repair_one_universe_symbol(db_session, symbol, day, day)
    with pytest.raises(ValueError, match="history_source_conflict"):
        call()
    db_session.rollback()
    assert tuple(db_session.execute(select(model.__table__)).one()) == before
