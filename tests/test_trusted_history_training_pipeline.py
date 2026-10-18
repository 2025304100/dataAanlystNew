"""Real isolated research pipeline, with verified synthetic provider receipts.

The temporary lifecycle fixture's deliberately learnable data/configuration is
used only to exercise a successful numerical fit. Production gate defaults are
never changed by the trusted-dataset path.
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import event, select

from tests.test_turnover_v3_public_warehouse_lifecycle import turnover_client, FORMULA

pytestmark = pytest.mark.whitebox


def _dataset(tmp_path, monkeypatch, *, day_count=310, incomplete_day=None, illiquid_day=None):
    import app.services.market_history_evidence as evidence
    monkeypatch.setattr(evidence, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path / 'datasets')
    dataset_id = 'isolated-trusted-history'
    directory = evidence.TRUSTED_HISTORY_DATASET_ROOT / dataset_id
    directory.mkdir(parents=True)
    days = tuple(pd.bdate_range('2024-01-02', periods=day_count).date)
    references = []
    symbols = tuple(f'{index + 1:06d}' for index in range(24))
    for index, symbol in enumerate(symbols):
        slope = index % 3 - 1
        previous, rows = 10., []
        for n, day in enumerate(days):
            close = previous * (1 + .001 * slope)
            if n == incomplete_day and index < 4:
                previous = close
                continue
            rows.append({'date': str(day), 'code': 'sz.' + symbol,
                'open': str(previous), 'high': str(max(previous, close) * 1.01),
                'low': str(min(previous, close) * .99), 'close': str(close),
                'volume': '0' if n == illiquid_day and index < 20 else '1000000',
                'amount': '0' if n == illiquid_day and index < 20 else str(close * 1000000),
                'turn': str(10 + slope * .02 * (n + 1)), 'adjustflag': '2'})
            previous = close
        reference = evidence.capture_baostock_history(artifact_root=directory / 'receipts',
            capture_id=symbol, request=evidence.HistoryRequest('sz' + symbol, days[0], days[-1], 'qfq'),
            sdk_version='synthetic-test-1', raw_frame=pd.DataFrame(rows))
        references.append(reference)
    evidence.write_trusted_history_dataset(dataset_id=dataset_id, receipts=references,
        cohort_symbols=symbols, calendar=days, plan_sha256='a' * 64)
    return dataset_id, days, directory


@pytest.mark.parametrize('dataset_id', ['../other', 'C:/escape', 'a/b', '', 'A' * 81])
def test_dataset_id_cannot_be_a_path(dataset_id):
    from app.schemas.async_task import FactorPipelineCreate
    with pytest.raises(ValidationError):
        FactorPipelineCreate(trusted_history_dataset_id=dataset_id, materialize_scores=False)


@pytest.mark.parametrize('overrides', [
    {'train_model': False}, {'materialize_scores': True}, {'mirror_only': True},
    {'universe_source': 'all'},
])
def test_dataset_training_cannot_publish_or_enter_mirror_scoring_modes(overrides):
    from app.schemas.async_task import FactorPipelineCreate
    values = {'trusted_history_dataset_id': 'valid-id', 'train_model': True, 'materialize_scores': False}
    with pytest.raises(ValidationError):
        FactorPipelineCreate(**{**values, **overrides})


@pytest.mark.parametrize('last_day', [date(2030, 1, 1), date(2030, 1, 2)])
def test_unclosed_or_future_dataset_tail_cannot_supply_future_labels(monkeypatch, last_day):
    from app.schemas.async_task import FactorPipelineCreate
    from app.services.factors import trusted_history_training as training
    from app.services import market_history_evidence as evidence

    class BeforeMarketClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, 6, tzinfo=timezone.utc)

    monkeypatch.setattr(training, 'datetime', BeforeMarketClose)
    monkeypatch.setattr(evidence, 'load_trusted_history_dataset', lambda _: SimpleNamespace(calendar=(last_day,)))
    payload = FactorPipelineCreate(trusted_history_dataset_id='future-input', materialize_scores=False)
    with pytest.raises(ValueError, match='dataset_end_not_closed'):
        training.load_training_dataset(payload, freeze=True)


def test_manifest_is_pinned_and_date_window_cannot_be_reselected(tmp_path, monkeypatch):
    from app.schemas.async_task import FactorPipelineCreate
    from app.services.factors.trusted_history_training import load_training_dataset
    dataset_id, days, directory = _dataset(tmp_path, monkeypatch)
    payload = FactorPipelineCreate(trusted_history_dataset_id=dataset_id, materialize_scores=False)
    dataset = load_training_dataset(payload, freeze=True)
    assert payload.start_date == days[0] and payload.data_cutoff_date == days[-1]
    assert payload.hyperparameters_json['trusted_history_dataset'] == dataset.identity
    assert load_training_dataset(payload).manifest_sha256 == dataset.manifest_sha256
    payload.start_date = days[1]
    with pytest.raises(ValueError, match='date_range_conflict'):
        load_training_dataset(payload)
    payload.start_date = days[0]
    payload.window_days = 120
    with pytest.raises(ValueError, match='fixed_250_50'):
        load_training_dataset(payload)
    payload.window_days = 250
    with (directory / 'manifest.json').open('a', encoding='utf-8') as handle:
        handle.write('\n')
    with pytest.raises(ValueError, match='changed_after_dispatch'):
        load_training_dataset(payload)


def test_factor_and_training_admission_keep_the_frozen_cohort_denominator(tmp_path):
    from app.services.factors.pipeline_task import _qualified_factor_dates
    from app.services.factors.ridge_model import _training_batch_evidence
    from app.services.factors.store import FactorWarehouse
    warehouse = FactorWarehouse(tmp_path / 'coverage.duckdb')
    day, now = pd.Timestamp('2025-01-02').date(), pd.Timestamp('2025-01-03').to_pydatetime()
    # Twenty providers returned bars; five of those have missing factor input.
    # Four additional frozen cohort names are absent from the provider result.
    warehouse.upsert_daily_bars([{'symbol': f'{index:06d}', 'trade_date': day,
        'adjust': 'qfq', 'source': 'isolated', 'source_origin': 'isolated',
        'source_row_id': index + 1, 'ingested_at': now, 'batch_id': 'raw'}
        for index in range(20)], source_key='test', watermark=20)
    warehouse.upsert_records('factor_values', [{'symbol': f'{index:06d}', 'trade_date': day,
        'factor_code': 'test_factor', 'factor_version': 1, 'normalized_value': index,
        'eligible': True, 'data_cutoff_at': now, 'created_at': now, 'calc_batch_id': 'test'}
        for index in range(15)])
    coverage = _qualified_factor_dates(warehouse, batch_id='test', expected_versions={'test_factor': 1},
                                       expected_symbol_count=24)
    assert coverage['accepted_dates'] == []
    assert coverage['skipped_dates'][0]['coverage_by_factor'] == {'test_factor': 15 / 24}
    samples = pd.DataFrame({'symbol': [f'{index:06d}' for index in range(15)],
        'trade_date': [day] * 15, 'test_factor': list(range(15)), 'target': list(range(15))})
    evidence = _training_batch_evidence(warehouse, samples, factor_calc_batch_id='test',
        feature_versions={'test_factor': 1}, window_days=250, expected_symbol_count=24)
    assert evidence['test_factor']['coverage'] == 15 / 24


def _frozen_set(client, db_session):
    from app.services.factors.definitions import seed_factor_definitions
    seed_factor_definitions(db_session)
    db_session.commit()
    version = client.post('/api/v1/factors/turnover_z20/versions', json={
        'formula_expr': FORMULA, 'params': {}, 'direction': 'nonlinear',
        'source_mapping': {'table': 'raw_daily_bars', 'field': 'turnover_rate'},
        'postprocess': {'winsorize': {'method': 'mad', 'mad_multiplier': 3.},
                        'zscore': {'ddof': 0}, 'missing_policy': 'exclude'},
    })
    assert version.status_code == 201, version.text
    created = client.post('/api/v1/factor-sets', json={'name': 'Isolated verified research'})
    assert created.status_code == 200, created.text
    set_id = created.json()['id']
    assert client.post(f'/api/v1/factor-sets/{set_id}/members', json={
        'factor_code': 'turnover_z20', 'factor_version': version.json()['version'],
        'role': 'feature', 'missing_policy': 'exclude',
    }).status_code == 200
    assert client.post(f'/api/v1/factor-sets/{set_id}/freeze', json={}).status_code == 200
    return set_id


def test_incomplete_cohort_day_does_not_shrink_denominator_or_allow_fewer_mature_dates(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from app.models.factor_model import FactorModelRun
    from app.services.factors.trusted_history_training import complete_training_dates, build_training_warehouse
    from app.services.market_history_evidence import load_trusted_history_dataset
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market

    _seed_local_market(db_session, tmp_path, day_count=2)
    set_id = _frozen_set(turnover_client, db_session)
    dataset_id, days, _directory = _dataset(tmp_path, monkeypatch, day_count=274, incomplete_day=200)
    dataset = load_trusted_history_dataset(dataset_id)
    inspection = build_training_warehouse(dataset, 'isolated-inspection')
    completeness = complete_training_dates(dataset, inspection, start_date=days[0], end_date=days[-1])
    skipped = next(row for row in completeness['skipped_dates'] if row['trade_date'] == days[200])
    assert skipped['expected_symbols'] == 24 and skipped['observed_symbols'] == 20
    assert skipped['coverage'] == 20 / 24
    submitted = turnover_client.post('/api/v1/scoring/tasks', json={
        'trusted_history_dataset_id': dataset_id, 'train_model': True,
        'materialize_scores': False, 'factor_set_id': set_id,
    })
    assert submitted.status_code == 200, submitted.text
    task = turnover_client.get('/api/v1/scoring/tasks/' + submitted.json()['id']).json()
    assert task['status'] == 'failed', task
    assert task['errors'][0]['error_code'] == 'TRUSTED_HISTORY_INVALID'
    assert 'insufficient_mature_signal_dates:249<250' in task['errors'][0]['technical_details']['error_message']
    assert db_session.scalars(select(FactorModelRun)).all() == []
    assert task['result'].get('trained_model_auto_activated', False) is False
    runtime = turnover_client.get('/api/v1/factor-models/runtime').json()
    assert runtime['weight_mode'] == 'manual' and runtime['active_model_run_id'] is None


def test_joint_label_coverage_uses_full_cohort_before_selecting_250_dates(
    turnover_client, db_session, tmp_path, monkeypatch,
):
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market
    _seed_local_market(db_session, tmp_path, day_count=2)
    set_id = _frozen_set(turnover_client, db_session)
    dataset_id, days, _directory = _dataset(tmp_path, monkeypatch, illiquid_day=200)
    submitted = turnover_client.post('/api/v1/scoring/tasks', json={
        'trusted_history_dataset_id': dataset_id, 'train_model': True,
        'materialize_scores': False, 'factor_set_id': set_id,
    })
    assert submitted.status_code == 200, submitted.text
    task = turnover_client.get('/api/v1/scoring/tasks/' + submitted.json()['id']).json()
    assert task['status'] == 'done', task
    detail = turnover_client.get('/api/v1/factor-models/' + task['result']['model']['model_run_id']).json()
    quality = detail['metrics']['trusted_joint_sample_quality']
    assert quality['expected_symbols'] == 24 and quality['minimum_coverage'] == .70
    skipped = {row['trade_date']: row for row in quality['skipped_dates']}
    # The same zero-volume day prevents an entry from T-1 and exit from T-5.
    assert skipped[str(days[195])]['observed_symbols'] == 4
    assert skipped[str(days[199])]['observed_symbols'] == 4
    assert all(row['coverage'] < .70 for row in skipped.values())
    assert detail['trade_date_count'] == 250
    policy = detail['hyperparameters']['trusted_training_policy']
    assert policy['minimum_joint_cohort_coverage'] == .70
    assert policy['performance_based_date_selection'] is False
    from app.services.factors.training_input_snapshot import load_training_input_snapshot
    frame, manifest = load_training_input_snapshot(detail['artifact_path'])
    assert manifest['split_metadata']['trusted_training_policy'] == policy
    assert not frame.trade_date.isin([days[195], days[199]]).any()


@pytest.mark.parametrize('inline', [False, True])
def test_verified_cohort_real_pipeline_is_isolated_replayable_and_never_activatable(
    turnover_client, db_session, tmp_path, monkeypatch, inline,
):
    from app.models.factor_model import FactorModelRun
    from app.models.score import Score
    from app.services.factors.store import FactorWarehouse
    from app.services.factors.training_input_snapshot import load_training_input_snapshot
    from tests.test_whitebox_factor_public_lifecycle_e2e import _seed_local_market

    default_path, _, _, _ = _seed_local_market(db_session, tmp_path, day_count=2)
    default = FactorWarehouse(default_path)
    default.initialize()
    with default.connection(read_only=True) as connection:
        before_default = connection.execute('SELECT * FROM raw_daily_bars').fetchall()
    client = turnover_client
    set_id = _frozen_set(client, db_session)
    dataset_id, days, directory = _dataset(tmp_path, monkeypatch)
    runtime_before = client.get('/api/v1/factor-models/runtime').json()
    db_session.rollback()
    engine, sql_bar_reads = db_session.get_bind(), []

    def forbid_sql_bars(_conn, _cursor, statement, _parameters, _context, _many):
        if any(('FROM ' + table) in statement for table in ('daily_bars', 'universe_daily_bars')):
            sql_bar_reads.append(statement)
            raise AssertionError('Trusted research read a mutable SQL bar source')

    def forbid(*args, **kwargs):
        raise AssertionError('Trusted research touched a default mirror, score publication or retention')

    from app.services.factors import pipeline_task
    for name in ('mirror_daily_bars', 'mirror_factor_inputs', 'materialize_factor_score_window', '_prune_unreferenced_scores'):
        monkeypatch.setattr(pipeline_task, name, forbid)
    event.listen(engine, 'before_cursor_execute', forbid_sql_bars)
    request = {'trusted_history_dataset_id': dataset_id, 'train_model': True,
        'materialize_scores': False, 'factor_set_id': set_id, 'window_days': 250,
        'validation_days': 50, 'actor': 'isolated_trusted_history'}
    try:
        if inline:
            from app.schemas.async_task import FactorPipelineCreate
            task = pipeline_task.run_trusted_history_training_inline(FactorPipelineCreate(**request))
            # The inline return uses the same serialized task schema as HTTP.
            task = client.get(f"/api/v1/scoring/tasks/{task['id']}").json()
        else:
            response = client.post('/api/v1/scoring/tasks', json=request)
            assert response.status_code == 200, response.text
            task = client.get(f"/api/v1/scoring/tasks/{response.json()['id']}").json()
    finally:
        event.remove(engine, 'before_cursor_execute', forbid_sql_bars)
    assert not sql_bar_reads
    assert task['status'] == 'done', task
    result = task['result']
    assert result['completion_status'] == 'RESEARCH_TRAINING_COMPLETED', result
    assert result['model']['research_model_gate_status'] == 'validated'
    assert result['model']['status'] == 'rejected'
    assert result['trained_model_auto_activated'] is False
    assert result['data_completeness']['denominator'] == 'frozen_research_cohort'
    assert result['data_completeness']['expected_symbols'] == 24
    assert result['data_completeness']['minimum_bar_coverage'] == .90
    assert result['batch_retention'] == {'status': 'SKIPPED', 'reason': 'TRAINING_ONLY_NON_PUBLISHING'}
    warehouse_path = result['research_warehouse_path']
    assert str(directory.resolve()) in warehouse_path and str(default_path) != warehouse_path
    model_id = result['model']['model_run_id']
    detail = client.get('/api/v1/factor-models/' + model_id).json()
    assert detail['production_eligible'] is False and detail['readiness_code'] == 'MODEL_RESEARCH_DATASET_ONLY'
    hp, metrics = detail['hyperparameters'], detail['metrics']
    assert metrics['research_model_gate_status'] == hp['research_model_gate_status'] == 'validated'
    assert hp['trusted_history_dataset'] == result['trusted_history_dataset']
    assert hp['fit_policy'] == 'cross_sectional_rank_v1'
    assert metrics['validation_date_count'] == 50 and detail['trade_date_count'] == 250
    assert 'research_only:independent_validation_required' in detail['rejection_reason']
    frame, manifest = load_training_input_snapshot(detail['artifact_path'])
    assert frame.trade_date.nunique() == 250
    assert manifest['split_metadata']['trusted_history_dataset'] == hp['trusted_history_dataset']
    for path in (f'/api/v1/factor-models/{model_id}/activate', f'/api/v1/scoring/models/{model_id}/activate'):
        blocked = client.post(path, json={'mode': 'ridge', 'actor': 'isolated_test'})
        assert blocked.status_code in (400, 409), blocked.text
    assert client.get('/api/v1/factor-models/runtime').json() == runtime_before
    assert db_session.scalars(select(Score)).all() == []
    # Rejected persisted status also protects a still-running older HTTP worker.
    db_session.expire_all()
    assert db_session.get(FactorModelRun, model_id).status == 'rejected'
    # The new contract also blocks a legacy/imported research record that
    # happens to carry validated status; status alone is not the new guard.
    model = db_session.get(FactorModelRun, model_id)
    model.status = 'validated'
    db_session.commit()
    try:
        detail = client.get('/api/v1/factor-models/' + model_id).json()
        assert detail['production_eligible'] is False
        assert detail['readiness_code'] == 'MODEL_RESEARCH_DATASET_ONLY'
        for path in (f'/api/v1/factor-models/{model_id}/activate', f'/api/v1/scoring/models/{model_id}/activate'):
            blocked = client.post(path, json={'mode': 'ridge', 'actor': 'isolated_test'})
            assert blocked.status_code in (400, 409), blocked.text
        assert client.get('/api/v1/factor-models/runtime').json() == runtime_before
    finally:
        model.status = 'rejected'
        db_session.commit()
    with default.connection(read_only=True) as connection:
        assert connection.execute('SELECT * FROM raw_daily_bars').fetchall() == before_default
