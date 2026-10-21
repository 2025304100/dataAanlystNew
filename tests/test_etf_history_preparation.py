"""ETF preparation through the HTTP boundary; external source only is replaced."""
from datetime import date
import hashlib
import json
import time
from uuid import uuid4
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.portfolio import Portfolio
from app.models.portfolio_member import PortfolioMember
from app.models.symbol import Symbol
from tests.test_etf_history_training_contract import etf_dataset

pytestmark = [pytest.mark.whitebox, pytest.mark.reap_workers]


@pytest.fixture
def etf_client(db_session):
    from app.api.router import api_router
    from app.db.session import get_db
    application = FastAPI()
    application.include_router(api_router)
    application.dependency_overrides[get_db] = lambda: db_session
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def main_etf(db_session):
    portfolio = Portfolio(name='main', account_type='simulated', total_capital=100000,
                          investable_ratio=1, cash_reserve_ratio=0)
    db_session.add(portfolio)
    db_session.flush()
    for code, kind, market in [('510300', 'etf', 'SH'), ('000001', 'stock', 'SZ')]:
        symbol = Symbol(symbol=code, name=code, asset_type=kind, market=market)
        db_session.add(symbol)
        db_session.flush()
        db_session.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbol.id, status='active'))
    db_session.commit()
    return portfolio


