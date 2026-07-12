from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.api.routes.dashboard import get_symbol_detail_panel
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.bar_queries import load_forward_bars_map, load_recent_bars


pytestmark = pytest.mark.whitebox


def _seed_universe_only_symbol(db_session, code: str = "588200"):
    symbol = Symbol(
        symbol=code,
        name="Universe ETF",
        asset_type="etf",
        market="sh",
        board="main",
        is_active=1,
    )
    universe_symbol = UniverseSymbol(
        symbol=code,
        name="Universe ETF",
        asset_type="etf",
        market="sh",
        region="cn",
        board="main",
        is_synced=1,
        bar_count=80,
    )
    db_session.add_all([symbol, universe_symbol])
    db_session.flush()

    first_day = date(2026, 1, 1)
    for offset in range(80):
        close = 4.0 + offset * 0.01
        db_session.add(
            UniverseDailyBar(
                universe_symbol_id=universe_symbol.id,
                trade_date=first_day + timedelta(days=offset),
                open=close - 0.02,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                volume=1_000_000 + offset,
            )
        )
    db_session.commit()
    return symbol, first_day


def test_symbol_detail_builds_setup_from_universe_bars(db_session):
    symbol, first_day = _seed_universe_only_symbol(db_session)
    portfolio = Portfolio(
        name="Universe fallback",
        account_type="sim",
        total_capital=100_000,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
        is_default=1,
    )
    score = Score(
        symbol_id=symbol.id,
        trade_date=first_day + timedelta(days=79),
        quality_score=78,
        quality_grade="B",
        timing_score=72,
        stage="start",
        action="open",
        priority_score=75,
        calc_batch_id="universe-fallback",
    )
    db_session.add_all([portfolio, score])
    db_session.commit()

    detail = get_symbol_detail_panel(
        symbol_id=symbol.id,
        portfolio_id=portfolio.id,
        sample_limit=None,
        bar_limit=60,
        db=db_session,
    )

    assert len(detail.bars) == 60
    assert detail.bars[-1].close == pytest.approx(4.79)
    assert detail.latest_trade_setup is not None
    assert detail.latest_trade_setup["entry_min"] is not None


def test_forward_bar_map_falls_back_to_universe_bars(db_session):
    symbol, first_day = _seed_universe_only_symbol(db_session, code="588201")

    bars = load_recent_bars(db_session, symbol.id, limit=20)
    forward_map = load_forward_bars_map(
        db_session,
        {symbol.id: first_day + timedelta(days=40)},
    )

    assert len(bars) == 20
    assert len(forward_map[symbol.id]) == 40
    assert forward_map[symbol.id][0].trade_date == first_day + timedelta(days=40)


def test_symbol_detail_without_bars_returns_partial_detail(db_session):
    symbol = Symbol(
        symbol="NO_BARS",
        name="No bars",
        asset_type="stock",
        market="sh",
        board="main",
        is_active=1,
    )
    portfolio = Portfolio(
        name="No bars fallback",
        account_type="sim",
        total_capital=100_000,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
        is_default=0,
    )
    db_session.add_all([symbol, portfolio])
    db_session.flush()
    db_session.add(
        Score(
            symbol_id=symbol.id,
            trade_date=date(2026, 1, 1),
            quality_score=60,
            quality_grade="C",
            timing_score=55,
            stage="start",
            action="watch",
            priority_score=58,
            calc_batch_id="no-bars",
        )
    )
    db_session.commit()

    detail = get_symbol_detail_panel(
        symbol_id=symbol.id,
        portfolio_id=portfolio.id,
        sample_limit=None,
        bar_limit=60,
        db=db_session,
    )

    assert detail.bars == []
    assert detail.latest_trade_setup is None
    assert detail.latest_score is not None
