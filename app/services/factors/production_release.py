"""Explicit local-user confirmation, with replayed evidence and one DB commit.

No request can supply a certificate or change its thresholds. The append-only
model audit is the certificate; a model metadata flag has no authority.
"""
from datetime import datetime, timezone, date
import json
import math
import threading
from pathlib import Path

from sqlalchemy import select
from app.models.factor_model import FactorModelRun
from app.models.factor_runtime import FactorModelAuditLog, FactorAuditLog
from app.services.factors import production_admission as admission
from app.services.factors import production_execution as execution
from app.services.factors import production_prediction_journal as journal
from app.services.factors import production_independent_evidence as evidence
from app.services.factors import production_cost_evaluation as cost
from app.services.factors.input_replay import _json, _sha

_CONFIRM_LOCK = threading.RLock()
AUDIT_ACTION = 'production_confirm'


def _verify_receipt_files(bundle, checked):
    """Re-hash proven receipt bytes, without repeating 50 factor calculations."""
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
    root = TRUSTED_HISTORY_DATASET_ROOT.resolve()
    directory = (root / bundle['dataset_id']).resolve()
    if not directory.is_relative_to(root):
        raise ValueError('原始资料路径不正确。')
    pending = [(directory, bundle['dataset_manifest_sha256'])]
    for folder, expected in pending:
        manifest_path = folder / 'manifest.json'
        if _sha(manifest_path) != expected:
            raise ValueError('原始资料清单已经变化。')
        manifest = _json(manifest_path)
        if manifest.get('schema_version') == 2:
            for item in manifest['composition']['parents']:
                parent = (root / item['dataset_id']).resolve()
                if not parent.is_relative_to(root):
                    raise ValueError('原始资料路径不正确。')
                pending.append((parent, item['manifest_sha256']))
            continue
        for reference in manifest['evidence']['receipts']:
            path = (folder / Path(reference['path'])).resolve()
            if not path.is_relative_to(folder) or _sha(path) != reference['sha256']:
                raise ValueError('原始行情回执已经变化。')
            receipt = _json(path)
            for name, digest in receipt['files'].items():
                target = (path.parent / name).resolve()
                if not target.is_relative_to(path.parent):
                    raise ValueError('原始行情文件路径不正确。')
                key = (str(target), digest)
                if key not in checked:
                    if _sha(target) != digest:
                        raise ValueError('原始行情文件已经变化。')
                    checked.add(key)


def _model_fingerprint(model):
    names = ('id', 'model_type', 'asset_type', 'target_code', 'train_start_date', 'train_end_date',
        'validation_start_date', 'validation_end_date', 'data_cutoff_at', 'feature_versions_json',
        'hyperparameters_json', 'metrics_json', 'sample_count', 'symbol_count', 'trade_date_count',
        'artifact_path', 'created_at')
    return admission.fingerprint({name: getattr(model, name) for name in names})


def _confirmation(db, model_id):
    rows = list(db.scalars(select(FactorModelAuditLog).where(
        FactorModelAuditLog.model_run_id == model_id, FactorModelAuditLog.action == AUDIT_ACTION)))
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('确认记录重复，请检查模型审计记录。')
    value = json.loads(rows[0].note or '{}')
    record = value.get('confirmation')
    if (not isinstance(record, dict) or value.get('sha256') != admission.fingerprint(record)
            or rows[0].actor != 'local_user' or record.get('confirmed_by') != 'local_user'
            or record.get('schema_version') != 1 or record.get('evidence_replayed') is not True):
        raise ValueError('模型确认记录不完整，请重新核对。')
    return value


