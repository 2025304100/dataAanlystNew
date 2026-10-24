"""All submissions use isolated SQLite stores and stub workers, never main."""
import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from app.models.async_task import AsyncTaskRecord
from app.models.portfolio_inspection_workflow import ReconciliationRecheckRequest
from app.services import portfolio_recheck_requests as receipts
from app.services import portfolio_reconciliation_job as job
from app.api.routes import portfolio_inspection_workflow as routes
from tests.test_portfolio_reconciliation_job import store, ready, DAY

pytestmark = pytest.mark.whitebox


@pytest.fixture
def submitted(ready, monkeypatch):
    monkeypatch.setattr(routes, "_authorize", lambda *a: None)
    calls = []
    monkeypatch.setattr(job, "request_reconciliation_check", lambda db, pid, **kw: calls.append(kw) or {"saved": True})
    return ready.store, calls


def submit(store, key="original-request-key", day=DAY):
    with store.factory() as db:
        return routes.recheck_sources(store.pid, routes.SourceRecheckRequest(trade_date=day, request_key=key), db, "tester")


def test_replay_only_registers_and_dispatches_once(submitted):
    store, calls = submitted
    first = submit(store)
    assert first["task_id"] and first["request_key"] == "original-request-key"
    assert submit(store) == first
    assert len(calls) == len(store.started) == 1
    with store.factory() as db:
        assert len(list(db.scalars(select(ReconciliationRecheckRequest)))) == 1
        assert routes.recover_source_request(store.pid, first["request_key"], db, "tester") == first


def test_changed_date_or_actor_conflicts(submitted):
    store, calls = submitted
    submit(store)
    with pytest.raises(routes.HTTPException) as exc:
        submit(store, day=DAY+timedelta(days=1))
    assert exc.value.status_code == 409
    with store.factory() as db, pytest.raises(routes.HTTPException):
        routes.recheck_sources(store.pid, routes.SourceRecheckRequest(trade_date=DAY, request_key="original-request-key"), db, "other")
    assert len(calls) == len(store.started) == 1


def test_concurrent_requests_share_the_same_registration(submitted):
    store, calls = submitted
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(store), range(2)))
    assert len(calls) == len(store.started) == 1
    with store.factory() as db:
        result = receipts.read_request(db, store.pid, "original-request-key")
    assert result["task_id"]
    assert all(r["task_id"] in {None, result["task_id"]} for r in results)


def test_crash_after_task_commit_is_exactly_recovered_without_new_task(submitted, monkeypatch):
    store, calls = submitted
    original = job.async_tasks.create_async_task_once
    def commit_then_lose_response(*a, **kw):
        original(*a, **kw)
        raise RuntimeError("response lost after commit")
    monkeypatch.setattr(job.async_tasks, "create_async_task_once", commit_then_lose_response)
    first = submit(store)
    assert first["task_id"] and first["task_status"] == "queued"
    assert submit(store) == first
    assert len(calls) == 1 and not store.started
    with store.factory() as db:
        assert len(list(db.scalars(select(AsyncTaskRecord)))) == 1


def test_dispatch_failure_is_not_requeued_by_replay_or_get(submitted, monkeypatch):
    store, calls = submitted
    monkeypatch.setattr(job.async_tasks, "_start_worker", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    first = submit(store)
    assert first["task_status"] == "failed"
    assert submit(store) == first
    assert len(calls) == 1


def test_receipt_before_task_creation_keeps_exact_intent_and_read_is_pure(submitted, monkeypatch):
    store, calls = submitted
    monkeypatch.setattr(job.async_tasks, "create_async_task_once", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("before commit")))
    first = submit(store)
    assert first["task_id"] and first["dispatch_status"] == "WAITING_RETRY"
    with store.factory() as db:
        before = db.execute(select(ReconciliationRecheckRequest)).scalar_one().result_json
        for _ in range(3):
            assert routes.recover_source_request(store.pid, first["request_key"], db, "tester") == first
        assert not db.dirty and not db.new
        assert db.execute(select(ReconciliationRecheckRequest)).scalar_one().result_json == before
    assert submit(store) == first and len(calls) == 1


def test_no_latest_task_guess_or_cross_portfolio_lookup(submitted):
    store, _ = submitted
    first = submit(store)
    with store.factory() as db:
        db.add(AsyncTaskRecord(id="f"*32, task_type="portfolio_reconciliation", status="done", stage="done", payload_json="{}"))
        db.commit()
        assert receipts.read_request(db, store.pid, first["request_key"])["task_id"] == first["task_id"]
        assert receipts.read_request(db, store.pid+1, first["request_key"]) is None
        with pytest.raises(routes.HTTPException) as exc:
            routes.recover_source_request(store.pid, "not-found-key", db, "tester")
        assert exc.value.status_code == 404


def test_reused_plan_task_also_gets_exact_association(submitted):
    store, _ = submitted
    first = submit(store)
    second = submit(store, key="second-explicit-key")
    assert first["task_id"] == second["task_id"]
    assert len(store.started) == 1


def test_registration_error_rolls_back_receipt_and_does_not_dispatch(submitted, monkeypatch):
    store, _ = submitted
    monkeypatch.setattr(job, "request_reconciliation_check", lambda *a, **kw: (_ for _ in ()).throw(ValueError("registration rejected")))
    with pytest.raises(routes.HTTPException):
        submit(store)
    with store.factory() as db:
        assert receipts.read_request(db, store.pid, "original-request-key") is None
    assert not store.started


def test_read_and_write_both_enforce_portfolio_authorization(submitted, monkeypatch):
    store, _ = submitted
    def deny(*a):
        raise routes.HTTPException(403, "denied")
    monkeypatch.setattr(routes, "_authorize", deny)
    with pytest.raises(routes.HTTPException) as exc:
        submit(store)
    assert exc.value.status_code == 403
    with store.factory() as db, pytest.raises(routes.HTTPException) as exc:
        routes.recover_source_request(store.pid, "original-request-key", db, "tester")
    assert exc.value.status_code == 403 and not store.started


def test_append_only_provenance_does_not_change_request_or_release(submitted):
    from app.models.portfolio_inspection_workflow import ReconciliationRequestAnnotation
    store, _ = submitted
    first = submit(store)
    with store.factory() as db:
        request = db.get(ReconciliationRecheckRequest, receipts.identity(store.pid, first['request_key']))
        original = (request.request_hash, request.result_json, request.task_id, request.created_by, request.created_at)
        note = ReconciliationRequestAnnotation(id='test-provenance', request_id=request.id,
            source_code='ACCEPTANCE_ROUTE_TEARDOWN_LEAK', note='isolated acceptance origin',
            evidence_ref='isolated-test-evidence', annotated_by='tester')
        db.add(note);db.commit()
        recovered=receipts.read_request(db,store.pid,first['request_key'])
        assert recovered['provenance_annotations'][0]['label']=='验收产生的请求关联'
        assert recovered['reconciliation_passed'] is False and recovered['trading_resumed'] is False
        assert original==(request.request_hash,request.result_json,request.task_id,request.created_by,request.created_at)
        assert not db.dirty and not db.new
        note.note='rewrite'
        with pytest.raises(ValueError):db.commit()
        db.rollback()
