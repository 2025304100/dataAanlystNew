"""Prospective, same-close-day predictions; never publish Scores or fit a model.

The server clock is the only issue clock. A caller cannot backfill an earlier
signal date or supply predictions. Journals are not qualification certificates.
"""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import math
from uuid import uuid4

from sqlalchemy import select
from app.models.factor_model import FactorModelRun
from app.models.factor_governance import FactorModelMember
from app.services.factors import production_admission as admission
from app.services.factors import production_execution as execution
from app.services.factors.input_replay import _json, _sha

JOURNAL_ROOT = admission.PROJECT / 'tmp/factor_production_predictions'
EXCHANGE_ZONE = timezone(timedelta(hours=8))


def _now():
    return datetime.now(timezone.utc)


def _candidate(db, plan_id):
    contract, sha = execution.load_execution(plan_id)
    result = _json(execution.EXECUTION_ROOT / (plan_id + '.result.json'))
    if result.get('plan_id') != plan_id or result.get('execution_sha256') != sha:
        raise ValueError('production_prediction_execution_changed')
    model_id = result['candidate']['model_run_id']
    model = db.get(FactorModelRun, model_id)
    if model is None or model.status != 'pending_validation':
        raise ValueError('production_prediction_requires_pending_candidate')
    hp = json.loads(model.hyperparameters_json or '{}')
    if (hp.get('production_admission') != {'plan_id': plan_id, 'execution_sha256': sha}
            or hp.get('trusted_history_dataset') is not None
            or hp.get('factor_set_id') != contract['protocol']['factor_set_id']
            or json.loads(model.metrics_json or '{}').get('production_model_gate_status') != 'validated'):
        raise ValueError('production_prediction_model_contract_changed')
    from app.services.factors.training_input_snapshot import load_training_input_snapshot
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
    from pathlib import Path
    path = Path(model.artifact_path or '').resolve()
    if not path.is_relative_to(TRUSTED_HISTORY_DATASET_ROOT.resolve()):
        raise ValueError('production_prediction_snapshot_path_escape')
    _, snapshot = load_training_input_snapshot(path)
    if (snapshot['model_run_id'] != model_id or snapshot['input_fingerprint'] != hp.get('input_fingerprint')
            or snapshot['split_metadata']['production_admission'] != hp['production_admission']):
        raise ValueError('production_prediction_snapshot_changed')
    members = list(db.scalars(select(FactorModelMember).where(FactorModelMember.model_run_id == model_id)))
    versions = {m.factor_code: int(m.factor_version) for m in members}
    coefficients = {m.factor_code: float(m.coefficient) for m in members}
    intercept = float(hp.get('final_intercept', 0))
    if (not members or len(versions) != len(members) or versions != contract['protocol']['feature_versions']
            or versions != json.loads(model.feature_versions_json)
            or coefficients != result['candidate']['coefficients']
            or not all(math.isfinite(v) for v in [*coefficients.values(), intercept])):
        raise ValueError('production_prediction_invalid_coefficients')
    parent, _ = admission._parent_context(db, contract['protocol']['parent_model_id'])
    if admission.fingerprint(parent) != contract['protocol']['parent_fingerprint']:
        raise ValueError('production_prediction_parent_changed')
    identity = {'model_id': model_id, 'coefficients': coefficients, 'intercept': intercept,
        'feature_versions': versions, 'input_fingerprint': hp['input_fingerprint'],
        'snapshot_manifest_sha256': _sha(path), 'execution_sha256': sha}
    return contract, identity


def _availability(dataset):
    manifest = _json(dataset.directory / 'manifest.json')
    if manifest['schema_version'] == 1:
        return admission._receipt_availability(dataset.directory, manifest)
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
    selection = _json(dataset.directory / 'quality.json')['selection']
    available = {}
    for ref in manifest['composition']['parents']:
        directory = TRUSTED_HISTORY_DATASET_ROOT / ref['dataset_id']
        times = admission._receipt_availability(directory, _json(directory / 'manifest.json'))
        for item in selection:
            if item['dataset_id'] == ref['dataset_id']:
                from datetime import date
                for day in item['dates']:
                    key = item['symbol'], date.fromisoformat(day)
                    available[key] = times[key]
    return available


def _window(now, boundary):
    local = now.astimezone(EXCHANGE_ZONE)
    if str(local.date()) <= boundary:
        raise ValueError('production_prediction_no_postboundary_signal_day')
    if (local.hour, local.minute) < (15, 15):
        raise ValueError('production_prediction_daily_bar_not_closed')
    return local.date()


def _read_journal(contract, candidate, signal_date):
    from datetime import date
    day = date.fromisoformat(str(signal_date))
    plan_id = contract['plan_id']
    path = JOURNAL_ROOT / plan_id / (str(day) + '.json')
    envelope = _json(path)
    report = envelope['prediction']
    if (envelope.get('sha256') != admission.fingerprint(report) or report['candidate'] != candidate
            or report['plan_id'] != plan_id or report['signal_date'] != str(day)
            or report.get('qualification_certificate') is not False):
        raise ValueError('production_prediction_journal_changed')
    issued = datetime.fromisoformat(report['issued_at'])
    if issued.tzinfo is None or _window(issued, contract['protocol']['observed_through']) != day:
        raise ValueError('production_prediction_invalid_issue_time')
    return envelope


def read_journal(db, plan_id, signal_date):
    return _read_journal(*_candidate(db, plan_id), signal_date)


