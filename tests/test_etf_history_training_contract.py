"""ETF receipts, training preparation and conservative execution admission.

All generated bars are synthetic fixtures in temporary local directories.
"""
from datetime import date
import hashlib

import pandas as pd
import pytest

from app.services import market_history_evidence as evidence
from app.services.factors.trusted_history_training import build_training_warehouse
from tests.test_turnover_v3_public_warehouse_lifecycle import turnover_client

pytestmark = pytest.mark.whitebox


def etf_dataset(tmp_path, monkeypatch, *, symbols=('510300',), day_count=280, include_raw=True,
                dataset_id='etf-test-history', turnover=''):
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    directory = evidence.TRUSTED_HISTORY_DATASET_ROOT / dataset_id
    days = tuple(pd.bdate_range('2024-01-02', periods=day_count).date)
    references = []
    for index, code in enumerate(symbols):
        market = 'sz' if code.startswith(('0', '1', '3')) else 'sh'
        previous, rows = 4., []
        for day in days:
            close = previous * (1 + .001 * (index % 3 - 1))
            rows.append({'date': str(day), 'code': market + '.' + code,
                'open': str(previous), 'high': str(max(previous, close) * 1.01),
                'low': str(min(previous, close) * .99), 'close': str(close),
                'volume': '1000000', 'amount': str(close * 1000000), 'turn': turnover, 'adjustflag': '2'})
            previous = close
        frame = pd.DataFrame(rows)
        references.append(evidence.capture_baostock_history(
            artifact_root=directory / 'receipts', capture_id=code,
            request=evidence.HistoryRequest(market + code, days[0], days[-1], 'qfq'),
            sdk_version='synthetic-etf-test', raw_frame=frame))
        if include_raw:
            raw = frame.copy()
            raw['adjustflag'] = '3'
            references.append(evidence.capture_baostock_history(
                artifact_root=directory / 'receipts', capture_id=code + '-raw',
                request=evidence.HistoryRequest(market + code, days[0], days[-1], ''),
                sdk_version='synthetic-etf-test', raw_frame=raw))
    evidence.write_trusted_history_dataset(dataset_id=dataset_id, receipts=references,
        cohort_symbols=tuple(sorted(symbols)), calendar=days, plan_sha256='e' * 64)
    return evidence.load_trusted_history_dataset(dataset_id)


def test_training_preparation_keeps_etf_type_and_exchange(tmp_path, monkeypatch):
    dataset = etf_dataset(tmp_path, monkeypatch, symbols=('159915', '510300'))
    warehouse = build_training_warehouse(dataset, 'prepare-etf')
    try:
        with warehouse.connection(read_only=True) as connection:
            instruments = connection.execute(
                'SELECT symbol,asset_type,market FROM raw_asset_universe ORDER BY symbol'
            ).fetchall()
        assert instruments == [('159915', 'etf', 'sz'), ('510300', 'etf', 'sh')]
        assert dataset.identity['asset_type'] == 'etf'
        assert dataset.identity['instruments']['510300'] == {'asset_type': 'etf', 'market': 'sh'}
    finally:
        warehouse.close()


def test_etf_research_data_requires_original_prices_for_adjustment_review(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='trusted_history_etf_raw_prices_required'):
        etf_dataset(tmp_path, monkeypatch, include_raw=False)


@pytest.mark.parametrize('code', ['501018', '508000', '519300', '150001', '160105'])
def test_non_etf_fund_codes_cannot_be_prepared_as_etfs(tmp_path, monkeypatch, code):
    with pytest.raises(ValueError, match='unknown_cn_stock_code_family'):
        etf_dataset(tmp_path, monkeypatch, symbols=(code,), day_count=2)


