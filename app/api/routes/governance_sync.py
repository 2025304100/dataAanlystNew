"""Explicitly injected candidate API; never mounted by application startup."""
from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import NoResultFound
from app.services.governance_fact_contracts import WriterError
from app.services.governance_sync import _pack


def create_sync_router(service, capturers):
    """Operators register acquisition functions in code, not in HTTP input.

    Only candidate engines accepted by install_sync_schema can construct the
    supplied service. Wiring this into production requires the M09/M12 gates.
    """
    router = APIRouter(prefix='/governance-sync')
    registry = dict(capturers)
    def view(job):
        result = dict(job); result.pop('lease_token', None)
        result['attempt_history'] = [{k:v for k,v in row.items() if k!='lease_token'} for row in result['attempt_history']]
        result['spec'] = _pack(result['spec'])
        return result
    def invoke(action):
        try: return action()
        except NoResultFound: raise HTTPException(status_code=404, detail='SYNC_JOB_NOT_FOUND') from None
        except WriterError as error: raise HTTPException(status_code=409, detail=str(error)) from None
        except (ValueError, TypeError, KeyError): raise HTTPException(status_code=422, detail='SYNC_SPEC_INVALID') from None
    @router.post('/jobs')
    def create(body: dict):
        if set(body) != {'spec', 'idempotency_key'}: raise HTTPException(status_code=422, detail='SYNC_CREATE_FIELDS_INVALID')
        job = invoke(lambda: service.create(body['spec'], idempotency_key=body['idempotency_key']))
        return {'job_id': job, 'production_admission': 'not_certified'}
    @router.get('/jobs/{job_id}')
    def status(job_id: str):
        return invoke(lambda: {**view(service.inspect(job_id)), 'coverage': service.coverage(job_id)})
    @router.post('/jobs/{job_id}/run')
    def run(job_id: str):
        def execute():
            job = service.inspect(job_id); acquire = registry.get(job['spec']['adapter'])
            if acquire is None: raise WriterError('SYNC_CAPTURE_UNREGISTERED')
            return view(service.run(job_id, lambda work: acquire(job['spec'], work)))
        return invoke(execute)
    @router.post('/jobs/{job_id}/cancel')
    def cancel(job_id: str): return invoke(lambda: {'status': service.cancel(job_id)})
    @router.post('/jobs/{job_id}/retry')
    def retry(job_id: str):
        invoke(lambda: service.retry(job_id))
        return {'status': 'queued'}
    return router
