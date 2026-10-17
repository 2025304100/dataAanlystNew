"""Durable, idempotent research-input verification jobs."""
from datetime import datetime, timezone
import hashlib
import json
import threading

from sqlalchemy import select
from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.services import async_tasks
from app.services.factors.input_replay import TASK_TYPE, _bundle_dir, _sha, load_model_contract, run_input_replay

_CREATE_LOCK = threading.Lock()

def get_input_replay_task(db, request_id):
    """Snapshot only: unlike generic get_async_task, no patrol writes on GET."""
    with db.no_autoflush:
        row=db.get(AsyncTaskRecord,request_id)
        if row is None or row.task_type!=TASK_TYPE:return None
        return async_tasks._task_to_read(row)

def create_input_replay(db, *, bundle_id, manifest_sha256, model_id, request_id):
    if _sha(_bundle_dir(bundle_id)/'manifest.json')!=manifest_sha256:
        raise ValueError('input_replay_reviewed_bundle_changed')
    with db.no_autoflush:load_model_contract(db,model_id)
    payload=dict(bundle_id=bundle_id,manifest_sha256=manifest_sha256,model_id=model_id,request_id=request_id)
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    with _CREATE_LOCK:
        # A worker has a separate session. Avoid an old request transaction
        # hiding its terminal state when the user explicitly runs another check.
        with get_session_local()()as check:
            active=check.scalar(select(AsyncTaskRecord.id).where(AsyncTaskRecord.task_type==TASK_TYPE,
                AsyncTaskRecord.status.in_(['queued','running','cancelling']),AsyncTaskRecord.id!=request_id).limit(1))
            if active:raise ValueError('input_replay_already_running:'+active)
        task,created=async_tasks.create_async_task_once(TASK_TYPE,payload,task_id=request_id,request_fingerprint=fingerprint)
        if created:async_tasks._start_worker(task.id,_run)
        return task

def _run(task_id):
    SessionLocal=get_session_local()
    def update(**values):
        with SessionLocal()as session:
            return async_tasks._set_task(session,task_id,**values)
    def progress(stage,percent):
        with SessionLocal()as session:
            row=session.get(AsyncTaskRecord,task_id)
            if row is None or row.cancel_requested or row.status in ('cancelled','failed') or async_tasks.is_worker_stop_requested():
                raise InterruptedError('input_replay_cancelled')
        update(status='running',stage=stage,percent=percent,heartbeat_at=datetime.now(timezone.utc).replace(tzinfo=None),
            message=stage,stage_budget_seconds=600)
    try:
        with SessionLocal()as session:
            row=session.get(AsyncTaskRecord,task_id)
            if row is None:return
            payload=json.loads(row.payload_json)
        update(started_at=datetime.now(timezone.utc).replace(tzinfo=None))
        with SessionLocal()as session:
            report=run_input_replay(session,**{k:payload[k]for k in ('bundle_id','manifest_sha256','model_id')},
                replay_id=task_id,progress=progress)
        update(status='done',stage='done',percent=100,message='Research input chain verified; no production scores published',
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            result_json=json.dumps({k:report[k]for k in ('replay_id','research_only','production_scores_published','scored_symbols','last_date')}))
    except InterruptedError:
        update(status='cancelled',stage='cancelled',message='Verification stopped; production data unchanged',
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
    except Exception as exc:
        # Do not leak SQL connection strings, paths or provider response bodies.
        code=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('input_replay_') else type(exc).__name__
        update(status='failed',stage='failed',message='Research input verification failed: '+code,
            errors_json=json.dumps([{'code':'input_replay.failed','detail_zh':code}]),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
