"""ETF fallback verifies basis and bounds; it cannot mutate trading records."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.services import market_data
from app.services.market_data_errors import MarketHistorySourceError
from app.services.market_data_sources.sources import tencent_etf_history as supplier

pytestmark = pytest.mark.whitebox
START, END = date(2026, 10, 21), date(2026, 10, 23)
CODE = SimpleNamespace(symbol='510300', market='sh', asset_type='etf')


def response(adjustment='qfq'):
    fields = [''] * 83
    fields[2], fields[61], fields[82] = '510300', 'ETF', 'CNY'
    return {'code': 0, 'data': {'stockCode': 'sh510300', 'fqtype': adjustment, 'qt': {'fields': fields},
        'nodes': [{'date': day, 'open': '4.2', 'high': '4.3', 'low': '4.1', 'last': '4.25',
                   'volume': '1000', 'amount': '4210', 'exchangeRaw': '1.25'}
                  for day in ['2026-10-16', '2026-10-19', '2026-10-20',
                              '2026-10-21', '2026-10-22', '2026-10-23']]}}


def mock_http(monkeypatch, tmp_path, mutate=None):
    calls = []

    class HTTPResponse:
        status_code = 200
        def __init__(self, body):
            self.content = json.dumps(body).encode()
        def raise_for_status(self):
            pass
        def close(self):
            pass

    def get(session, url, *, params, **kwargs):
        calls.append({'url': url, 'params': params, **kwargs})
        body = response(params['fqtype'])
        if mutate:
            mutate(body, params['fqtype'])
        return HTTPResponse(body)
    monkeypatch.setattr(supplier, 'RECEIPT_ROOT', tmp_path / 'receipts')
    monkeypatch.setattr(supplier.requests.Session, 'get', get)
    monkeypatch.setattr('app.services.akshare_registry.apply_delay', lambda *_: None)
    return calls


def seeded(db):
    etf = Symbol(symbol='510300', name='ETF', market='sh', asset_type='etf')
    stock = Symbol(symbol='000001', name='已完成股票', market='sz', asset_type='stock')
    db.add_all([etf, stock])
    db.flush()
    for day in [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)]:
        db.add(DailyBar(symbol_id=etf.id, trade_date=day, open=4.2, high=4.3, low=4.1, close=4.25,
            volume=1000., amount=4210., source='baostock:cn-etf:v1:qfq'))
    db.add(DailyBar(symbol_id=stock.id, trade_date=END, open=10., high=10., low=10., close=10.,
        volume=500., amount=5000., source='successful-stock'))
    db.commit()
    return etf, stock


def snapshot(db):
    return [(bar.id, bar.symbol_id, bar.trade_date, bar.open, bar.high, bar.low, bar.close,
             bar.volume, bar.amount, bar.source, bar.created_at)
            for bar in db.scalars(select(DailyBar).order_by(DailyBar.id))]


def unavailable(**kwargs):
    raise ConnectionError('primary down')


def test_primary_failure_uses_verified_qfq_backup_and_preserves_prior_rows(db_session, monkeypatch, tmp_path):
    etf, stock = seeded(db_session)
    before = snapshot(db_session)
    calls = mock_http(monkeypatch, tmp_path)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', unavailable)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_sina', lambda **kw: pytest.fail('raw fallback used'))
    result = market_data.sync_symbol_daily_bars(db_session, etf, START, END)
    db_session.commit()
    assert result['inserted'] == 3 and result['updated'] == 0
    assert snapshot(db_session)[:len(before)] == before
    assert [call['params']['fqtype'] for call in calls] == ['qfq', '']
    assert all(call['params']['code'] == 'sh510300' for call in calls)
    assert all(call['timeout'] == (5, 15) and call['allow_redirects'] is False for call in calls)
    bars = list(db_session.scalars(select(DailyBar).where(DailyBar.symbol_id == etf.id, DailyBar.trade_date >= START)))
    assert len(bars) == 3
    assert {(row.volume, row.amount, row.turnover_rate, row.source) for row in bars} == {
        (1000., 4210., 1.25, 'tencent:cn-etf:v1:qfq')}
    evidence = result['history_evidence']
    assert evidence['actual_adjustment'] == 'qfq' and evidence['fallback_used'] is True
    assert evidence['basis_comparison']['reference_count'] == 3
    assert evidence['basis_comparison']['max_absolute_differences']['price'] == 0
    receipt_root = tmp_path / 'receipts' / evidence['receipt_id']
    assert hashlib.sha256((receipt_root / 'qfq.response.json').read_bytes()).hexdigest() == evidence['response_sha256']
    assert hashlib.sha256((receipt_root / 'raw.response.json').read_bytes()).hexdigest() == evidence['raw_response_sha256']


def test_primary_success_never_calls_backup(db_session, monkeypatch):
    etf, _ = seeded(db_session)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', lambda **kw: pd.DataFrame([{
        '日期': '2026-10-23', '开盘': 4., '最高': 4., '最低': 4., '收盘': 4.,
        '成交量': 10., '成交额': 4000., '换手率': 1.}]))
    monkeypatch.setattr(market_data, 'fetch_tencent_etf_history', lambda **kw: pytest.fail('unnecessary backup'))
    monkeypatch.setattr('app.services.akshare_registry.apply_delay', lambda *_: None)
    assert market_data.sync_symbol_daily_bars(db_session, etf, END, END)['inserted'] == 1


@pytest.mark.parametrize('fault', ['wrong_code', 'raw', 'hfq', 'stock', 'currency', 'duplicate', 'nan',
    'negative_volume', 'invalid_ohlc', 'missing_amount', 'wrong_date', 'empty', 'failed'])
def test_malformed_or_wrong_basis_responses_are_rejected(fault):
    body = response()
    data = body['data']
    if fault == 'wrong_code': data['stockCode'] = 'sh510050'
    elif fault in {'raw', 'hfq'}: data['fqtype'] = '' if fault == 'raw' else 'hfq'
    elif fault == 'stock': data['qt']['fields'][61] = 'A'
    elif fault == 'currency': data['qt']['fields'][82] = 'HKD'
    elif fault == 'duplicate': data['nodes'].append(deepcopy(data['nodes'][0]))
    elif fault == 'nan': data['nodes'][-1]['last'] = 'NaN'
    elif fault == 'negative_volume': data['nodes'][-1]['volume'] = '-1'
    elif fault == 'invalid_ohlc': data['nodes'][-1]['high'] = '4.0'
    elif fault == 'missing_amount': del data['nodes'][-1]['amount']
    elif fault == 'wrong_date': data['nodes'][-1]['date'] = '2026-10-24'
    elif fault == 'empty': data['nodes'] = []
    elif fault == 'failed': body['code'] = 1
    with pytest.raises(ValueError, match='tencent_etf_response_unverified'):
        supplier.parse_etf_response(json.dumps(body).encode(), prefixed='sh510300',
            start=date(2026, 10, 7), end=END, adjustment='qfq')


@pytest.mark.parametrize('fault', ['price_anchor', 'volume_unit', 'amount_unit', 'unknown_source', 'missing_reference'])
def test_inconsistent_history_is_not_written(db_session, monkeypatch, tmp_path, fault):
    etf, _ = seeded(db_session)
    row = db_session.scalar(select(DailyBar).where(DailyBar.symbol_id == etf.id).limit(1))
    if fault == 'price_anchor': row.close += .01
    elif fault == 'volume_unit': row.volume /= 100
    elif fault == 'amount_unit': row.amount /= 10000
    elif fault == 'unknown_source': row.source = 'akshare'
    elif fault == 'missing_reference': row.trade_date = date(2026, 9, 1)
    db_session.commit()
    before = snapshot(db_session)
    mock_http(monkeypatch, tmp_path)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', unavailable)
    with pytest.raises(MarketHistorySourceError) as error:
        market_data.sync_symbol_daily_bars(db_session, etf, START, END)
    db_session.rollback()
    assert error.value.code == 'ETF_QFQ_BASIS_UNVERIFIED'
    assert error.value.retryable is False
    assert snapshot(db_session) == before


def test_raw_and_qfq_quantity_mismatch_is_rejected_before_writes(monkeypatch, tmp_path):
    def mutate(body, adjustment):
        if adjustment == '': body['data']['nodes'][-1]['volume'] = '1100'
    mock_http(monkeypatch, tmp_path, mutate)
    with pytest.raises(ValueError, match='dates_or_quantities_differ'):
        supplier.fetch_tencent_etf_history(symbol=CODE, start_date=START, end_date=END, adjust='qfq')


@pytest.mark.parametrize('symbol,adjust,start', [
    (CODE, '', START), (CODE, 'hfq', START),
    (SimpleNamespace(symbol='600000', market='sh', asset_type='etf'), 'qfq', START),
    (SimpleNamespace(symbol='510300', market='sz', asset_type='etf'), 'qfq', START),
    (CODE, 'qfq', date(2020, 1, 1)),
])
def test_invalid_request_cannot_contact_source(monkeypatch, symbol, adjust, start):
    monkeypatch.setattr(supplier.requests.Session, 'get', lambda *a, **kw: pytest.fail('invalid request contacted supplier'))
    with pytest.raises(ValueError):
        supplier.fetch_tencent_etf_history(symbol=symbol, start_date=start, end_date=END, adjust=adjust)


@pytest.mark.parametrize('primary', ['failed', 'empty'])
def test_registered_alternative_chain_uses_qfq_backup(monkeypatch, tmp_path, primary):
    from app.services.market_data_sources.registry import get_chain
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', unavailable if primary == 'failed' else lambda **kwargs: pd.DataFrame())
    mock_http(monkeypatch, tmp_path)
    result = get_chain(CODE).fetch_with_lineage(CODE, START, END, 'qfq')
    assert result.source_name == 'tx_etf' and result.fallback_used is True
    assert result.frame.attrs['history_response']['actual_adjustment'] == 'qfq'
    assert result.frame.volume.tolist() == [1000.] * 3


def test_universe_writer_rejects_a_different_historical_basis(db_session, monkeypatch, tmp_path):
    from app.models.universe import UniverseSymbol, UniverseDailyBar
    from app.services.universe_sync import _upsert_universe_bars
    etf = UniverseSymbol(symbol='510300', name='ETF', market='sh', region='cn', asset_type='etf')
    db_session.add(etf)
    db_session.flush()
    for day in [date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)]:
        db_session.add(UniverseDailyBar(universe_symbol_id=etf.id, trade_date=day,
            open=4.2, high=4.3, low=4.1, close=4.15, volume=1000., amount=4210.,
            source='baostock:cn-etf:v1:qfq'))
    db_session.commit()
    mock_http(monkeypatch, tmp_path)
    frame = supplier.fetch_tencent_etf_history(symbol=CODE, start_date=START, end_date=END, adjust='qfq')
    with pytest.raises(MarketHistorySourceError):
        _upsert_universe_bars(db_session, etf.id, frame)
    db_session.rollback()
    assert len(list(db_session.scalars(select(UniverseDailyBar)))) == 3


def test_disabled_etf_backup_does_not_contact_network(monkeypatch, tmp_path):
    calls = mock_http(monkeypatch, tmp_path)
    monkeypatch.setattr(market_data.ak, 'fund_etf_hist_em', unavailable)
    monkeypatch.setattr('app.services.akshare_registry.is_api_enabled', lambda key: key != 'fund_etf_hist_tx')
    with pytest.raises(MarketHistorySourceError) as error:
        market_data._fetch_history(CODE, START, END, 'qfq')
    assert error.value.retryable is False
    assert calls == []
