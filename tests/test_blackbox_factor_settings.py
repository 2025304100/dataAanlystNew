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
            # 本地 skip；CI（REQUIRE_LIVE_BACKEND=1）下直接失败，不让零覆盖变成绿。
            from tests._live_backend_guard import skip_or_fail_no_live_backend
            
            skip_or_fail_no_live_backend(exc)
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
    """开关关闭时不得建任务，且必须给出可机读的结构化错误契约。

    断言为什么不再是 `json()['detail']`：后端有全局异常处理器，会把
    `HTTPException(409, detail=str)` 重写成统一契约（`error_code` / `user_message` /
    `retryable` / `next_actions` …），响应里根本**没有 `detail` 键**。原断言只在
    “开关恰好是关”的环境下才执行（见下面的 skip），而 CI 就红在这里：
    `KeyError: 'detail'`。现在按真契约断，同时多校了 error_code 与 retryable，
    比原来更严（而不是放宽）。

    已知覆盖盲区（没在本改里解决）：开关开着时这用例在本地 skip，等于“只有 CI
    跑”；想靠 PUT /factors/config 主动把开关关掉再测来消除它，但那个接口自己
    在 CI 上另有一条未修的红（PUT enabled → 409 DB_INTEGRITY_VIOLATION），
    在那修好前驱动状态会把本用例变成另一种不确定。等那条修完再补。
    """
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
    payload = response.json()
    # 统一错误契约的必备字段（缺任何一个就是契约退化，不是“文案变了”）
    assert payload.get('error_code'), f'409 响应缺少 error_code：{payload}'
    assert payload.get('retryable') is True, f'开关关闭应可重试（打开开关后重跑）：{payload}'
    assert 'disabled' in str(payload.get('user_message', '')).lower(), (
        f'用户文案应说明是开关关闭导致的拦截：{payload}'
    )
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
