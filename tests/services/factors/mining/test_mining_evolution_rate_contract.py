"""Invalid evolution rates are rejected before a task or write lock is created."""
import pytest

from test_m1_closure import mining_env
from test_factor_mining_routes import _client

pytestmark = pytest.mark.whitebox


def _payload(rates):
    return dict(candidate_pool_snapshot_id='not-created-for-rate-validation',
                start_date='2025-01-01T00:00:00', end_date='2026-01-01T00:00:00',
                data_cutoff_at='2026-02-01T00:00:00', rebalance_frequency='daily',
                evolution_params={'adaptive': False, 'ai_enabled': False, **rates})


def test_invalid_rate_sum_is_rejected_before_snapshot_or_task_creation(mining_env):
    client = _client(mining_env)
    response = client.post('/factor-mining/runs', json=_payload(
        dict(mutation_rate=.60, crossover_rate=.25, random_rate=.20)))
    assert response.status_code == 422, response.text
    assert client.get('/factor-mining/runs').json()['total'] == 0
    assert client.get('/factor-mining/locks/status').json()['miningDomain']['busy'] is False


@pytest.mark.parametrize('rates', [
    {'mutation_rate': 0}, {'crossover_rate': 0}, {'random_rate': 0},
    {'mutation_rate': .9, 'crossover_rate': .05, 'random_rate': .05},
    {'mutation_rate': .4, 'crossover_rate': .55, 'random_rate': .05},
    {'mutation_rate': .4, 'crossover_rate': .3, 'random_rate': .3},
])
def test_out_of_bounds_rates_are_rejected_instead_of_changed(mining_env, rates):
    response = _client(mining_env).post('/factor-mining/runs', json=_payload(rates))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize('rates', [
    {}, {'mutation_rate': None, 'crossover_rate': None, 'random_rate': None},
    {'mutation_rate': .6, 'crossover_rate': .25, 'random_rate': .15},
])
def test_valid_and_default_rates_proceed_to_the_snapshot_check(mining_env, rates):
    response = _client(mining_env).post('/factor-mining/runs', json=_payload(rates))
    assert response.status_code == 404, response.text
    assert response.json()['detail']['error_code'] == 'MINING_SNAPSHOT_NOT_FOUND'
