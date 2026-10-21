"""ETF default features through public APIs; temporary stores, no model fitting."""
import pytest
from concurrent.futures import ThreadPoolExecutor

from tests.test_etf_training_request_recovery import etf_training_api

pytestmark = pytest.mark.whitebox


@pytest.fixture(autouse=True)
def include_etf_data_reads(etf_training_api):
    from app.api.routes.etf_history import router
    etf_training_api.app.include_router(router, prefix='/api/v1')


def test_prepares_one_reusable_etf_set_with_four_verified_price_volume_versions(etf_training_api):
    client = etf_training_api
    models = client.get('/api/v1/factor-models').json()
    tasks = client.get('/api/v1/scoring/tasks').json()
    response = client.post('/api/v1/scoring/etf-training/factor-set', json={})
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared['name'] == 'ETF价量特征'
    assert prepared['status'] == 'frozen' and prepared['asset_type'] == 'etf'
    assert len(prepared['members']) == 4
    assert all(item['role'] == 'feature' and item['missing_policy'] == 'exclude'
               and item['version_trainable'] is True for item in prepared['members'])
    definitions = {}
    for member in prepared['members']:
        factor = client.get('/api/v1/factors/' + member['factor_code']).json()
        assert factor['asset_scope'] == ['cn-etf']
        assert factor['active_version_id'] is None and factor['is_active'] == 0
        versions = client.get('/api/v1/factors/' + member['factor_code'] + '/versions').json()
        version = next(item for item in versions if item['version'] == member['factor_version'])
        definitions[factor['name']] = version['formula_expr']
        assert version['validation_status'] == 'valid'
        assert version['postprocess']['missing_policy'] == 'exclude'
        assert version['source_mapping']['table'] == 'raw_daily_bars'
    assert definitions == {
        'ETF5日涨幅': 'close / ref(close, 5) - 1',
        'ETF20日均价': 'iif(count(close - close, 20) == 20, mean(close, 20), None)',
        'ETF20日成交量均值': 'iif(count(volume - volume, 20) == 20, mean(volume, 20), None)',
        'ETF20日收益波动': 'iif(count(close / prev_close - 1, 20) == 20, stddev(close / prev_close - 1, 20), None)',
    }
    repeated = client.post('/api/v1/scoring/etf-training/factor-set', json={})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == prepared
    visible = client.get('/api/v1/factor-sets/' + prepared['id']).json()
    assert [item['factor_version_id'] for item in visible['members']] == [item['factor_version_id'] for item in prepared['members']]
    assert client.get('/api/v1/factor-models').json() == models
    assert client.get('/api/v1/scoring/tasks').json() == tasks


def test_default_etf_set_is_identified_in_both_collection_list_and_detail(etf_training_api):
    client = etf_training_api
    prepared = client.post('/api/v1/scoring/etf-training/factor-set', json={}).json()
    collections = client.get('/api/v1/scoring/factorsets?scope=frozen').json()
    found = next(item for item in collections if item['id'] == prepared['id'])
    assert found['asset_type'] == 'etf'
    assert found['schema_version'] >= 3
    assert client.get('/api/v1/factor-sets/' + prepared['id']).json()['asset_type'] == 'etf'


def test_preparing_concurrently_reuses_the_same_versions_without_starting_training(etf_training_api):
    client = etf_training_api
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post('/api/v1/scoring/etf-training/factor-set', json={}), range(4)))
    assert [item.status_code for item in responses] == [200] * 4, [item.text for item in responses]
    assert all(item.json() == responses[0].json() for item in responses)
    assert len(client.get('/api/v1/factor-sets').json()) == 1
    for member in responses[0].json()['members']:
        assert len(client.get('/api/v1/factors/' + member['factor_code'] + '/versions').json()) == 1
    assert client.get('/api/v1/factor-models').json()['items'] == []
    assert client.get('/api/v1/scoring/tasks').json() == []


def test_conflicting_existing_code_rolls_back_all_new_factors_and_collection(etf_training_api):
    client = etf_training_api
    existing = client.post('/api/v1/factors', json={'code': 'etf_return_volatility_20d',
        'name': '原有自定义特征', 'category': 'volatility', 'asset_scope': ['cn-stock']})
    assert existing.status_code == 201, existing.text
    before = client.get('/api/v1/factors').json()
    rejected = client.post('/api/v1/scoring/etf-training/factor-set', json={})
    assert rejected.status_code == 409, rejected.text
    assert client.get('/api/v1/factors').json() == before
    assert client.get('/api/v1/factor-sets').json() == []


def test_preparation_does_not_replace_a_frozen_version_when_a_newer_version_exists(etf_training_api):
    client = etf_training_api
    prepared = client.post('/api/v1/scoring/etf-training/factor-set', json={}).json()
    code = prepared['members'][0]['factor_code']
    created = client.post('/api/v1/factors/' + code + '/versions', json={'formula_expr': 'close / ref(close, 10) - 1'})
    assert created.status_code == 201, created.text
    before = client.get('/api/v1/factors/' + code + '/versions').json()
    repeated = client.post('/api/v1/scoring/etf-training/factor-set', json={})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == prepared
    assert client.get('/api/v1/factors/' + code + '/versions').json() == before


