"""Independent ETF preparation through HTTP, with source I/O replaced only."""
from uuid import uuid4
import hashlib
import json

import pytest

from tests.test_etf_history_preparation import etf_client, main_etf, public_etf_source, wait_preparation
from tests.test_etf_history_training_contract import etf_dataset

pytestmark = [pytest.mark.whitebox, pytest.mark.reap_workers]
ROOT = '/api/v1/etf-history/training-list'
CODES = tuple(str(510300 + i) for i in range(20))  # Synthetic instruments, isolated test DB only.


def test_training_list_starts_empty_and_keeps_twenty_codes_without_changing_main(etf_client, main_etf):
    members_url = f'/api/v1/portfolios/{main_etf.id}/members'
    positions_url = f'/api/v1/portfolios/{main_etf.id}/positions'
    before_members = etf_client.get(members_url).json()
    before_positions = etf_client.get(positions_url).json()
    response = etf_client.get(ROOT)
    assert response.status_code == 200, response.text
    empty = response.json()
    assert empty['symbols'] == []
    assert empty['minimum_symbols'] == 20
    saved = etf_client.put(ROOT, json={'codes_text': '，'.join(CODES),
        'expected_list_sha256': empty['list_sha256']})
    assert saved.status_code == 200, saved.text
    assert [s['symbol'] for s in saved.json()['symbols']] == list(CODES)
    assert saved.json()['symbol_count'] == 20
    assert saved.json()['max_symbols'] >= 20
    assert etf_client.get(ROOT).json() == saved.json()
    assert etf_client.get(members_url).json() == before_members
    assert etf_client.get(positions_url).json() == before_positions


def save_codes(client, codes=CODES):
    current = client.get(ROOT).json()
    response = client.put(ROOT, json={'codes_text': '\n'.join(codes),
        'expected_list_sha256': current['list_sha256']})
    assert response.status_code == 200, response.text
    return response.json()