def test_etf_page_scope_finds_reviewed_history_and_explains_the_existing_training_limit(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    dataset = etf_dataset(tmp_path, monkeypatch)
    response = etf_client.get('/api/v1/etf-history/scope', params={'portfolio_id': main_etf.id,
        'start_date': '2024-01-02', 'end_date': '2024-01-04'})
    assert response.status_code == 200, response.text
    scope = response.json()
    assert [row['symbol'] for row in scope['symbols']] == ['510300']
    assert scope['symbols'][0]['missing_trade_dates'] == ['2024-01-02', '2024-01-03', '2024-01-04']
    assert scope['symbols'][0]['dataset_id'] == dataset.dataset_id
    assert scope['minimum_symbols'] == 20
    assert scope['training_ready'] is False
    assert '1只ETF' in scope['training_message'] and '20只' in scope['training_message']
    assert scope['backtest_ready'] is False


def public_etf_source(*, missing=False, failure=False, dividend=False):
    def capture(request, directory, progress, *, timeout_seconds=180):
        from app.services.security_status.baostock_source import SourceDownloadError
        if failure:
            raise SourceDownloadError('历史资料来源连接中断，请稍后重试。')
        queries = []

        def save(key, endpoint, parameters, fields, rows):
            value = {'endpoint': endpoint, 'parameters': parameters, 'fetched_at': '2024-01-05T08:00:00+00:00',
                     'raw': {'fields': fields, 'rows': rows, 'error_code': '0', 'error_msg': 'success'}}
            data = json.dumps(value).encode()
            (directory / (key + '.json')).write_bytes(data)
            queries.append({'key': key, 'file': key + '.json', 'sha256': hashlib.sha256(data).hexdigest()})
            (directory / 'capture.json').write_text(json.dumps({'queries': queries, 'complete': False}), encoding='utf-8')
            progress(len(queries), key)

        dates = {'start_date': request['start_date'], 'end_date': request['end_date']}
        save('calendar', 'query_trade_dates', dates, ['calendar_date', 'is_trading_day'],
             [['2024-01-02', '1'], ['2024-01-03', '1'], ['2024-01-04', '1']])
        for symbol in request['symbols']:
            code = symbol['provider_code']
            save('basic_' + symbol['symbol'], 'query_stock_basic', {'code': code},
                 ['code', 'ipoDate', 'outDate', 'type'], [[code, '2012-05-28', '', '5']])
            for key, flag in [('raw', '3'), ('qfq', '2')]:
                rows = []
                for day in ['2024-01-02', '2024-01-03', '2024-01-04']:
                    if missing and day == '2024-01-03':
                        continue
                    price = '3.9' if dividend and (key == 'qfq' or day != '2024-01-02') else '4'
                    amount = '3900' if dividend and day != '2024-01-02' else '4000'
                    rows.append([day, code, price, price, price, price, '1000', amount, flag, ''])
                save(key + '_' + symbol['symbol'], 'query_history_k_data_plus',
                     {'code': code, **dates, 'frequency': 'd', 'adjustflag': flag},
                     ['date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount', 'adjustflag', 'turn'], rows)
        (directory / 'capture.json').write_text(json.dumps({'sdk_version': 'synthetic-public-api', 'queries': queries, 'complete': True}), encoding='utf-8')
    return capture


def wait_preparation(client, task_id):
    from app.services.async_tasks import is_worker_running
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get('/api/v1/etf-history/preparations/' + task_id)
        assert response.status_code == 200, response.text
        value = response.json()
        if value['task']['status'] not in {'queued', 'running'} and not is_worker_running(task_id):
            return value
        time.sleep(.03)
    pytest.fail('ETF preparation did not finish')


def test_missing_dates_are_visible_and_retry_completes_only_after_full_price_checks(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    from app.services import etf_history_preparation as preparation
    from app.services import market_history_evidence as evidence
    from app.services.security_status import baostock_source
    etf_dataset(tmp_path, monkeypatch, day_count=3)
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparation')
    monkeypatch.setattr(baostock_source, 'capture_history', public_etf_source(missing=True))
    scope = etf_client.get('/api/v1/etf-history/scope', params={'portfolio_id': main_etf.id}).json()
    body = {'portfolio_id': main_etf.id, 'request_id': str(uuid4()), 'start_date': '2024-01-02',
            'end_date': '2024-01-04', 'expected_scope_sha256': scope['scope_sha256']}
    response = etf_client.post('/api/v1/etf-history/preparations', json=body)
    assert response.status_code == 202, response.text
    first = wait_preparation(etf_client, response.json()['task']['id'])
    assert first['task']['status'] == 'partial'
    assert first['report']['symbols'][0]['missing_trade_dates'] == ['2024-01-03']
    assert first['report']['imported'] is False
    assert '缺少' in first['report']['symbols'][0]['error']
    monkeypatch.setattr(baostock_source, 'capture_history', public_etf_source())
    retry = etf_client.post('/api/v1/etf-history/preparations', json={**body, 'request_id': str(uuid4())})
    assert retry.status_code == 202, retry.text
    done = wait_preparation(etf_client, retry.json()['task']['id'])
    assert done['task']['status'] == 'done', done
    assert done['report']['imported'] is True
    assert done['report']['symbols'][0]['missing_trade_dates'] == []
    assert done['report']['backtest_ready'] is False
    assert done['report']['backup_sha256']
    updated = etf_client.get('/api/v1/etf-history/scope', params={'portfolio_id': main_etf.id,
        'start_date': '2024-01-02', 'end_date': '2024-01-04'}).json()
    assert updated['symbols'][0]['row_count'] == 3
    assert updated['symbols'][0]['missing_trade_dates'] == []
    assert updated['symbols'][0]['dataset_id'] == done['report']['dataset_id']
    replay = etf_client.post('/api/v1/etf-history/preparations', json=body)
    assert replay.json()['task']['id'] == first['task']['id']
    assert replay.json()['task']['status'] == 'partial'


def test_training_page_checks_features_and_reports_twenty_etf_requirement_before_submission(
    etf_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_etf_history_training_contract import frozen_etf_set
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    dataset = etf_dataset(tmp_path, monkeypatch)
    factor_set_id = frozen_etf_set(etf_client)
    response = etf_client.get('/api/v1/etf-history/training-options', params={'factor_set_id': factor_set_id})
    assert response.status_code == 200, response.text
    options = response.json()
    assert options['factor_ready'] is True
    item = next(row for row in options['items'] if row['dataset_id'] == dataset.dataset_id)
    assert item['feature_ready'] is True
    assert item['training_ready'] is False
    assert item['minimum_symbols'] == 20
    assert '20只ETF' in item['training_message']


def test_source_disconnect_is_a_failure_with_unknown_missing_dates_not_an_empty_success(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    from app.services import etf_history_preparation as preparation
    from app.services import market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparation')
    monkeypatch.setattr(baostock_source, 'capture_history', public_etf_source(failure=True))
    scope = etf_client.get('/api/v1/etf-history/scope', params={'portfolio_id': main_etf.id}).json()
    submitted = etf_client.post('/api/v1/etf-history/preparations', json={
        'portfolio_id': main_etf.id, 'request_id': str(uuid4()), 'start_date': '2024-01-02',
        'end_date': '2024-01-04', 'expected_scope_sha256': scope['scope_sha256']})
    value = wait_preparation(etf_client, submitted.json()['task']['id'])
    assert value['task']['status'] == 'failed'
    assert value['report']['imported'] is False
    assert value['report']['symbols'][0]['missing_trade_dates'] is None
    assert value['report']['symbols'][0]['symbol'] == '510300'
    assert '连接中断' in value['report']['errors'][0]


def test_new_dividend_without_original_disclosure_does_not_import_or_claim_price_review_passed(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    from app.services import etf_history_preparation as preparation
    from app.services import market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparation')
    monkeypatch.setattr(baostock_source, 'capture_history', public_etf_source(dividend=True))
    scope = etf_client.get('/api/v1/etf-history/scope', params={'portfolio_id': main_etf.id}).json()
    submitted = etf_client.post('/api/v1/etf-history/preparations', json={
        'portfolio_id': main_etf.id, 'request_id': str(uuid4()), 'start_date': '2024-01-02',
        'end_date': '2024-01-04', 'expected_scope_sha256': scope['scope_sha256']})
    value = wait_preparation(etf_client, submitted.json()['task']['id'])
    assert value['report']['imported'] is False
    assert value['report']['symbols'][0]['missing_trade_dates'] == [], value
    assert '分红' in value['report']['symbols'][0]['error']
    assert etf_client.get('/api/v1/etf-history/datasets').json()['items'] == []
    assert etf_client.get('/api/v1/etf-history/scope', params={'portfolio_id': main_etf.id}).json()['symbols'][0]['row_count'] == 0
