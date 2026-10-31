"""Task and adapter entry points for the shared source request controller.

Opt-in is explicit: a configured store must already exist. No policies, source
admissions or legacy settings are silently imported or enabled here.
"""
from contextlib import contextmanager
from contextvars import copy_context, ContextVar
from functools import wraps
import os
from pathlib import Path
import uuid

_inline_choice = ContextVar('source_inline_choice', default=None)
_business_origin = ContextVar('source_business_origin', default=None)


@contextmanager
def business_task_context(task_id):
    """Lazy origin for generic workers; jobs without data I/O open no store."""
    from app.db.session import get_session_local
    token = _business_origin.set((task_id, get_session_local()))
    try:
        yield
    finally:
        _business_origin.reset(token)


def _origin_options(origin):
    import json
    from app.models.async_task import AsyncTaskRecord
    from app.services.async_tasks import is_worker_stop_requested
    from app.services.data_source_control import business_control_io, PolicyDenied
    task_id, factory = origin
    with factory() as db:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            raise PolicyDenied('TASK_UNCONFIGURED')
        payload = json.loads(task.payload_json or '{}')
        kind = task.task_type
    lane = 'backfill' if payload.get('mode') == 'backfill' or any(
        word in str(kind) for word in ('backfill','history','repair','discovery')) else 'incremental'
    def cancelled():
        if is_worker_stop_requested(task_id): return True
        with business_control_io(), factory() as db:
            current = db.get(AsyncTaskRecord, task_id)
            return current is None or bool(current.cancel_requested) or current.status in {'done','partial','failed','cancelled'}
    return lane, cancelled


@contextmanager
def data_task(*, lane='incremental', task_id=None, max_requests=10000, timeout_s=7200, cancelled=None):
    from app.services.data_source_control import current_controller, PolicyDenied, RequestController
    if os.environ.get('DATA_SOURCE_EXECUTION_MODE', 'controlled') not in {'controlled', 'production'}:
        raise PolicyDenied('EXECUTION_MODE_INVALID')
    origin = _business_origin.get()
    inherited = task_id is None and origin is not None
    if inherited:
        task_id = origin[0]
    try:
        active = current_controller()
    except PolicyDenied as exc:
        if str(exc) != 'TASK_CONTEXT_REQUIRED':
            raise
        active = None
    if active is not None:
        if task_id is not None and task_id != active.task_identity():
            raise PolicyDenied('CONTROL_CONTEXT_MISMATCH')
        lanes = ('probe',) if lane == 'probe' else ('incremental','backfill')
        with active.scope(active.task_identity(), cancelled=cancelled, allowed_lanes=lanes):
            yield active
        return
    path = os.environ.get('DATA_SOURCE_CONTROL_DB')
    if not path:
        if os.environ.get('DATA_SOURCE_EXECUTION_MODE', 'controlled') == 'production':
            raise PolicyDenied('PRODUCTION_CONTROL_REQUIRED')
        yield None
        return
    if not Path(path).is_file():
        raise PolicyDenied('CONTROL_STORE_UNAVAILABLE')
    if inherited:
        lane, origin_cancelled = _origin_options(origin)
        supplied = cancelled
        cancelled = lambda: origin_cancelled() or bool(supplied and supplied())
    controller = RequestController(path)
    task_id = task_id or 'source:' + uuid.uuid4().hex
    try:
        controller.start_task(task_id, lane=lane, max_requests=max_requests, timeout_s=timeout_s)
    except PolicyDenied as exc:
        if str(exc) != 'TASK_ALREADY_EXISTS':
            raise
    with controller.scope(task_id, cancelled=cancelled, allowed_lanes=(lane,)):
        yield controller


def source_task(*, lane='incremental', max_requests=10000, timeout_s=7200):
    def decorate(func):
        @wraps(func)
        def run(*args, **kwargs):
            cancelled = kwargs.get('is_cancelled') or kwargs.get('should_cancel')
            effective_lane = 'backfill' if kwargs.get('mode') == 'backfill' else lane
            with data_task(lane=effective_lane, max_requests=max_requests, timeout_s=timeout_s, cancelled=cancelled):
                return func(*args, **kwargs)
        return run
    return decorate


def submit_with_context(executor, func, *args, **kwargs):
    """Every submitted job owns a copy; contexts cannot be entered concurrently."""
    return executor.submit(copy_context().run, func, *args, **kwargs)