def write_daily_prediction(db, plan_id, *, bundle_id):
    """Accept only server-verified local inputs for TODAY after close.

    Same-day issuance is stricter than next-open issuance and avoids inventing
    a future exchange session. Late requests cannot fill a missing journal.
    """
    started = _now()
    contract, candidate = _candidate(db, plan_id)
    day = _window(started, contract['protocol']['observed_through'])
    path = JOURNAL_ROOT / plan_id / (str(day) + '.json')
    if path.exists():
        existing = read_journal(db, plan_id, day)
        if existing['prediction']['source']['bundle_id'] != bundle_id:
            raise ValueError('production_prediction_day_already_locked')
        return existing
    from app.services.market_history_evidence import load_trusted_history_dataset
    bundle, _, _ = admission._bundle_context(bundle_id)
    dataset = load_trusted_history_dataset(bundle['dataset_id'])
    protocol = contract['protocol']
    if (dataset.manifest_sha256 != bundle['dataset_manifest_sha256']
            or list(dataset.symbols) != protocol['symbols'] or dataset.calendar[-1] != day):
        raise ValueError('production_prediction_requires_same_day_verified_cohort')
    available = _availability(dataset)
    # Every bar used by the engine must already exist in a verified receipt.
    for row in dataset.bars.itertuples():
        captured = available.get((row.symbol, row.trade_date.date()))
        if captured is None or captured > started:
            raise ValueError('production_prediction_source_not_yet_available')
    from app.services.factors.factor_engine import calculate_stock_factors, resolve_frozen_factor_specs
    from app.services.factors.trusted_history_training import build_training_warehouse
    from app.services.factors.scoring_bridge import _load_factor_rows, evaluate_linear_factor_model
    from app.services.factors.store import close_owned_warehouse_path
    specs = resolve_frozen_factor_specs(db, (protocol['factor_set_id'],))
    if {s.code: s.version for s in specs} != candidate['feature_versions']:
        raise ValueError('production_prediction_factor_versions_changed')
    dataset = replace(dataset, bars=dataset.bars.loc[dataset.bars.trade_date.dt.date <= day].copy())
    batch = 'prospective-' + uuid4().hex
    warehouse = None
    try:
        warehouse = build_training_warehouse(dataset, batch)
        calculation = calculate_stock_factors(warehouse, start_date=day, end_date=day,
            calc_batch_id=batch, trade_dates=(day,), frozen_factor_specs=specs, selected_features_only=True)
        rows, _ = _load_factor_rows(warehouse, trade_date=day, factor_calc_batch_id=calculation.calc_batch_id,
            extra_factor_codes=tuple(candidate['coefficients']), expected_versions=candidate['feature_versions'])
        inference = evaluate_linear_factor_model(rows, candidate['coefficients'], candidate['intercept'])
        predictions = inference['alpha_raw']
        if (not set(predictions).issubset(protocol['symbols'])
                or len(predictions) / len(protocol['symbols']) < .90):
            raise ValueError('production_prediction_insufficient_full_cohort_coverage')
        finished = _now()
        if _window(finished, protocol['observed_through']) != day:
            raise ValueError('production_prediction_issue_window_expired')
        # Read back model and source after calculation before immutable commit.
        db.expire_all()
        if _candidate(db, plan_id)[1] != candidate or admission._bundle_context(bundle_id)[0] != bundle:
            raise ValueError('production_prediction_inputs_changed_during_calculation')
        report = {'schema_version': 1, 'plan_id': plan_id, 'signal_date': str(day),
            'issued_at': finished.isoformat(), 'candidate': candidate, 'source': bundle,
            'full_cohort_denominator': len(protocol['symbols']), 'predicted_symbols': len(predictions),
            'predictions': {s: {'linear_signal': predictions.get(s),
                'features': {code: rows.get(s, {}).get(code, {}).get('normalized_value')
                    for code in candidate['coefficients']}} for s in protocol['symbols']},
            'qualification_certificate': False, 'production_scores_published': 0,
            'model_activated': False, 'labels_used': False,
            'issue_policy': 'same_signal_day_after_1515_exchange_time'}
        envelope = {'prediction': report, 'sha256': admission.fingerprint(report)}
        execution._publish(path, envelope)
        return envelope
    finally:
        if warehouse is not None:
            close_owned_warehouse_path(warehouse.path)


def journal_status(db, plan_id, *, bundle_id=None):
    contract, candidate = _candidate(db, plan_id)
    directory = JOURNAL_ROOT / plan_id
    dates = []
    for path in sorted(directory.glob('*.json')) if directory.exists() else ():
        report = _read_journal(contract, candidate, path.stem)['prediction']
        dates.append(report['signal_date'])
    can_record = False
    if bundle_id is not None:
        bundle, _, _ = admission._bundle_context(bundle_id)
        local = _now().astimezone(EXCHANGE_ZONE)
        can_record = (str(local.date()) > contract['protocol']['observed_through']
            and (local.hour, local.minute) >= (15, 15)
            and bundle['last_date'] == str(local.date())
            and bundle['symbols'] == contract['protocol']['symbols']
            and str(local.date()) not in dates)
    return {'plan_id': plan_id, 'candidate_model_id': candidate['model_id'],
        'independent_after': contract['protocol']['observed_through'],
        'journal_dates': dates, 'journal_count': len(dates),
        'required_independent_dates': 50, 'independent_validation_complete': False,
        'cost_evaluation_complete': False, 'qualification_issued': False,
        'can_record_today': can_record, 'model_activated': False, 'read_only': True}