def verify_release_confirmation(db, model, production):
    """Cheap immutable binding verification for each normal readiness request.

    Recheck stored inputs/receipts and the exact first 50 journals. The expensive
    factor/target/cash replay runs only in confirm_release, before DB publication.
    """
    envelope = _confirmation(db, model.id)
    if envelope is None:
        return None
    record = envelope['confirmation']
    contract, candidate = journal._candidate(db, production['plan_id'], allow_released=True)
    if (production != {'plan_id': contract['plan_id'], 'execution_sha256': admission.fingerprint(contract)}
            or record['candidate'] != candidate or candidate['model_id'] != model.id
            or record['model_fingerprint'] != _model_fingerprint(model)):
        raise ValueError('模型、训练记录或确认方案已经变化，不能继续使用。')
    locked = cost.load_cost_policy(contract)
    review_id = record['independent_review_sha256']
    if not admission.HEX.fullmatch(review_id):
        raise ValueError('独立验证记录编号无效。')
    report = _json(evidence.REPORT_ROOT / contract['plan_id'] / (review_id + '.json'))
    result = report['evaluation']; identity = result['identity']
    source, _, calendar = admission._bundle_context(identity['source']['bundle_id'])
    checked = set()
    _verify_receipt_files(source, checked)
    signals = [str(d) for d in calendar if d > date.fromisoformat(contract['protocol']['observed_through'])][:50]
    constraints = evidence._execution_constraints(db, contract, calendar, [date.fromisoformat(d) for d in signals])
    if (report.get('sha256') != admission.fingerprint(result) or report['sha256'] != record['report_sha256']
            or admission.fingerprint(identity) != review_id or identity['candidate'] != candidate
            or identity['plan_id'] != contract['plan_id'] or identity['source'] != source
            or identity['cost_policy_sha256'] != locked['sha256']
            or not identity.get('execution_constraints_sha256')
            or identity['execution_constraints_sha256'] != admission.fingerprint(constraints)
            or locked['record']['candidate'] != candidate or source['symbols'] != contract['protocol']['symbols']
            or identity['signal_dates'] != signals or len(signals) != 50
            or result['independent_signal_dates'] != signals or len(identity['journals']) != 50
            or result.get('tradability_policy') != 'dated_status_explicit_limits_shared_matcher_v1'
            or not result.get('evidence_pass') or not result.get('statistical_pass') or not result.get('net_cost_pass')
            or not math.isfinite(result['rank_ic']['mean_daily_ic']) or result['rank_ic']['mean_daily_ic'] <= 0
            or not math.isfinite(result['cash_evaluation']['net_return']) or result['cash_evaluation']['net_return'] <= 0):
        raise ValueError('独立验证、股票范围或成本记录已经变化，不能继续使用。')
    for item, signal in zip(identity['journals'], signals, strict=True):
        if item['signal_date'] != signal or journal._read_journal(contract, candidate, signal)['sha256'] != item['journal_sha256']:
            raise ValueError('独立预测记录已经变化，不能继续使用。')
    # Saved journal bundles may differ from the outcome bundle. Verify their
    # immutable receipt hashes too, without calculating factors on every read.
    for signal in signals:
        saved = journal._read_journal(contract, candidate, signal)['prediction']
        current, _, _ = admission._bundle_context(saved['source']['bundle_id'])
        if saved['source'] != current:
            raise ValueError('独立预测使用的原始资料已经变化，不能继续使用。')
        _verify_receipt_files(current, checked)
    return envelope


def review_release(db, plan_id, *, bundle_id):
    contract, candidate = journal._candidate(db, plan_id, allow_released=True)
    model = db.get(FactorModelRun, candidate['model_id'])
    confirmed = verify_release_confirmation(db, model, json.loads(model.hyperparameters_json)['production_admission'])
    runtime = execution.current_runtime_baseline(db)
    if confirmed:
        return {'plan_id': plan_id, 'candidate_model_id': model.id, 'ready': False, 'blockers': [],
            'qualification_issued': True, 'model_activated': bool(runtime and runtime['active_model_run_id'] == model.id
                and runtime['weight_mode'] == 'ridge'), 'sha256': confirmed['confirmation']['review_sha256'],
            'read_only': True, 'message': '已由本人确认，重复点击不会再次启用或修改组合。'}
    try:
        locked = cost.load_cost_policy(contract)
    except FileNotFoundError:
        return {'plan_id': plan_id, 'candidate_model_id': model.id, 'ready': False,
            'blockers': ['验证费用没有在后续样本开始前固定，这个候选不能补发确认。请重新准备研究模型和验证方案。'],
            'sha256': admission.fingerprint({'plan_id':plan_id,'missing':'cost-policy'}),
            'qualification_issued':False,'model_activated':False,'read_only':True,
            'message':'缺少事先固定的验证费用，未启用模型。'}
    review = evidence.review_independent_evidence(db, plan_id, bundle_id=bundle_id, require_execution_constraints=True)
    reports = evidence.saved_report_summaries(contract, candidate, locked['sha256'])
    blockers = list(review['blockers'])
    if len(reports) != 1:
        blockers.append('请先完成一次独立验证；多个结果不能自动挑选。')
    result = reports[0] if len(reports) == 1 else None
    if result and (result['review_sha256'] != review['sha256'] or result['bundle_id'] != bundle_id):
        blockers.append('资料已更新，请使用当前资料重新完成独立验证。')
    if result and not result['evidence_pass']:
        blockers.append('独立验证或扣除费用后的收益未通过，不能启用。')
    if execution.current_portfolio_cost(db, contract['cost']['portfolio_id']) != contract['cost']:
        blockers.append('组合资金或费用设置已变化，与验证时不一致，不能启用。')
    identity = {'schema_version': 1, 'plan_id': plan_id, 'candidate': candidate,
        'model_fingerprint': _model_fingerprint(model), 'independent_review_sha256': review['sha256'],
        'report_sha256': result['report_sha256'] if result else None,
        'runtime_before': runtime, 'cost_sha256': admission.fingerprint(contract['cost'])}
    return {'plan_id': plan_id, 'candidate_model_id': model.id, 'ready': not blockers,
        'blockers': blockers, 'sha256': admission.fingerprint(identity), 'identity': identity,
        'qualification_issued': False, 'model_activated': False, 'read_only': True,
        'independent_signal_days': len(review['identity']['signal_dates']),
        'mature_signal_days': review['mature_signal_count'], 'result': result,
        'message': '确认后将启用此模型用于后续评分；组合交易开关和历史记录不会改变。'}