def source_boundary(endpoint):
    """Protect a direct adapter as well as its wrapped callers, exactly once."""
    def decorate(func):
        @wraps(func)
        def run(*args, **kwargs):
            key = endpoint(*args, **kwargs) if callable(endpoint) else endpoint
            from app.services.data_source_control import current_controller
            controller = current_controller()
            if controller is not None:
                return execute_source(controller, key, func, *args, **kwargs)
            if os.environ.get('DATA_SOURCE_EXECUTION_MODE') == 'production':
                from app.services.data_source_control import PolicyDenied
                raise PolicyDenied('PRODUCTION_CONTROL_REQUIRED')
            from app.services.akshare_utils import assert_akshare_api_enabled
            assert_akshare_api_enabled(key)
            return func(*args, **kwargs)
        return run
    return decorate


def raise_if_control_error(exc):
    from app.services.data_source_control import PolicyDenied
    from app.services.data_contract_errors import DataContractError
    if isinstance(exc, (PolicyDenied, DataContractError)):
        raise exc


def execute_source(controller, endpoint, func, *args, **kwargs):
    choice = _inline_choice.get()
    if choice is None or choice['active'] == endpoint:
        return controller.execute(endpoint, func, *args, **kwargs)
    # The previous returned candidate was not accepted by its normalizer if
    # the caller is now attempting another endpoint.
    if choice['pending'] is not None:
        controller.observe_source('failed', choice['pending'])
        choice['pending'] = None
    controller.observe_source('considered', endpoint)
    controller.observe_source('attempted', endpoint)
    previous = choice['active']
    choice['active'] = endpoint
    try:
        result = controller.execute(endpoint, func, *args, **kwargs)
    except BaseException as exc:
        raise_if_control_error(exc)
        controller.observe_source('failed', endpoint)
        raise
    else:
        from app.services.data_source_control import result_is_empty
        if result_is_empty(result):
            controller.observe_source('unavailable', endpoint)
        else:
            choice['pending'] = endpoint
        return result
    finally:
        choice['active'] = previous


def source_selection(func):
    @wraps(func)
    def run(*args, **kwargs):
        with data_task() as controller:
            if controller is None or _inline_choice.get() is not None:
                return func(*args, **kwargs)
            choice = {'active': None, 'pending': None}
            token = _inline_choice.set(choice)
            try:
                with controller.selection(require_selected=True):
                    result = func(*args, **kwargs)
                    if choice['pending'] is not None:
                        controller.observe_source('selected', choice['pending'])
                    return result
            finally:
                _inline_choice.reset(token)
    return run


def source_worker(func):
    """Attach a persisted business task, including fresh cancellation checks."""
    @wraps(func)
    def run(task_id, *args, **kwargs):
        if not os.environ.get('DATA_SOURCE_CONTROL_DB') and os.environ.get('DATA_SOURCE_EXECUTION_MODE') != 'production':
            return func(task_id, *args, **kwargs)
        import json
        from app.db.session import get_session_local
        from app.models.async_task import AsyncTaskRecord
        from app.services.async_tasks import is_worker_stop_requested, _set_task, _now
        factory = get_session_local()
        TaskModel = AsyncTaskRecord
        setter = _set_task
        if func.__module__.endswith('discovery_tasks'):
            from app.models.discovery import DiscoveryTaskRecord
            from app.services.discovery_tasks import _set_task as discovery_setter
            TaskModel, setter = DiscoveryTaskRecord, discovery_setter
        with factory() as db:
            task = db.get(TaskModel, task_id)
            payload = json.loads(task.payload_json or '{}') if task else {}
        lane = 'backfill' if payload.get('mode') == 'backfill' or any(
            word in func.__name__ for word in ('backfill', 'history_initialization', 'range_repair', 'sync_init', 'smart_sync', 'discovery', 'download')) else 'incremental'
        def cancelled():
            if is_worker_stop_requested(task_id):
                return True
            from app.services.data_source_control import business_control_io
            with business_control_io(), factory() as db:
                current = db.get(TaskModel, task_id)
                return current is None or bool(getattr(current, 'cancel_requested', 0)) or current.status in {'done', 'partial', 'cancelled', 'failed', 'paused'}
        try:
            with data_task(task_id=task_id, lane=lane, cancelled=cancelled):
                return func(task_id, *args, **kwargs)
        except Exception as exc:
            from app.services.data_source_control import PolicyDenied
            if not isinstance(exc, PolicyDenied):
                raise
            with factory() as db:
                current = db.get(TaskModel, task_id)
                if current and current.status not in {'done', 'partial', 'failed', 'cancelled'}:
                    setter(db, task_id, status='cancelled' if str(exc) == 'CANCELLED' else 'failed',
                              stage='cancelled' if str(exc) == 'CANCELLED' else 'failed', percent=100,
                              message='Data source control: ' + str(exc), finished_at=_now())
            return None
    return run