def test_prepared_features_pass_etf_checks_but_do_not_lower_the_quantity_gate(etf_training_api, tmp_path, monkeypatch):
    from tests.test_etf_history_training_contract import etf_dataset
    client = etf_training_api
    dataset = etf_dataset(tmp_path, monkeypatch)
    prepared = client.post('/api/v1/scoring/etf-training/factor-set', json={}).json()
    options = client.get('/api/v1/etf-history/training-options', params={'factor_set_id': prepared['id']})
    assert options.status_code == 200, options.text
    item = next(row for row in options.json()['items'] if row['dataset_id'] == dataset.dataset_id)
    assert item['feature_ready'] is True and item['factor_ready'] is True
    assert item['training_ready'] is False and item['minimum_symbols'] == 20
    assert client.get('/api/v1/factor-models').json()['items'] == []
    assert client.get('/api/v1/scoring/tasks').json() == []


def test_registered_formulas_keep_known_values_and_warmup_missing_data(etf_training_api, tmp_path):
    """Use the public training-panel calculator, without fitting a model."""
    import pandas as pd
    from datetime import datetime
    from app.services.factors.store import FactorWarehouse
    from app.services.factors.factor_compiler import FactorCompiler
    from app.services.factors.factor_executor import FactorExecutor
    client = etf_training_api
    prepared = client.post('/api/v1/scoring/etf-training/factor-set', json={}).json()
    days = tuple(pd.bdate_range('2024-01-02', periods=25).date)
    rows = [{'symbol': '510300', 'trade_date': day, 'open': float(index + 1), 'high': float(index + 1),
             'low': float(index + 1), 'close': float(index + 1), 'volume': float((index + 1) * 100),
             'amount': float((index + 1) ** 2 * 100), 'adjust': 'qfq', 'source': 'synthetic-etf-formula',
             'source_origin': 'synthetic-etf-formula', 'source_row_id': index + 1,
             'ingested_at': datetime(2024, 2, 10), 'batch_id': 'formula-check'}
            for index, day in enumerate(days)]
    warehouse = FactorWarehouse(tmp_path / 'formula-check.duckdb')
    warehouse.initialize()
    try:
        warehouse.upsert_daily_bars(rows, source_key='synthetic-etf-formula', watermark=25)
        warehouse.upsert_records('raw_asset_universe', [{'symbol': '510300', 'name': '测试ETF',
            'asset_type': 'etf', 'region': 'cn', 'market': 'sh', 'is_active': True,
            'source': 'synthetic-etf-formula', 'source_row_id': 1, 'updated_at': datetime(2024, 2, 10)}])
        expected = {'etf_return_5d': (5, .25), 'etf_mean_close_20d': (19, 15.5),
                    'etf_mean_volume_20d': (19, 1550.), 'etf_return_volatility_20d': (20, .043164423593087003)}
        volume_plan = None
        for member in prepared['members']:
            code = member['factor_code']
            versions = client.get('/api/v1/factors/' + code + '/versions').json()
            version = next(item for item in versions if item['version'] == member['factor_version'])
            result = FactorCompiler().compile(formula=version['formula_expr'], params=version['params'],
                direction=version['direction'], postprocess=version['postprocess'])
            assert result.success, result.errors
            panel = FactorExecutor(warehouse).execute_panel(result.execution_plan, start_date=days[0], end_date=days[-1], symbols=['510300'])
            assert not panel.read_errors, panel.read_errors
            values = panel.factors_long.sort_values('trade_date').raw_value
            warmup, final = expected[code]
            assert len(values) == 25
            assert values.iloc[:warmup].isna().all()
            assert values.iloc[warmup:].notna().all()
            assert values.iloc[-1] == pytest.approx(final, rel=1e-12)
            if code == 'etf_mean_volume_20d':
                volume_plan = result.execution_plan
        rows[10]['volume'] = None
        warehouse.upsert_daily_bars(rows, source_key='synthetic-etf-formula', watermark=26)
        with_gap = FactorExecutor(warehouse).execute_panel(volume_plan, start_date=days[0], end_date=days[-1], symbols=['510300'])
        assert with_gap.factors_long.raw_value.isna().all(), 'Missing volume must not become zero or a partial mean'
    finally:
        warehouse.close()
    assert client.get('/api/v1/factor-models').json()['items'] == []


def test_disabled_factor_feature_and_extra_training_arguments_cannot_create_a_set(etf_training_api):
    client = etf_training_api
    extra = client.post('/api/v1/scoring/etf-training/factor-set', json={'train_model': True})
    assert extra.status_code == 422
    disabled = client.post('/api/v1/scoring/system/config', json={'feature_enabled': False})
    assert disabled.status_code == 200, disabled.text
    rejected = client.post('/api/v1/scoring/etf-training/factor-set', json={})
    assert rejected.status_code == 409, rejected.text
    assert client.get('/api/v1/factor-sets').json() == []
    assert client.get('/api/v1/factor-models').json()['items'] == []
