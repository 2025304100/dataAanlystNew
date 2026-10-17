"""One frozen-plan candidate attempt, isolated from live scores and trading."""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
import json
import threading

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks
from app.services.factors import production_execution as execution
from app.services.factors import production_admission as admission

TASK_TYPE = 'factor_production_candidate'
_CREATE_LOCK = threading.Lock()


def task_id_for_plan(plan_id):
    return admission.fingerprint({'task_type': TASK_TYPE, 'plan_id': plan_id})[:32]


def get_candidate_task(db, plan_id):
    if not admission.HEX.fullmatch(plan_id):
        raise ValueError('production_execution_invalid_plan')
    with db.no_autoflush:
        row = db.get(AsyncTaskRecord, task_id_for_plan(plan_id))
        return async_tasks._task_to_read(row) if row and row.task_type == TASK_TYPE else None


def create_candidate_task(db, model_id, *, bundle_id, portfolio_id, expected_sha256):
    with _CREATE_LOCK:
        frozen = execution.freeze_execution(db, model_id, bundle_id=bundle_id,
            portfolio_id=portfolio_id, expected_sha256=expected_sha256)
        contract = frozen['contract']
        task_id = task_id_for_plan(contract['plan_id'])
        payload = {'plan_id': contract['plan_id'], 'execution_sha256': frozen['sha256']}
        task, created = async_tasks.create_async_task_once(TASK_TYPE, payload,
            task_id=task_id, request_fingerprint=admission.fingerprint(payload))
        if created:
            async_tasks._start_worker(task_id, _run)
        return task