def confirm_release(db, plan_id, *, bundle_id, expected_sha256, confirmed):
    if confirmed is not True:
        raise ValueError('请先由本人确认。')
    with _CONFIRM_LOCK:
        try:
            review = review_release(db, plan_id, bundle_id=bundle_id)
            if review['sha256'] != expected_sha256:
                raise ValueError('确认内容已经变化，请重新查看后再确认。')
            if review['qualification_issued']:
                return review
            if not review['ready']:
                raise ValueError('；'.join(review['blockers']))
            # Independently replay every saved prediction, source receipt,
            # tradable target and frozen cash cost, even for a cached report.
            report = evidence.run_independent_evaluation(db, plan_id, bundle_id=bundle_id,
                expected_sha256=review['identity']['independent_review_sha256'], require_execution_constraints=True)
            from app.models.factor_runtime import FactorRuntimeState
            db.scalar(select(FactorRuntimeState).where(FactorRuntimeState.id == 1)
                .with_for_update().execution_options(populate_existing=True))
            db.get(FactorModelRun, review['candidate_model_id'], with_for_update=True, populate_existing=True)
            latest = review_release(db, plan_id, bundle_id=bundle_id)
            if latest['sha256'] != expected_sha256 or not latest['ready']:
                raise ValueError('验证期间模型或资料发生变化，请重新查看。')
            if report['sha256'] != review['identity']['report_sha256'] or not report['evaluation']['evidence_pass']:
                raise ValueError('独立验证重新计算后不一致，未启用模型。')
            model = db.get(FactorModelRun, review['candidate_model_id'])
            model_id = model.id
            record = {**review['identity'], 'review_sha256': expected_sha256,
                'confirmed_by': 'local_user', 'confirmed_at': datetime.now(timezone.utc).isoformat(),
                'evidence_replayed': True}
            certificate = {'confirmation': record, 'sha256': admission.fingerprint(record)}
            before = execution.current_runtime_baseline(db)
            db.add(FactorModelAuditLog(action=AUDIT_ACTION, model_run_id=model.id,
                previous_mode=(before or {}).get('weight_mode', 'manual'), new_mode='ridge',
                previous_model_run_id=(before or {}).get('active_model_run_id'), new_model_run_id=model.id,
                actor='local_user', note=admission.canonical(certificate)))
            model.status = 'validated'
            db.flush()
            from app.services.factors.runtime import activate_factor_model
            snapshot = activate_factor_model(db, model.id, mode='ridge', actor='local_user',
                note='本人确认独立验证和费用结果；确认记录：' + certificate['sha256'])
            db.add(FactorAuditLog(action=AUDIT_ACTION, factor_set_id=execution.load_execution(plan_id)[0]['protocol']['factor_set_id'],
                model_run_id=model.id, actor='local_user', before_json=admission.canonical(before),
                after_json=admission.canonical(snapshot.to_dict()), attributes_json=admission.canonical(certificate)))
            response = {'plan_id': plan_id, 'candidate_model_id': model_id, 'qualification_issued': True,
                'model_activated': True, 'sha256': expected_sha256, 'runtime': snapshot.to_dict(),
                'message': '已确认并启用此模型；组合交易开关和历史记录未改变。'}
            db.commit()
            return response
        except BaseException:
            db.rollback()
            raise
