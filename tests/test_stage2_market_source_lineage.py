"""Stage-2 market-source lineage contracts.

The source chain is a public data boundary: a successful fallback must remain
visible to callers instead of being collapsed into a generic source label.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from app.models.symbol import Symbol
from app.services.market_data_sources.chain import SourceChain


def _source(name: str, *, unavailable: bool = False) -> MagicMock:
    source = MagicMock()
    source.name = name
    source.available.return_value = True
    source.supports.return_value = True
    if unavailable:
        from app.services.market_data_sources.sources.base import SourceUnavailable

        source.fetch.side_effect = SourceUnavailable(f"{name} unavailable")
    else:
        source.fetch.return_value = pd.DataFrame([{"close": 10.0}])
    return source


def test_source_chain_reports_the_backup_that_supplied_data():
    primary = _source("primary", unavailable=True)
    backup = _source("backup")

    result = SourceChain([primary, backup]).fetch_with_lineage(
        SimpleNamespace(symbol="600000"),
        date(2026, 1, 1),
        date(2026, 1, 2),
        "qfq",
    )

    assert result.source_name == "backup"
    assert result.fallback_used is True
    assert result.attempted_sources == ("primary", "backup")
    assert result.frame.iloc[0]["close"] == 10.0


def test_daily_bar_gateway_preserves_precise_chain_source(db_session, monkeypatch):
    """The L4 gateway seam returns the concrete source selected by the chain."""
    from app.services import external_data_gateway as gateway
    from app.services.market_data_sources.chain import SourceFetchResult

    symbol = Symbol(symbol="600002", name="lineage", asset_type="stock", market="SH")
    db_session.add(symbol)
    db_session.commit()

    @contextmanager
    def _session_scope():
        yield db_session

    class _LineageChain:
        def fetch_with_lineage(self, *_args, **_kwargs):
            return SourceFetchResult(
                frame=pd.DataFrame([{"close": 11.0}]),
                source_name="sina_stock",
                attempted_sources=("em_stock", "sina_stock"),
                fallback_used=True,
            )

    monkeypatch.setattr(gateway, "get_session_local", lambda: _session_scope)
    gateway.register_source_chain("akshare.daily_bars", _LineageChain())
    try:
        frame, source_detail = gateway._default_l4_daily_bars({
            "symbol": "600002",
            "start": "2026-01-01",
            "end": "2026-01-02",
        })
    finally:
        gateway.unregister_source_chain("akshare.daily_bars")

    assert frame.iloc[0]["close"] == 11.0
    assert source_detail == "sina_stock"
