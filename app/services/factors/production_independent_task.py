"""Durable isolated evaluation task; all input dates/fees/model are server fixed."""
from datetime import datetime, timezone
import json

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks
from app.services.factors import production_independent_evidence as evidence
from app.services.factors import production_admission as admission

TASK_TYPE = 'factor_independent_evaluation'


def submit_independent_evaluation(db, plan_id, *, bundle_id, expected_sha256):
    review = evidence.review_independent_evidence(db, plan_id, bundle_id=bundle_id, require_execution_constraints=True)
    if not review['ready']:
        raise ValueError('production_evidence_not_ready:' + ','.join(review['blockers']))
    if review['sha256'] != expected_sha256:
        raise ValueError('production_evidence_review_changed')
    payload = {'plan_id': plan_id, 'bundle_id': bundle_id, 'expected_sha256': expected_sha256}
    identity = admission.fingerprint({'task_type': TASK_TYPE, **payload})
    row, created = async_tasks.create_async_task_once(TASK_TYPE, payload,
        task_id=identity[:32], request_fingerprint=identity)
    if created: async_tasks._start_worker(row.id, _run)
    return row


def get_independent_task(db, task_id):
    if not isinstance(task_id, str) or len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
        raise ValueError('production_evidence_invalid_task_id')
    with db.no_autoflush:
        row = db.get(AsyncTaskRecord, task_id)
        return async_tasks._task_to_read(row) if row and row.task_type == TASK_TYPE else None


def compact_result(payload,report):
    result=report['evaluation']
    return {'plan_id':payload['plan_id'],'review_sha256':payload['expected_sha256'],
        'report_sha256':report['sha256'],'mean_daily_ic':result['rank_ic']['mean_daily_ic'],
        'net_return':result['cash_evaluation']['net_return'],'evidence_pass':result['evidence_pass'],
        'qualification_certificate':False,'model_activated':False,'production_scores_published':0}


def get_independent_report(db,task_id):
    from app.services.factors.input_replay import _json
    task=get_independent_task(db,task_id)
    if task is None:raise FileNotFoundError('production_evidence_task_not_found')
    if task.status!='done':raise ValueError('production_evidence_task_not_completed')
    payload=task.payload_json
    if isinstance(payload,str):payload=json.loads(payload)
    plan_id,sha=payload['plan_id'],payload['expected_sha256']
    if not admission.HEX.fullmatch(plan_id) or not admission.HEX.fullmatch(sha):
        raise ValueError('production_evidence_invalid_artifact_identity')
    envelope=_json(evidence.REPORT_ROOT/plan_id/(sha+'.json'))
    review=evidence.review_independent_evidence(db,plan_id,bundle_id=payload['bundle_id'],
        require_execution_constraints='execution_constraints_sha256' in envelope['evaluation']['identity'])
    if review['sha256']!=sha:raise ValueError('production_evidence_report_source_changed')
    if (envelope.get('sha256')!=admission.fingerprint(envelope['evaluation'])
            or envelope['evaluation']['identity']!=review['identity']
            or compact_result(payload,envelope)!=task.result):
        raise ValueError('production_evidence_report_checksum_or_task_binding_changed')
    return {'report':envelope,'release_reverification_required':True,'qualification_certificate':False}


def _run(task_id):
    SessionLocal = get_session_local()
    def update(**values):
        with SessionLocal() as db: return async_tasks._set_task(db, task_id, **values)
    def progress(stage, percent):
        with SessionLocal() as db:
            task = db.get(AsyncTaskRecord, task_id)
            if task is None or task.cancel_requested or task.status in ('cancelled','failed') or async_tasks.is_worker_stop_requested():
                raise InterruptedError('production_evidence_cancelled')
        update(status='running',stage=stage,percent=percent,message='正在核对独立预测和费用：'+str(percent)+'%',
            heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None),stage_budget_seconds=1200)
    try:
        with SessionLocal() as db:
            row = db.get(AsyncTaskRecord, task_id)
            if row is None:return
            payload = json.loads(row.payload_json)
        update(started_at=datetime.now(timezone.utc).replace(tzinfo=None))
        with SessionLocal() as db:
            report = evidence.run_independent_evaluation(db, **payload, progress=progress, require_execution_constraints=True)
        passed = report['evaluation']['evidence_pass']
        # MySQL async_tasks.result_json is TEXT. Keep large order traces only
        # in the checksummed local artifact, not a >64KiB task result field.
        compact = compact_result(payload,report)
        update(status='done',stage='done',percent=100,
            message='验证已通过，请核对结果后确认启用模型' if passed else
                '已完成验证，但预测效果或扣除费用后的收益未达到要求，模型未启用',
            result_json=json.dumps(compact,ensure_ascii=False,default=str),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    except InterruptedError:
        update(status='cancelled',stage='cancelled',message='独立评估已停止；模型未启用。',
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    except Exception as exc:
        detail = str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        update(status='failed',stage='failed',message='独立证据验证未完成：'+detail,
            errors_json=json.dumps([{'code':'production_evidence.failed','detail_zh':detail}]),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
