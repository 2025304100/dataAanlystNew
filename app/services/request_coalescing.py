"""Share only overlapping identical reads; completed responses are never cached."""
from concurrent.futures import Future, TimeoutError
from copy import deepcopy
import json
import re
from threading import Lock
from typing import Callable, TypeVar
from app.services.sync_progress import cancellation_requested

T = TypeVar("T")
_lock = Lock()
_pending: dict[str, Future] = {}


def coalesced_read(key: str, read: Callable[[], T], *, should_cancel=None) -> T:
    """Each waiter can cancel independently; only the leader performs I/O."""
    if cancellation_requested(should_cancel, fresh=True):
        raise InterruptedError("同步已取消")
    with _lock:
        future = _pending.get(key)
        leader = future is None
        if leader:
            future = Future()
            _pending[key] = future
    if leader:
        try:
            result = read()
            future.set_result(result)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with _lock:
                if _pending.get(key) is future:
                    _pending.pop(key)
    while True:
        if should_cancel and should_cancel():
            raise InterruptedError("同步已取消")
        try:
            return deepcopy(future.result(timeout=.25))
        except TimeoutError:
            # A provider may itself raise TimeoutError. Do not mistake that
            # completed failure for a pending future and wait forever.
            if future.done():
                return deepcopy(future.result())


def request_key(api_key: str, url: str, params: dict) -> str:
    """Scope includes every filter/page/column; code order has no meaning."""
    normalized = dict(params)
    if "secids" in normalized:
        normalized["secids"] = ",".join(sorted(normalized["secids"].split(",")))
    if isinstance(normalized.get("filter"), str):
        normalized["filter"] = re.sub(r'\bin \(([^()]*)\)',
            lambda match: "in (" + ",".join(sorted(part.strip() for part in match[1].split(","))) + ")",
            normalized["filter"])
    return json.dumps([api_key, url, normalized], sort_keys=True, separators=(",", ":"))
