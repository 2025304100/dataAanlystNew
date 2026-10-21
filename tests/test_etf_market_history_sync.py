"""Public ETF history sync retains a single explicit qfq price basis."""
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.services import market_data

pytestmark = pytest.mark.whitebox


def test_etf_qfq_sync_does_not_replace_prices_with_an_unadjusted_fallback(db_session, monkeypatch):
    symbol = Symbol(symbol='510300', name='ETF', asset_type='etf', market='sh')
    db_session.add(symbol)
    db_session.commit()

    def unavailable(**kwargs):
        raise ConnectionError('Synthetic external provider unavailable')

    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', unavailable)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_sina', lambda **kwargs: pd.DataFrame([{
        'date': '2024-01-03', 'open': 3.9, 'high': 3.9, 'low': 3.9,
        'close': 3.9, 'volume': 1000, 'amount': 3900,
    }]))
    monkeypatch.setattr('app.services.akshare_registry.apply_delay', lambda *args: None)
    with pytest.raises(ValueError, match='ETF前复权行情获取失败'):
        market_data.sync_symbol_daily_bars(db_session, symbol, date(2024, 1, 3), date(2024, 1, 3))
    assert db_session.scalars(select(DailyBar).where(DailyBar.symbol_id == symbol.id)).all() == []


def test_etf_sync_stores_fund_units_and_an_explicit_qfq_source(db_session, monkeypatch):
    symbol = Symbol(symbol='510300', name='ETF', asset_type='etf', market='sh')
    db_session.add(symbol)
    db_session.commit()
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', lambda **kwargs: pd.DataFrame([{
        '日期': '2024-01-03', '开盘': 4., '最高': 4., '最低': 4., '收盘': 4.,
        '成交量': 10., '成交额': 4000., '换手率': 1.,
    }]))
    monkeypatch.setattr('app.services.akshare_registry.apply_delay', lambda *args: None)
    result = market_data.sync_symbol_daily_bars(db_session, symbol, date(2024, 1, 3), date(2024, 1, 3))
    assert result['inserted'] == 1
    bar = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == symbol.id))
    assert bar.volume == 1000.
    assert bar.amount == 4000.
    assert bar.source == 'akshare:em:cn-etf:v1:qfq'


def test_alternative_etf_data_entry_uses_the_same_fund_units(monkeypatch):
    from types import SimpleNamespace
    from app.services.market_data_sources.chain import SourceChain
    from app.services.market_data_sources.sources.akshare_source import EMETFSource
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', lambda **kwargs: pd.DataFrame([{
        '日期': '2024-01-03', '开盘': 4., '最高': 4., '最低': 4., '收盘': 4.,
        '成交量': 10., '成交额': 4000., '换手率': 1.,
    }]))
    monkeypatch.setattr('app.services.akshare_registry.apply_delay', lambda *args: None)
    frame = SourceChain([EMETFSource()]).fetch(
        SimpleNamespace(symbol='510300', asset_type='etf', market='sh'),
        date(2024, 1, 3), date(2024, 1, 3), 'qfq')
    assert frame.volume.tolist() == [1000.]
    assert frame.attrs['history_contract']['version'] == 'cn-etf-shares-v1'


def test_alternative_etf_entry_also_refuses_a_raw_fallback_for_qfq(monkeypatch):
    from types import SimpleNamespace
    from app.services.market_data_sources.chain import SourceChain
    from app.services.market_data_sources.sources.akshare_source import EMETFSource, SinaETFSource

    def unavailable(**kwargs):
        raise ConnectionError('Synthetic external provider unavailable')

    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', unavailable)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_sina', lambda **kwargs: pd.DataFrame([{
        'date': '2024-01-03', 'open': 3.9, 'high': 3.9, 'low': 3.9,
        'close': 3.9, 'volume': 1000, 'amount': 3900,
    }]))
    monkeypatch.setattr('app.services.akshare_registry.apply_delay', lambda *args: None)
    with pytest.raises(RuntimeError, match='前复权'):
        SourceChain([EMETFSource(), SinaETFSource()]).fetch(
            SimpleNamespace(symbol='510300', asset_type='etf', market='sh'),
            date(2024, 1, 3), date(2024, 1, 3), 'qfq')


def test_reviewed_etf_history_import_replaces_only_the_requested_instrument(
    db_session, tmp_path, monkeypatch,
):
    from datetime import datetime
    from tests.test_etf_history_training_contract import dividend_dataset
    from app.models.universe import UniverseSymbol, UniverseDailyBar
    dataset = dividend_dataset(tmp_path, monkeypatch)
    etf = Symbol(symbol='510300', name='ETF', asset_type='etf', market='sh')
    stock = Symbol(symbol='600000', name='stock', asset_type='stock', market='sh')
    db_session.add_all([etf, stock])
    universe = UniverseSymbol(symbol='510300', name='ETF', asset_type='etf', market='sh', region='cn')
    db_session.add(universe)
    db_session.flush()
    db_session.add_all([
        DailyBar(symbol_id=etf.id, trade_date=date(2024, 1, 2), open=4., high=4., low=4., close=4.,
                 volume=10., amount=4000., source='akshare', created_at=datetime(2024, 1, 2)),
        DailyBar(symbol_id=stock.id, trade_date=date(2024, 1, 2), open=10., high=10., low=10., close=10.,
                 volume=100., amount=1000., source='keep-stock'),
    ])
    db_session.commit()
    result = market_data.import_verified_etf_history(db_session, etf, dataset.dataset_id)
    assert result['inserted'] == 1 and result['updated'] == 1
    bars = db_session.scalars(select(DailyBar).where(DailyBar.symbol_id == etf.id)
                             .order_by(DailyBar.trade_date)).all()
    assert [(bar.close, bar.volume, bar.amount, bar.source) for bar in bars] == [
        (3.9, 1000., 4000., 'baostock:cn-etf:v1:qfq'),
        (3.9, 1000., 3900., 'baostock:cn-etf:v1:qfq'),
    ]
    assert bars[0].created_at > datetime(2024, 1, 2)
    assert db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == stock.id)).source == 'keep-stock'
    mirrored = db_session.scalars(select(UniverseDailyBar).where(UniverseDailyBar.universe_symbol_id == universe.id)
                                 .order_by(UniverseDailyBar.trade_date)).all()
    assert [(row.close, row.volume, row.source) for row in mirrored] == [
        (3.9, 1000., 'baostock:cn-etf:v1:qfq'), (3.9, 1000., 'baostock:cn-etf:v1:qfq')]
    assert universe.bar_count == 2 and universe.last_bar_date == date(2024, 1, 3)


@pytest.mark.parametrize('adjust', ['', 'hfq'])
def test_etf_business_sync_rejects_other_price_bases_before_database_or_network(adjust):
    from types import SimpleNamespace

    class NoIO:
        def execute(self, *args, **kwargs):
            pytest.fail('Rejected price mode queried the database')

    with pytest.raises(ValueError, match='cn_etf_production_requires_qfq'):
        market_data.sync_symbol_daily_bars(NoIO(),
            SimpleNamespace(symbol='510300', asset_type='etf', market='sh'),
            date(2024, 1, 3), date(2024, 1, 3), adjust)