def dividend_dataset(tmp_path, monkeypatch, *, adjusted=(3.9, 3.9), cash=.1, include_event=True,
                     adjusted_volume=1000):
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    directory = evidence.TRUSTED_HISTORY_DATASET_ROOT / 'etf-dividend'
    document = directory / 'documents' / 'dividend.pdf'
    document.parent.mkdir(parents=True)
    document.write_bytes(b'%PDF-1.4 synthetic dividend evidence fixture')
    days = (date(2024, 1, 2), date(2024, 1, 3))
    references = []
    for label, flag, prices in [('raw', '3', (4., 3.9)), ('qfq', '2', adjusted)]:
        frame = pd.DataFrame([{'date': str(day), 'code': 'sh.510300',
            'open': str(price), 'high': str(price), 'low': str(price), 'close': str(price),
            'volume': str(1000 if label == 'raw' else adjusted_volume),
            'amount': str(raw_price * 1000), 'turn': '', 'adjustflag': flag,
        } for day, price, raw_price in zip(days, prices, (4., 3.9))])
        references.append(evidence.capture_baostock_history(artifact_root=directory / 'receipts',
            capture_id=label, request=evidence.HistoryRequest('sh510300', days[0], days[-1],
                '' if label == 'raw' else 'qfq'), sdk_version='synthetic-etf-test', raw_frame=frame))
    events = [{'ex_date': '2024-01-03', 'cash_per_unit': cash,
        'source_url': 'https://www.sse.com.cn/test/dividend.pdf',
        'source_path': 'documents/dividend.pdf',
        'source_sha256': hashlib.sha256(document.read_bytes()).hexdigest()}] if include_event else []
    evidence.write_trusted_history_dataset(dataset_id='etf-dividend', receipts=references,
        cohort_symbols=('510300',), calendar=days, plan_sha256='d' * 64,
        etf_cash_dividends={'510300': events})
    return evidence.load_trusted_history_dataset('etf-dividend')


def test_etf_dividend_review_preserves_quantities_and_explains_adjusted_return(tmp_path, monkeypatch):
    dataset = dividend_dataset(tmp_path, monkeypatch)
    assert dataset.bars.close.tolist() == [3.9, 3.9]
    review = dataset.identity['etf_price_review']['510300']
    assert review['cash_dividends_checked'] == 1
    assert review['price_basis'] == 'qfq'
    assert review['return_basis'] == 'adjusted_price_return'
    assert review['cash_dividend_accounting_verified'] is False
    assert dataset.identity['point_in_time_certified'] is False


@pytest.mark.parametrize('overrides,error', [
    ({'include_event': False}, 'dividend_adjustment_mismatch'),
    ({'cash': .2}, 'dividend_adjustment_mismatch'),
    ({'adjusted': (4., 3.9)}, 'dividend_adjustment_mismatch'),
    ({'adjusted_volume': 10}, 'adjusted_quantities_changed'),
])
def test_etf_dividend_review_rejects_wrong_cash_raw_prices_and_scaled_volume(
    tmp_path, monkeypatch, overrides, error,
):
    with pytest.raises(ValueError, match=error):
        dividend_dataset(tmp_path, monkeypatch, **overrides)


def test_changed_dividend_document_invalidates_prepared_training_data(tmp_path, monkeypatch):
    dataset = dividend_dataset(tmp_path, monkeypatch)
    (dataset.directory / 'documents/dividend.pdf').write_bytes(b'%PDF changed fixture')
    with pytest.raises(ValueError, match='dividend_evidence_invalid'):
        evidence.load_trusted_history_dataset(dataset.dataset_id)


def frozen_etf_set(client, *, formula='close / ref(close, 5) - 1',
                   mapping=None, missing_policy='exclude'):
    created = client.post('/api/v1/factors', json={
        'code': 'etf_price_test', 'name': 'ETF价格测试', 'category': 'momentum',
        'asset_scope': ['cn-etf'], 'default_missing_policy': missing_policy,
    })
    assert created.status_code == 201, created.text
    version = client.post('/api/v1/factors/etf_price_test/versions', json={
        'formula_expr': formula, 'direction': 'higher_better',
        'source_mapping': mapping or {'table': 'raw_daily_bars', 'field': 'close'},
        'postprocess': {'winsorize': {'method': 'mad', 'mad_multiplier': 3.},
                        'zscore': {'ddof': 0}, 'missing_policy': missing_policy},
    })
    assert version.status_code == 201, version.text
    created_set = client.post('/api/v1/factor-sets', json={'name': 'ETF测试集合'})
    assert created_set.status_code == 200, created_set.text
    set_id = created_set.json()['id']
    member = client.post(f'/api/v1/factor-sets/{set_id}/members', json={
        'factor_code': 'etf_price_test', 'factor_version': version.json()['version'],
        'role': 'feature', 'missing_policy': missing_policy,
    })
    assert member.status_code == 200, member.text
    frozen = client.post(f'/api/v1/factor-sets/{set_id}/freeze', json={})
    assert frozen.status_code == 200, frozen.text
    return set_id