def run_candidate(db, identity, *, progress=lambda *args: None):
    """Use original fit policy/gates on pre-boundary data, never new holdout labels."""
    from app.services.market_history_evidence import load_trusted_history_dataset
    from app.services.factors.input_replay import _load_inputs
    from app.services.factors.factor_engine import calculate_stock_factors, resolve_frozen_factor_specs
    from app.services.factors.target_engine import calculate_targets
    from app.services.factors.trusted_history_training import build_training_warehouse, complete_training_dates
    from app.services.factors.pipeline_task import _qualified_factor_dates
    from app.services.factors.ridge_model import train_rolling_ridge, ModelGate
    from app.services.factors.store import close_owned_warehouse_path

    contract, sha = execution.load_execution(identity['plan_id'])
    if identity != {'plan_id': contract['plan_id'], 'execution_sha256': sha}:
        raise ValueError('production_execution_contract_changed')
    protocol, source = contract['protocol'], contract['source']
    progress('verifying_source_receipts', 5)
    bundle, _ = _load_inputs(source['bundle_id'], source['bundle_manifest_sha256'])
    dataset = load_trusted_history_dataset(bundle['dataset_id'])
    if (dataset.manifest_sha256 != source['dataset_manifest_sha256']
            or list(dataset.symbols) != protocol['symbols']):
        raise ValueError('production_execution_training_source_changed')
    parent, _ = admission._parent_context(db, protocol['parent_model_id'])
    if admission.fingerprint(parent) != protocol['parent_fingerprint']:
        raise ValueError('production_execution_parent_changed')
    if execution.current_runtime_baseline(db) != contract.get('runtime_baseline'):
        raise ValueError('production_execution_runtime_baseline_changed')
    specs = resolve_frozen_factor_specs(db, (protocol['factor_set_id'],))
    if {s.code: s.version for s in specs} != protocol['feature_versions']:
        raise ValueError('production_execution_factor_versions_changed')
    db.rollback()
    cutoff = date.fromisoformat(contract['training_cutoff'])
    # Physically exclude future holdout prices and labels from the fit store.
    dataset = replace(dataset,
        bars=dataset.bars.loc[dataset.bars.trade_date.dt.date <= cutoff].copy(),
        calendar=tuple(d for d in dataset.calendar if d <= cutoff))
    if len(dataset.calendar) < protocol['minimum_raw_trade_dates']:
        raise ValueError('production_execution_preboundary_history_insufficient')
    claim = execution.EXECUTION_ROOT / (contract['plan_id'] + '.attempt.json')
    execution._publish(claim, {'execution_sha256': sha,
        'started_at': datetime.now(timezone.utc).isoformat(), 'single_attempt': True})
    warehouse = None
    try:
        progress('building_isolated_training_store', 25)
        warehouse = build_training_warehouse(dataset, 'production-' + task_id_for_plan(contract['plan_id']))
        complete = complete_training_dates(dataset, warehouse, start_date=dataset.calendar[0],
            end_date=dataset.calendar[-1], threshold=.90)
        progress('calculating_frozen_factor', 40)
        batch = 'production-' + task_id_for_plan(contract['plan_id'])
        factors = calculate_stock_factors(warehouse, start_date=dataset.calendar[0], end_date=dataset.calendar[-1],
            calc_batch_id=batch, frozen_factor_specs=specs, selected_features_only=True,
            trade_dates=tuple(complete['accepted_dates']))
        quality = _qualified_factor_dates(warehouse, batch_id=batch,
            expected_versions=protocol['feature_versions'], minimum_coverage=.90,
            expected_symbol_count=len(protocol['symbols']))
        progress('building_mature_tradable_labels', 60)
        targets = calculate_targets(warehouse, start_date=dataset.calendar[0], end_date=dataset.calendar[-1],
            calc_batch_id=batch, target_horizon=5, symbols=dataset.symbols)
        progress('fitting_frozen_candidate', 75)
        result = train_rolling_ridge(db, warehouse, factor_calc_batch_id=factors.calc_batch_id,
            target_calc_batch_id=targets.calc_batch_id, factor_set_id=protocol['factor_set_id'],
            data_cutoff_date=cutoff, window_days=protocol['window_days'], validation_days=protocol['validation_days'],
            alphas=tuple(protocol['alphas']), gate=ModelGate(**protocol['model_gate']),
            allowed_trade_dates=tuple(quality['accepted_dates']), fit_policy=protocol['fit_policy'],
            production_protocol_identity=identity)
        progress('saving_candidate_evidence', 95)
        # Changes to frozen factors/source while fitting must not be published.
        current, _ = admission._parent_context(db, protocol['parent_model_id'])
        if admission.fingerprint(current) != protocol['parent_fingerprint']:
            raise ValueError('production_execution_parent_changed_during_fit')
        if execution.current_runtime_baseline(db) != contract.get('runtime_baseline'):
            raise ValueError('production_execution_runtime_baseline_changed_during_fit')
        db.commit()
        report = {'schema_version': 1, 'plan_id': contract['plan_id'], 'execution_sha256': sha,
            'candidate': asdict(result), 'warehouse_path': str(warehouse.path),
            'complete_bar_dates': len(complete['accepted_dates']),
            'factor_qualified_dates': len(quality['accepted_dates']),
            'full_cohort_denominator': len(protocol['symbols']),
            'independent_qualification_complete': False, 'production_scores_published': 0,
            'model_activated': False, 'finished_at': datetime.now(timezone.utc).isoformat()}
        execution._publish(execution.EXECUTION_ROOT / (contract['plan_id'] + '.result.json'), report)
        return report
    except BaseException:
        db.rollback()
        raise
    finally:
        if warehouse is not None:
            close_owned_warehouse_path(warehouse.path)


def _run(task_id):
    SessionLocal = get_session_local()
    def update(**values):
        with SessionLocal() as session:
            return async_tasks._set_task(session, task_id, **values)
    def progress(stage, percent):
        with SessionLocal() as session:
            row = session.get(AsyncTaskRecord, task_id)
            if row is None or row.cancel_requested or row.status in ('cancelled', 'failed') or async_tasks.is_worker_stop_requested():
                raise InterruptedError('production_execution_cancelled')
        update(status='running', stage=stage, percent=percent, message=stage,
            heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None), stage_budget_seconds=900)
    try:
        with SessionLocal() as session:
            row = session.get(AsyncTaskRecord, task_id)
            if row is None:
                return
            identity = json.loads(row.payload_json)
        update(started_at=datetime.now(timezone.utc).replace(tzinfo=None))
        with SessionLocal() as session:
            report = run_candidate(session, identity, progress=progress)
        update(status='done', stage='done', percent=100,
            message='候选训练完成；仍需独立样本及成本后验证，尚未启用',
            result_json=json.dumps(report, default=str),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    except InterruptedError:
        update(status='cancelled', stage='cancelled', message='候选训练已停止；未启用模型',
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(('production_execution_', 'trusted_history_insufficient_')) else type(exc).__name__
        update(status='failed', stage='failed', message='候选训练未完成：' + detail,
            errors_json=json.dumps([{'code': 'production_candidate.failed', 'detail_zh': detail}]),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
