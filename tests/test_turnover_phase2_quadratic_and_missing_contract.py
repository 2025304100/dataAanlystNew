"""Public panel contracts for raw TS-Z20 squared and missing-safe normalization.

All bars live in pytest's isolated temporary DuckDB. No live model, source table,
published scores or active configuration is touched.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import FactorExecutor
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


CONTRACT = json.loads((Path(__file__).resolve().parents[1] /
    'tests/fixtures/turnover/turnover-phase2-quadratic-contract-2026-10-16.json').read_text(encoding='utf-8'))
DAYS = [day.date() for day in pd.bdate_range('2026-01-05', periods=20)]
NOW = datetime(2026, 10, 16)


@pytest.fixture
def warehouse(tmp_path):
    result = FactorWarehouse(tmp_path / 'turnover-phase2-isolated.duckdb')
    result.initialize()
    return result


def _plan(formula=None, *, basis='turnover_z20_squared', missing_policy='exclude'):
    postprocess = deepcopy(CONTRACT['postprocess'])
    postprocess['missing_policy'] = missing_policy
    compiled = compile_formula(
        formula=formula or CONTRACT['basis'][basis]['formula_expr'], params={},
        direction=CONTRACT['direction'], postprocess=postprocess, strict_fields=True,
    )
    assert compiled.success, [error.to_dict() for error in compiled.errors]
    return compiled.execution_plan


def _seed(warehouse, symbol, values, *, days=None):
    days = DAYS if days is None else days
    rows = [{
        'symbol': symbol, 'trade_date': day, 'adjust': 'qfq',
        'open': 10.0, 'high': 11.0, 'low': 9.0, 'close': 10.0,
        'volume': 1000.0, 'amount': 10000.0, 'turnover_rate': value,
        'source': 'phase2-isolated-contract', 'source_origin': 'test',
        'source_row_id': index, 'source_updated_at': NOW,
        'ingested_at': NOW, 'batch_id': 'phase2-isolated-contract',
    } for index, (day, value) in enumerate(zip(days, values))]
    warehouse.upsert_daily_bars(rows, source_key='phase2-isolated-contract', watermark=len(rows))


def _panel(warehouse, plan=None, *, day=None):
    day = DAYS[-1] if day is None else day
    outcome = FactorExecutor(warehouse).execute_panel(
        plan or _plan(), start_date=day, end_date=day,
    )
    assert outcome.read_errors == []
    return outcome.factors_long.set_index('symbol').sort_index()


@pytest.mark.parametrize('values', [
    [None, None, None], [None, 2.0, None], [2.0, 2.0, None],
], ids=['all_missing', 'single_valid', 'constant_with_missing'])
@pytest.mark.parametrize('policy', ['exclude', 'impute_zero'])
def test_public_panel_preserves_missing_in_degenerate_cross_sections(warehouse, values, policy):
    for index, value in enumerate(values):
        _seed(warehouse, f'symbol-{index}', [value], days=[DAYS[-1]])
    panel = _panel(warehouse, _plan('turnover_rate', missing_policy=policy))
    if policy == 'impute_zero':
        np.testing.assert_allclose(panel.normalized_value.to_numpy(dtype=float), [0.0] * 3)
        np.testing.assert_allclose(panel.winsorized_value.to_numpy(dtype=float),
                                   [0.0 if value is None else value for value in values])
    else:
        expected_missing = np.asarray([value is None for value in values])
        np.testing.assert_array_equal(panel.normalized_value.isna().to_numpy(), expected_missing)
        np.testing.assert_array_equal(panel.winsorized_value.isna().to_numpy(), expected_missing)
        assert panel.loc[~expected_missing, 'normalized_value'].eq(0.0).all()
    # Preview is a separate public entry point and must have identical policy.
    preview = FactorExecutor(warehouse).preview(
        _plan('turnover_rate', missing_policy=policy), trade_date=DAYS[-1], limit=10)
    assert preview.errors == []
    for row in preview.values:
        value = panel.loc[row['symbol'], 'normalized_value']
        assert row['normalized_value'] is None if pd.isna(value) else row['normalized_value'] == value


def test_quadratic_basis_compiles_under_original_limits_and_locked_hash():
    plan = _plan().to_dict()
    locked = CONTRACT['basis']['turnover_z20_squared']
    for field in ('execution_plan_hash', 'compiler_version', 'ast_depth',
                  'function_call_count', 'node_count', 'max_lookback'):
        assert plan[field] == locked[field]
    assert plan['ast_depth'] <= 6
    assert plan['function_call_count'] <= 12
    assert plan['data_dependencies']['fields'] == ['turnover_rate']
    assert plan['postprocess'] == CONTRACT['postprocess']


@pytest.mark.parametrize('values,expected', [
    ([1.0] * 19 + [21.0], 19.0),
    ([2.0] * 19 + [0.0], 19.0),
    ([1.0] * 19 + [-10.0], 19.0),
    ([float(i) for i in range(1, 21)], 2.7142857142857144),
    ([2.0] * 20, 0.0),
    ([1e-12 * i for i in range(1, 21)], 0.0),
], ids=['high_tail', 'zero_current_low_tail', 'same_v3_finite_negative_domain',
        'graded_window', 'zero_variance', 'same_v3_near_zero_guard'])
def test_squared_raw_basis_matches_hand_values_and_original_v3_before_postprocess(
    warehouse, values, expected,
):
    _seed(warehouse, 'worked', values)
    squared = _panel(warehouse).loc['worked', 'raw_value']
    original = _panel(warehouse, _plan(basis='turnover_z20_v3')).loc['worked', 'raw_value']
    assert squared == pytest.approx(expected, abs=1e-12)
    assert squared == pytest.approx(original ** 2, abs=1e-12)


@pytest.mark.parametrize('index', [0, 19], ids=['historical', 'current'])
@pytest.mark.parametrize('invalid', [None, float('nan'), float('inf'), float('-inf')],
                         ids=['missing', 'nan', 'positive_infinity', 'negative_infinity'])
def test_nonfinite_twenty_bar_window_stays_missing_after_normalization(warehouse, index, invalid):
    values = [1.0] * 19 + [21.0]
    values[index] = invalid
    _seed(warehouse, 'invalid', values)
    row = _panel(warehouse).loc['invalid']
    assert pd.isna(row.raw_value)
    assert pd.isna(row.normalized_value)


def test_nineteen_observations_cannot_become_a_zero_normalized_training_sample(warehouse):
    _seed(warehouse, 'too-new', [1.0] * 18 + [21.0], days=DAYS[1:])
    row = _panel(warehouse).loc['too-new']
    assert pd.isna(row.raw_value)
    assert pd.isna(row.normalized_value)


@pytest.mark.parametrize('scale', [0.01, 100.0])
def test_positive_unit_scaling_preserves_nondegenerate_raw_and_normalized_values(warehouse, scale):
    history = [float(value) for value in range(1, 20)]
    currents = [0.0, 3.0, 8.0, 21.0, 70.0]
    for index, current in enumerate(currents):
        _seed(warehouse, str(index), history + [current])
    before = _panel(warehouse)
    for index, current in enumerate(currents):
        _seed(warehouse, str(index), [value * scale for value in history] + [current * scale])
    after = _panel(warehouse)
    np.testing.assert_allclose(before[['raw_value', 'normalized_value']].to_numpy(dtype=float),
                               after[['raw_value', 'normalized_value']].to_numpy(dtype=float),
                               rtol=1e-12, atol=1e-12)


def test_future_and_new_symbols_do_not_change_past_quadratic_panel(warehouse):
    _seed(warehouse, 'existing-high', [1.0] * 19 + [21.0])
    _seed(warehouse, 'existing-low', [2.0] * 19 + [0.0])
    before = _panel(warehouse)
    future_days = [day.date() for day in pd.bdate_range('2026-02-02', periods=30)]
    _seed(warehouse, 'existing-high', [1e6] * 29 + [1e9], days=future_days)
    _seed(warehouse, 'future-only', [1e6] * 29 + [1e12], days=future_days)
    after = _panel(warehouse)
    pd.testing.assert_frame_equal(before, after)


def test_sparse_real_observation_warmup_preserves_raw_z20_square_contract(warehouse):
    days = [day.date() for day in pd.bdate_range('2025-01-01', periods=120)]
    _seed(warehouse, 'clock', [2.0] * 120, days=days)
    sparse = [days[index] for index in range(0, 95, 5)] + [days[-1]]
    _seed(warehouse, 'sparse', [1.0] * 19 + [21.0], days=sparse)
    row = _panel(warehouse, day=days[-1]).loc['sparse']
    assert row.raw_value == pytest.approx(19.0)