def test_public_etf_research_training_records_etf_without_activation(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    client = turnover_client
    set_id = frozen_etf_set(client)
    dataset = etf_dataset(tmp_path, monkeypatch,
        symbols=tuple(str(510300 + index) for index in range(24)))
    before = client.get('/api/v1/factor-models/runtime').json()
    submitted = client.post('/api/v1/scoring/tasks', json={
        'trusted_history_dataset_id': dataset.dataset_id, 'train_model': True,
        'materialize_scores': False, 'factor_set_id': set_id,
        'factor_scope': {'factor_set_id': set_id, 'asset_type': 'etf'},
        'hyperparameters': {'factor_set_id': set_id},
    })
    assert submitted.status_code == 200, submitted.text
    task = client.get('/api/v1/scoring/tasks/' + submitted.json()['id']).json()
    assert task['status'] == 'done', task
    detail = client.get('/api/v1/factor-models/' + task['result']['model']['model_run_id']).json()
    assert detail['asset_type'] == 'etf'
    assert detail['production_eligible'] is False
    assert detail['hyperparameters']['trusted_history_dataset']['asset_type'] == 'etf'
    assert task['result']['trained_model_auto_activated'] is False
    activated = client.post('/api/v1/factor-models/' + detail['id'] + '/activate',
        json={'mode': 'ridge', 'actor': 'isolated_etf_test'})
    assert activated.status_code in (400, 409), activated.text
    assert client.get('/api/v1/factor-models/runtime').json() == before


def test_etf_training_rejects_stock_financial_features_before_creating_a_task(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    client = turnover_client
    set_id = frozen_etf_set(client, formula='1 / pe_ttm',
        mapping={'table': 'raw_valuation_snapshots', 'field': 'pe_ttm'})
    dataset = etf_dataset(tmp_path, monkeypatch,
        symbols=tuple(str(510300 + index) for index in range(24)))
    before = client.get('/api/v1/scoring/tasks').json()
    submitted = client.post('/api/v1/scoring/tasks', json={
        'trusted_history_dataset_id': dataset.dataset_id, 'train_model': True,
        'materialize_scores': False, 'factor_set_id': set_id,
    })
    assert submitted.status_code == 400, submitted.text
    assert submitted.json()['detail']['error_code'] == 'ETF_TRAINING_FEATURE_UNSUPPORTED'
    assert '历史价格和成交量' in submitted.json()['detail']['detail_zh']
    assert client.get('/api/v1/scoring/tasks').json() == before


def test_single_etf_is_not_misrepresented_as_a_cross_sectional_training_pool(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    client = turnover_client
    set_id = frozen_etf_set(client)
    dataset = etf_dataset(tmp_path, monkeypatch)
    submitted = client.post('/api/v1/scoring/tasks', json={
        'trusted_history_dataset_id': dataset.dataset_id, 'train_model': True,
        'materialize_scores': False, 'factor_set_id': set_id,
    })
    assert submitted.status_code == 400, submitted.text
    detail = submitted.json()['detail']
    assert detail['error_code'] == 'ETF_TRAINING_POOL_TOO_SMALL'
    assert '至少20只ETF' in detail['detail_zh']


def test_composed_etf_history_cannot_lose_its_type_and_bypass_dividend_review(tmp_path, monkeypatch):
    from app.schemas.async_task import FactorPipelineCreate
    from app.schemas.errors import FactorSevenError
    from app.services.factors.trusted_history_training import load_training_dataset
    from app.services.market_history_composition import write_composed_history
    first = etf_dataset(tmp_path, monkeypatch, dataset_id='etf-first', turnover='1')
    second = etf_dataset(tmp_path, monkeypatch, dataset_id='etf-second', turnover='1')
    composed = write_composed_history(dataset_id='etf-composed',
        references=[{'dataset_id': item.dataset_id, 'manifest_sha256': item.manifest_sha256}
                    for item in (first, second)],
        symbols=('510300',), plan_id='a' * 64, plan_sha256='b' * 64)
    with pytest.raises(FactorSevenError) as rejected:
        load_training_dataset(FactorPipelineCreate(trusted_history_dataset_id=composed.dataset_id,
            materialize_scores=False), freeze=True)
    assert rejected.value.error_code == 'ETF_HISTORY_REVIEW_REQUIRED'
