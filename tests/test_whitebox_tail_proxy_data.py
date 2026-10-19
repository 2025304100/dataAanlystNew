"""Historical date resolution remains readable after retiring collection."""
from datetime import date
import pytest
from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.services.tail_proxy_data import resolve_tail_proxy_trade_date

pytestmark = pytest.mark.whitebox


def test_tail_proxy_automatic_date_uses_latest_local_trading_session(db_session):
    """Weekend/holiday jobs must request the latest local trading date."""
    symbol = Symbol(symbol="600519", name="贵州茅台", asset_type="stock", market="sh")
    db_session.add(symbol)
    db_session.flush()
    db_session.add(
        DailyBar(
            symbol_id=symbol.id,
            trade_date=date(2026, 8, 21),
            open=10,
            high=11,
            low=9,
            close=10.5,
            volume=100,
            amount=1000,
            source="test",
        )
    )
    db_session.commit()
    assert resolve_tail_proxy_trade_date(db_session) == date(2026, 8, 21)
