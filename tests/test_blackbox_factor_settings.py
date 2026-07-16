'''HTTP black-box coverage for the factor settings readiness contract.'''
from __future__ import annotations

import os

import httpx
import pytest


pytestmark = pytest.mark.blackbox
BASE = os.getenv('BLACKBOX_BASE_URL', 'http://localhost:8000')


@pytest.fixture(scope='module')
def client():
    with httpx.Client(base_url=BASE, timeout=15.0, trust_env=False) as value:
        try:
            response = value.get('/health')
            response.raise_for_status()
        except Exception as exc:
            pytest.skip(f'backend is not running: {exc}')
        yield value


def test_factor_overview_exposes_readiness_contract(client):
    response = client.get('/api/v1/factors/overview')
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload['feature_enabled'], bool)
    assert payload['config']['feature_enabled'] == payload['feature_enabled']
    assert isinstance(payload['config']['warehouse_path'], str)
    assert 'warehouse_error' in payload
    assert isinstance(payload['health']['warehouse_available'], bool)


def test_disabled_factor_pipeline_returns_409_without_creating_task(client):
    overview = client.get('/api/v1/factors/overview').json()
    if overview['feature_enabled']:
        pytest.skip('factor feature is enabled in this environment')
    before = client.get('/api/v1/factor-pipeline/tasks?limit=20').json()
    response = client.post(
        '/api/v1/factor-pipeline/tasks',
        json={
            'full_refresh': False,
            'train_model': True,
            'materialize_scores': True,
            'window_days': 250,
            'validation_days': 50,
        },
    )
    after = client.get('/api/v1/factor-pipeline/tasks?limit=20').json()
    assert response.status_code == 409
    assert 'disabled' in response.json()['detail'].lower()
    assert [item['id'] for item in after] == [item['id'] for item in before]


def test_invalid_training_windows_are_rejected_before_dispatch(client):
    response = client.post(
        '/api/v1/factor-pipeline/tasks',
        json={
            'window_days': 60,
            'validation_days': 60,
        },
    )
    assert response.status_code == 422
    assert 'validation_days' in response.text