def test_list_scope_checks_all_twenty_etfs_on_the_same_dates_without_using_main(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    from app.services import market_history_evidence as evidence
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    saved = save_codes(etf_client)
    dates = {'start_date': '2024-01-02', 'end_date': '2025-01-27'}  # 280 fixture weekdays.
    response = etf_client.get(ROOT + '/scope', params=dates)
    assert response.status_code == 200, response.text
    unknown = response.json()
    assert unknown['list_sha256'] == saved['list_sha256']
    assert unknown['common_trade_date_count'] is None
    assert unknown['training_ready'] is False
    assert all(row['missing_trade_dates'] is None and row['reason'] for row in unknown['symbols'])
    dataset = etf_dataset(tmp_path, monkeypatch, symbols=CODES, day_count=280)
    scope = etf_client.get(ROOT + '/scope', params=dates).json()
    assert len(scope['symbols']) == 20  # main has only one ETF.
    assert scope['expected_trade_date_count'] == 280
    assert scope['common_trade_date_count'] == 280
    assert scope['common_start_date'] == '2024-01-02'
    assert scope['common_end_date'] == '2025-01-27'
    assert scope['dataset_id'] == dataset.dataset_id
    assert scope['training_dataset']['dataset_id'] == dataset.dataset_id
    assert scope['training_dataset']['manifest_sha256'] == dataset.manifest_sha256
    assert (scope['training_dataset']['start_date'], scope['training_dataset']['end_date']) == ('2024-01-02', '2025-01-27')
    assert scope['training_ready'] is True
    assert scope['backtest_ready'] is False
    assert all(row['missing_trade_dates'] == [] and row['row_count'] == 280 for row in scope['symbols'])


def test_twenty_etf_preparation_saves_training_files_without_importing_into_main(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    from app.services import etf_history_preparation as preparation, market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparations')
    monkeypatch.setattr(baostock_source, 'capture_history', public_etf_source())
    save_codes(etf_client)
    params = {'start_date': '2024-01-02', 'end_date': '2024-01-04'}
    main_url = '/api/v1/etf-history/scope'
    main_before = etf_client.get(main_url, params={'portfolio_id': main_etf.id, **params}).json()
    scope = etf_client.get(ROOT + '/scope', params=params).json()
    body = {'request_id': str(uuid4()), **params, 'expected_scope_sha256': scope['scope_sha256']}
    response = etf_client.post(ROOT + '/preparations', json=body)
    assert response.status_code == 202, response.text
    done = wait_preparation(etf_client, response.json()['task']['id'])
    assert done['task']['status'] == 'done', done
    report = done['report']
    assert report['scope_kind'] == 'training_list'
    assert report['prepared'] is True
    assert report['imported'] is False
    assert report['expected_trade_date_count'] == 3
    assert report['common_trade_date_count'] == 3
    assert len(report['symbols']) == 20
    assert report['training']['training_ready'] is False
    assert report['training']['minimum_symbols'] == 20
    assert report['backtest_ready'] is False
    main_after = etf_client.get(main_url, params={'portfolio_id': main_etf.id, **params}).json()
    assert main_after['symbols'][0]['row_count'] == main_before['symbols'][0]['row_count'] == 0
    repeated = etf_client.post(ROOT + '/preparations', json=body)
    assert repeated.json()['task']['id'] == done['task']['id']
    recovered = etf_client.get(ROOT + '/preparations/requests/' + body['request_id']).json()
    assert recovered['task']['id'] == done['task']['id']


def test_later_listing_explains_unavailable_dates_and_does_not_shrink_the_training_list(
    etf_client, tmp_path, monkeypatch,
):
    from app.services import etf_history_preparation as preparation, market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparations')

    def source(request, directory, progress, **kwargs):
        public_etf_source()(request, directory, progress, **kwargs)
        capture = json.loads((directory / 'capture.json').read_bytes())
        for key in ('basic_510300', 'raw_510300', 'qfq_510300'):
            path = directory / (key + '.json')
            obj = json.loads(path.read_bytes())
            if key.startswith('basic'):
                obj['raw']['rows'][0][1] = '2024-01-03'
            else:
                obj['raw']['rows'] = obj['raw']['rows'][1:]
            raw = json.dumps(obj).encode()
            path.write_bytes(raw)
            next(ref for ref in capture['queries'] if ref['key'] == key)['sha256'] = hashlib.sha256(raw).hexdigest()
        (directory / 'capture.json').write_text(json.dumps(capture), encoding='utf-8')

    monkeypatch.setattr(baostock_source, 'capture_history', source)
    save_codes(etf_client)
    dates = {'start_date': '2024-01-02', 'end_date': '2024-01-04'}
    scope = etf_client.get(ROOT + '/scope', params=dates).json()
    response = etf_client.post(ROOT + '/preparations', json={**dates,
        'request_id': str(uuid4()), 'expected_scope_sha256': scope['scope_sha256']})
    assert response.status_code == 202, response.text
    value = wait_preparation(etf_client, response.json()['task']['id'])
    assert value['task']['status'] == 'partial'
    report = value['report']
    assert report['prepared'] is False
    assert len(report['symbols']) == 20
    assert report['expected_trade_date_count'] == 3
    assert report['common_trade_date_count'] == 2
    assert report['common_start_date'] == '2024-01-03'
    assert report['symbols'][0]['missing_trade_dates'] == ['2024-01-02']
    assert '2024-01-03' in report['symbols'][0]['error'] and '上市' in report['symbols'][0]['error']
    assert report['symbols'][1]['missing_trade_dates'] == []
    assert etf_client.get(ROOT).json()['symbol_count'] == 20
    assert etf_client.get('/api/v1/etf-history/datasets').json()['items'] == []


def test_training_entry_uses_the_independent_list_and_cannot_pick_main_history_by_default(
    etf_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_etf_history_training_contract import frozen_etf_set
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    factor_set_id = frozen_etf_set(etf_client)
    etf_dataset(tmp_path, monkeypatch, day_count=300, dataset_id='main-longer-history')
    cohort = etf_dataset(tmp_path, monkeypatch, symbols=CODES, day_count=280, dataset_id='independent-list')
    params = {'factor_set_id': factor_set_id, 'use_training_list': 'true'}
    empty = etf_client.get('/api/v1/etf-history/training-options', params=params)
    assert empty.status_code == 200, empty.text
    assert empty.json()['items'] == []
    assert '清单' in empty.json()['list_message']
    save_codes(etf_client)
    options = etf_client.get('/api/v1/etf-history/training-options', params=params).json()
    assert [item['dataset_id'] for item in options['items']] == [cohort.dataset_id]
    assert options['list_symbol_count'] == 20
    assert options['items'][0]['training_ready'] is True
    assert options['items'][0]['feature_ready'] is True
    assert options['minimum_symbols'] == 20


def test_training_checks_keep_the_prepared_dataset_even_when_the_same_list_has_later_history(
    etf_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_etf_history_training_contract import frozen_etf_set
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    factor_set_id = frozen_etf_set(etf_client)
    prepared = etf_dataset(tmp_path, monkeypatch, symbols=CODES, day_count=280, dataset_id='prepared-range')
    etf_dataset(tmp_path, monkeypatch, symbols=CODES, day_count=300, dataset_id='later-range')
    saved = save_codes(etf_client)
    response = etf_client.get('/api/v1/etf-history/training-options', params={
        'factor_set_id': factor_set_id, 'use_training_list': 'true',
        'dataset_id': prepared.dataset_id, 'manifest_sha256': prepared.manifest_sha256,
        'expected_list_sha256': saved['list_sha256'],
    })
    assert response.status_code == 200, response.text
    assert [item['dataset_id'] for item in response.json()['items']] == ['prepared-range']
    chosen = response.json()['items'][0]
    assert chosen['manifest_sha256'] == prepared.manifest_sha256
    assert (chosen['start_date'], chosen['end_date'], chosen['trade_date_count']) == ('2024-01-02', '2025-01-27', 280)
    assert chosen['training_ready'] is True
    assert chosen['backtest_ready'] is False


@pytest.mark.parametrize('change,reason', [
    ('missing', '找不到本次准备的资料'), ('file_changed', '资料文件已有变化'),
    ('list_changed', '训练清单已修改'), ('wrong_codes', '找不到本次准备的资料'),
    ('incomplete', '资料记录不完整'), ('wrong_entry', '资料记录不完整'),
])
def test_prepared_training_input_is_rejected_without_falling_back_when_its_evidence_changes(
    etf_client, tmp_path, monkeypatch, change, reason,
):
    codes = ('510300',)
    prepared = etf_dataset(tmp_path, monkeypatch, symbols=codes, day_count=2, dataset_id='prepared-range')
    etf_dataset(tmp_path, monkeypatch, symbols=codes, day_count=3, dataset_id='later-range')
    saved = save_codes(etf_client, codes)
    params = {'factor_set_id': 'price-set', 'use_training_list': 'true',
              'dataset_id': prepared.dataset_id, 'manifest_sha256': prepared.manifest_sha256,
              'expected_list_sha256': saved['list_sha256']}
    if change == 'missing':
        (tmp_path / 'datasets' / prepared.dataset_id / 'manifest.json').unlink()
    elif change == 'file_changed':
        path = tmp_path / 'datasets' / prepared.dataset_id / 'manifest.json'
        path.write_bytes(path.read_bytes() + b'\n')
    elif change == 'list_changed':
        save_codes(etf_client, ('159915', '510300'))
    elif change == 'wrong_codes':
        current = save_codes(etf_client, ('159915',))
        params['expected_list_sha256'] = current['list_sha256']
    elif change == 'incomplete':
        del params['manifest_sha256']
    elif change == 'wrong_entry':
        params['use_training_list'] = 'false'
    current_list = etf_client.get(ROOT).json()
    response = etf_client.get('/api/v1/etf-history/training-options', params=params)
    assert response.status_code == 409, response.text
    assert reason in response.json()['detail']
    assert etf_client.get(ROOT).json() == current_list


def test_full_preparation_keeps_calendar_coverage_when_the_selected_end_is_a_weekend(
    etf_client, main_etf, tmp_path, monkeypatch,
):
    import pandas as pd
    import time
    from app.services import etf_history_preparation as preparation, market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparations')

    def source(request, directory, progress, **kwargs):
        public_etf_source()(request, directory, progress, **kwargs)
        days = pd.date_range(request['start_date'], request['end_date'])
        trading = [day.strftime('%Y-%m-%d') for day in days if day.weekday() < 5]
        capture = json.loads((directory / 'capture.json').read_bytes())
        for ref in capture['queries']:
            path = directory / ref['file']
            obj = json.loads(path.read_bytes())
            if ref['key'] == 'calendar':
                obj['raw']['rows'] = [[day.strftime('%Y-%m-%d'), '1' if day.weekday() < 5 else '0'] for day in days]
            elif ref['key'].startswith(('raw_', 'qfq_')):
                template = obj['raw']['rows'][0]
                obj['raw']['rows'] = [[day, *template[1:]] for day in trading]
            raw = json.dumps(obj).encode()
            path.write_bytes(raw)
            ref['sha256'] = hashlib.sha256(raw).hexdigest()
        (directory / 'capture.json').write_text(json.dumps(capture), encoding='utf-8')

    monkeypatch.setattr(baostock_source, 'capture_history', source)
    save_codes(etf_client)
    dates = {'start_date': '2024-01-02', 'end_date': '2025-02-02'}  # Sunday; 284 fixture weekdays.
    scope = etf_client.get(ROOT + '/scope', params=dates).json()
    response = etf_client.post(ROOT + '/preparations', json={**dates, 'request_id': str(uuid4()),
        'expected_scope_sha256': scope['scope_sha256']})
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 45
    from app.services.async_tasks import is_worker_running
    while time.monotonic() < deadline:
        done = etf_client.get(ROOT + '/preparations/' + response.json()['task']['id']).json()
        if done['task']['status'] not in {'queued', 'running'} and not is_worker_running(done['task']['id']):
            break
        time.sleep(.05)
    assert done['task']['status'] == 'done', done
    assert done['report']['training']['training_ready'] is True
    checked = etf_client.get(ROOT + '/scope', params=dates).json()
    assert checked['expected_trade_date_count'] == 284
    assert checked['common_trade_date_count'] == 284
    assert checked['common_end_date'] == '2025-01-31'
    assert checked['training_ready'] is True
    assert checked['backtest_ready'] is False


@pytest.mark.parametrize('text,reason', [
    ('000001', '不是可识别的沪深ETF'), ('501018', '不是可识别的沪深ETF'),
    ('sh.510300', '六位'), ('510300,未填写', '六位'),
    (' '.join(str(510300 + i) for i in range(51)), '最多保存50只'),
])
def test_invalid_or_oversized_list_is_rejected_without_replacing_the_saved_codes(etf_client, text, reason):
    saved = save_codes(etf_client)
    response = etf_client.put(ROOT, json={'codes_text': text, 'expected_list_sha256': saved['list_sha256']})
    assert response.status_code == 409, response.text
    assert reason in response.json()['detail']
    assert etf_client.get(ROOT).json() == saved


def test_stale_list_and_stale_preparation_scope_cannot_overwrite_new_choices(etf_client):
    first = save_codes(etf_client)
    dates = {'start_date': '2024-01-02', 'end_date': '2024-01-04'}
    old_scope = etf_client.get(ROOT + '/scope', params=dates).json()
    latest = save_codes(etf_client, ('159915', *CODES))
    stale_save = etf_client.put(ROOT, json={'codes_text': '510300', 'expected_list_sha256': first['list_sha256']})
    assert stale_save.status_code == 409
    stale_prepare = etf_client.post(ROOT + '/preparations', json={**dates, 'request_id': str(uuid4()),
        'expected_scope_sha256': old_scope['scope_sha256']})
    assert stale_prepare.status_code == 409
    assert '已改变' in stale_prepare.json()['detail']
    assert etf_client.get(ROOT).json() == latest


def test_another_page_cannot_change_the_list_while_its_history_is_being_prepared(
    etf_client, tmp_path, monkeypatch,
):
    from threading import Event
    from app.services import etf_history_preparation as preparation, market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparations')
    entered, release = Event(), Event()

    def source(request, directory, progress, **kwargs):
        entered.set()
        assert release.wait(10), 'Test source was not released'
        public_etf_source()(request, directory, progress, **kwargs)

    monkeypatch.setattr(baostock_source, 'capture_history', source)
    saved = save_codes(etf_client)
    dates = {'start_date': '2024-01-02', 'end_date': '2024-01-04'}
    scope = etf_client.get(ROOT + '/scope', params=dates).json()
    started = etf_client.post(ROOT + '/preparations', json={**dates, 'request_id': str(uuid4()),
        'expected_scope_sha256': scope['scope_sha256']})
    assert started.status_code == 202, started.text
    try:
        assert entered.wait(3)
        changed = etf_client.put(ROOT, json={'codes_text': '159915', 'expected_list_sha256': saved['list_sha256']})
        assert changed.status_code == 409, changed.text
        assert '还在准备' in changed.json()['detail']
        assert etf_client.get(ROOT).json() == saved
    finally:
        release.set()
        wait_preparation(etf_client, started.json()['task']['id'])
    changed = etf_client.put(ROOT, json={'codes_text': '159915', 'expected_list_sha256': saved['list_sha256']})
    assert changed.status_code == 200, changed.text
    assert [row['symbol'] for row in changed.json()['symbols']] == ['159915']


def test_separate_price_files_do_not_make_an_older_short_cohort_ready_for_training(
    etf_client, tmp_path, monkeypatch,
):
    etf_dataset(tmp_path, monkeypatch, symbols=CODES, day_count=3, dataset_id='older-combined-list')
    etf_dataset(tmp_path, monkeypatch, symbols=CODES[:10], day_count=280, dataset_id='first-half')
    etf_dataset(tmp_path, monkeypatch, symbols=CODES[10:], day_count=280, dataset_id='second-half')
    save_codes(etf_client)
    response = etf_client.get(ROOT + '/scope', params={'start_date': '2024-01-02', 'end_date': '2025-01-27'})
    assert response.status_code == 200, response.text
    scope = response.json()
    assert scope['common_trade_date_count'] == 280
    assert scope['training_ready'] is False
    assert '一起核对' in scope['training_message']


@pytest.mark.parametrize('issue,common,reason', [
    ('missing', 2, '缺少1个交易日'), ('failure', None, '连接中断'), ('dividend', 3, '分红'),
])
def test_incomplete_source_or_dividend_evidence_keeps_the_list_blocked_and_explains_why(
    etf_client, tmp_path, monkeypatch, issue, common, reason,
):
    from app.services import etf_history_preparation as preparation, market_history_evidence as evidence
    from app.services.security_status import baostock_source
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    monkeypatch.setattr(preparation, 'ARTIFACT_ROOT', tmp_path / 'preparations')
    monkeypatch.setattr(baostock_source, 'capture_history', public_etf_source(**{issue: True}))
    saved = save_codes(etf_client)
    dates = {'start_date': '2024-01-02', 'end_date': '2024-01-04'}
    scope = etf_client.get(ROOT + '/scope', params=dates).json()
    body = {**dates, 'request_id': str(uuid4()), 'expected_scope_sha256': scope['scope_sha256']}
    started = etf_client.post(ROOT + '/preparations', json=body)
    assert started.status_code == 202, started.text
    value = wait_preparation(etf_client, started.json()['task']['id'])
    report = value['report']
    assert value['task']['status'] in {'failed', 'partial'}
    assert report['prepared'] is False and report['imported'] is False
    assert report['common_trade_date_count'] == common
    assert len(report['symbols']) == 20
    assert all(reason in row['error'] for row in report['symbols'])
    assert report['backtest_ready'] is False
    assert etf_client.get('/api/v1/etf-history/datasets').json()['items'] == []
    assert etf_client.get(ROOT).json() == saved
    # A retry with the same request is still the original attempt, even after editing the list.
    save_codes(etf_client, ('159915',))
    assert etf_client.post(ROOT + '/preparations', json=body).json()['task']['id'] == value['task']['id']
    changed = etf_client.post(ROOT + '/preparations', json={**body, 'end_date': '2024-01-05'})
    assert changed.status_code == 409
