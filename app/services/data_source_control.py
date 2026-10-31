"""Shared, fail-closed request admission. The store is operational, not a fact DB.

One SQLite file must be shared by all processes on a single host. Callers opt in
with ``scope``; deployment-wide opt-in additionally requires task context. No
configuration is imported from the legacy permissive cache.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable
import uuid
from weakref import WeakValueDictionary
from app.services import data_source_statistics as observations


class PolicyDenied(RuntimeError):
    """A local policy rejection, never a supplier failure."""


_store_locks = WeakValueDictionary()
_store_locks_guard = threading.Lock()


def _transaction_lock(path):
    # SQLite remains the cross-process authority. Serialize local contenders
    # before BEGIN so a busy writer cannot repeatedly starve sibling threads.
    with _store_locks_guard:
        key = os.path.normcase(path)
        lock = _store_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _store_locks[key] = lock
        return lock


@lru_cache(maxsize=1)
def _declaration_scopes():
    catalog = json.loads(Path(__file__).with_name("data_source_contract_catalog.json").read_text(encoding="utf-8"))
    return {item["endpoint"]: (item["in_current_release"], item["implementation_status"] == "implemented") for item in catalog["items"]}


def validate_upstream_scope(hosts):
    # AkShare's A-share name aggregator calls its BSE helper internally. The
    # upstream remains PLAN:BSE even when the outer endpoint has another key.
    bse = _declaration_scopes().get('PLAN:BSE', (False, False))
    if not all(bse) and any(str(host).lower().rstrip('.') == 'bse.cn' or str(host).lower().rstrip('.').endswith('.bse.cn') for host in hosts):
        raise PolicyDenied('UPSTREAM_OUT_OF_RELEASE')


@dataclass(frozen=True)
class RequestPolicy:
    enabled: bool = True
    min_interval_s: float = 0.0
    max_attempts: int = 3
    timeout_s: float = 30.0
    failure_threshold: int = 3
    cooldown_s: float = 60.0
    max_cooldown_s: float = 900.0
    probe_lease_s: float = 30.0
    window_s: float = 60.0
    total_limit: int | None = None
    incremental_limit: int | None = None
    backfill_limit: int | None = None
    probe_limit: int | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be bool")
        for name in ("min_interval_s", "timeout_s", "cooldown_s", "max_cooldown_s", "probe_lease_s", "window_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(name)
            if value < 0 or (name != "min_interval_s" and value == 0):
                raise ValueError(name)
        for name in ("max_attempts", "failure_threshold", "total_limit", "incremental_limit", "backfill_limit", "probe_limit"):
            value = getattr(self, name)
            if value is None and name.endswith("limit"):
                continue
            if type(value) is not int or value < (0 if name.endswith("limit") else 1):
                raise ValueError(name)
        if self.max_cooldown_s < self.cooldown_s:
            raise ValueError("max_cooldown_s < cooldown_s")


class RequestController:
    def __init__(self, path, *, clock=time.time, sleep=time.sleep):
        self.path = str(Path(path).resolve())
        self.clock, self.sleep = clock, sleep
        self._transaction_lock = _transaction_lock(self.path)
        with self._db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "request_control_meta" not in tables:
                raise PolicyDenied("CONTROL_STORE_IDENTITY")
            db.execute("CREATE TABLE IF NOT EXISTS request_control_meta (version INTEGER NOT NULL)")
            row = db.execute("SELECT version FROM request_control_meta").fetchone()
            if row is None:
                db.execute("INSERT INTO request_control_meta VALUES (1)")
            elif row[0] != 1:
                raise PolicyDenied("CONTROL_STORE_VERSION")
            db.execute("CREATE TABLE IF NOT EXISTS policies (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS request_policy_versions (key TEXT NOT NULL,version INTEGER NOT NULL,body TEXT NOT NULL,changed_at REAL NOT NULL,origin TEXT NOT NULL,PRIMARY KEY(key,version))")
            db.execute("CREATE TABLE IF NOT EXISTS source_admissions (key TEXT PRIMARY KEY,body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS source_admission_versions (key TEXT NOT NULL,version INTEGER NOT NULL,body TEXT NOT NULL,changed_at REAL NOT NULL,PRIMARY KEY(key,version))")
            for key, body in db.execute("SELECT key,body FROM policies").fetchall():
                current = json.loads(body)
                db.execute("INSERT OR IGNORE INTO request_policy_versions VALUES (?,?,?,?,?)",
                           (key, current["version"], body, self.clock(), "import_current_only"))
            observations.initialize(db)

    @contextmanager
    def _db(self):
        db = None
        if not self._transaction_lock.acquire(timeout=2.0):
            raise PolicyDenied('CONTROL_STORE_UNAVAILABLE')
        try:
            db = sqlite3.connect(self.path, timeout=0.5)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise PolicyDenied("CONTROL_STORE_UNAVAILABLE") from exc
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()
            self._transaction_lock.release()

    @staticmethod
    def _get(db, table, key):
        row = db.execute(f"SELECT body FROM {table} WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(db, table, key, body):
        db.execute(f"INSERT OR REPLACE INTO {table} (key,body) VALUES (?,?)", (key, json.dumps(body, allow_nan=False)))

    def _configure(self, key, policy, expected_version, **metadata):
        if not isinstance(policy, RequestPolicy):
            raise ValueError("RequestPolicy required")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version")
        with self._db() as db:
            return self._write_configuration(db, key, policy, expected_version, metadata, "publish")

    def _write_configuration(self, db, key, policy, expected_version, metadata, origin):
        old = self._get(db, "policies", key)
        version = old["version"] if old else 0
        if version != expected_version:
            raise PolicyDenied("CONFIG_VERSION_CONFLICT")
        if key.startswith('endpoint:') and policy.enabled:
            validate_upstream_scope(metadata.get('allowed_hosts', ()))
        body = dict(policy=asdict(policy), version=version + 1, **metadata)
        self._put(db, "policies", key, body)
        db.execute("INSERT INTO request_policy_versions VALUES (?,?,?,?,?)",
                   (key, version + 1, json.dumps(body, allow_nan=False), self.clock(), origin))
        state = self._get(db, "state", key)
        if state:
            state["generation"] += 1
            state["lease"] = None
            self._put(db, "state", key, state)
        return version + 1

    def patch_policy(self, endpoint, *, scope, expected_endpoint_version, expected_provider_version, patch=None, restore_version=None):
        """Publish one policy and its audit atomically; never dual-write business DB."""
        self._assert_endpoint_allowed(endpoint, enabled=True)
        if scope not in {"endpoint", "provider"}:
            raise ValueError("scope")
        if type(expected_endpoint_version) is not int or type(expected_provider_version) is not int:
            raise ValueError("expected versions")
        with self._db() as db:
            ep = self._get(db, "policies", "endpoint:" + endpoint)
            provider = self._get(db, "policies", "provider:" + ep["provider"]) if ep else None
            if not ep or not provider:
                raise PolicyDenied("ENDPOINT_UNCONFIGURED")
            if (ep["version"], provider["version"]) != (expected_endpoint_version, expected_provider_version):
                raise PolicyDenied("CONFIG_VERSION_CONFLICT")
            key = "endpoint:" + endpoint if scope == "endpoint" else "provider:" + ep["provider"]
            current = ep if scope == "endpoint" else provider
            if restore_version is not None:
                if patch or type(restore_version) is not int or restore_version < 1:
                    raise ValueError("restore_version")
                row = db.execute("SELECT body FROM request_policy_versions WHERE key=? AND version=?", (key, restore_version)).fetchone()
                if row is None:
                    raise ValueError("history version not found")
                policy = RequestPolicy(**json.loads(row[0])["policy"])
                origin = f"restore:{restore_version}"
            else:
                if not isinstance(patch, dict) or not patch or set(patch) - set(current["policy"]):
                    raise ValueError("unsupported policy fields")
                policy = RequestPolicy(**{**current["policy"], **patch})
                origin = "publish"
            return self._write_configuration(db, key, policy, current["version"],
                {k: v for k, v in current.items() if k not in {"policy", "version"}}, origin)

    def configuration_history(self, endpoint, *, scope="endpoint"):
        if scope not in {"endpoint", "provider"}:
            raise ValueError("scope")
        with self._db() as db:
            ep = self._get(db, "policies", "endpoint:" + endpoint)
            if not ep:
                raise PolicyDenied("ENDPOINT_UNCONFIGURED")
            key = "endpoint:" + endpoint if scope == "endpoint" else "provider:" + ep["provider"]
            return [{"version": v, "configuration": json.loads(b), "changed_at": t, "origin": o}
                    for v, b, t, o in db.execute("SELECT version,body,changed_at,origin FROM request_policy_versions WHERE key=? ORDER BY version DESC", (key,))]

    def set_provider(self, provider, policy, *, expected_version=0):
        self._name(provider)
        return self._configure("provider:" + provider, policy, expected_version)

    def set_endpoint(self, endpoint, provider, policy, *, transport="callable", allowed_hosts=(), expected_version=0):
        self._name(endpoint)
        self._name(provider)
        if transport not in {"callable", "requests", "sdk"}:
            raise ValueError("transport")
        if transport in {"requests", "sdk"} and not allowed_hosts:
            raise ValueError("explicit allowed_hosts required")
        if any(not isinstance(h, str) or not h or "/" in h or ":" in h or "*" in h for h in allowed_hosts):
            raise ValueError("exact host names required")
        self._assert_endpoint_allowed(endpoint, enabled=policy.enabled)
        return self._configure("endpoint:" + endpoint, policy, expected_version, provider=provider,
                               transport=transport, allowed_hosts=list(allowed_hosts))

    @staticmethod
    def _assert_endpoint_allowed(endpoint, *, enabled):
        # Declaration scope is not evidence of unit quality or production admission.
        declaration = _declaration_scopes().get(endpoint)
        if not declaration or not declaration[0]:
            raise PolicyDenied("ENDPOINT_OUT_OF_RELEASE")
        if enabled and not declaration[1]:
            raise PolicyDenied("IMPLEMENTATION_NOT_READY")

    @staticmethod
    def _name(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError("nonempty identifier required")

    def start_task(self, task_id, *, lane, max_requests, timeout_s):
        self._name(task_id)
        if lane not in {"incremental", "backfill", "probe"} or type(max_requests) is not int or max_requests < 1:
            raise ValueError("task lane/budget")
        if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("task timeout")
        with self._db() as db:
            if self._get(db, "state", "task:" + task_id):
                raise PolicyDenied("TASK_ALREADY_EXISTS")
            self._put(db, "state", "task:" + task_id, dict(lane=lane, max_requests=max_requests,
                      timeout_s=timeout_s, deadline=self.clock() + timeout_s, used=0, cancelled=False))

    def cancel_task(self, task_id):
        with self._db() as db:
            task = self._get(db, "state", "task:" + task_id)
            if not task:
                raise PolicyDenied("TASK_UNCONFIGURED")
            task["cancelled"] = True
            self._put(db, "state", "task:" + task_id, task)

    def task_identity(self):
        context, op = _scope.get(), _operation.get()
        if context and context[0].path == self.path:
            return context[1]
        if op and op.controller.path == self.path:
            return op.task_id
        raise PolicyDenied('TASK_CONTEXT_REQUIRED')

    def checkpoint(self, *, task_id=None, endpoints=(), inspect_operations=True):
        """Parent/child SDK and local readers recheck without granting a request."""
        task_id = task_id or self.task_identity()
        context = _scope.get()
        if context and context[2] and context[2]():
            raise PolicyDenied('CANCELLED')
        with self._db() as db:
            task = self._get(db, 'state', 'task:' + task_id)
            if not task or task['cancelled']:
                raise PolicyDenied('CANCELLED' if task else 'TASK_UNCONFIGURED')
            if self.clock() >= task['deadline']:
                raise PolicyDenied('DEADLINE')
            for endpoint in endpoints:
                self._load(db, endpoint, task_id)
            running = db.execute("SELECT body FROM request_calls WHERE json_extract(body,'$.task_id')=? AND json_extract(body,'$.status')='running'", (task_id,)) if inspect_operations else ()
            for (body,) in running:
                call = json.loads(body)
                ep, provider, _ = self._load(db, call['endpoint'], task_id)
                if call['deadline'] is not None and self.clock() >= call['deadline']:
                    raise PolicyDenied('DEADLINE')
                if call['config_version'] != {'endpoint': ep['version'], 'provider': provider['version']}:
                    raise PolicyDenied('CONFIG_CHANGED')
            return task['deadline']

    def snapshot(self):
        with self._db() as db:
            return {table: {key: json.loads(body) for key, body in db.execute(f"SELECT key,body FROM {table}")}
                    for table in ("policies", "state")}

    def configuration(self, endpoint):
        """Return detached, authoritative policy versions, including disabled ones."""
        with self._db() as db:
            ep = self._get(db, "policies", "endpoint:" + endpoint)
            if not ep:
                raise PolicyDenied("ENDPOINT_UNCONFIGURED")
            provider = self._get(db, "policies", "provider:" + ep["provider"])
            if not provider:
                raise PolicyDenied("PROVIDER_UNCONFIGURED")
            return {"endpoint": ep, "provider": provider}

    def statistics(self, *, endpoint=None, task_id=None, kind=None, include_records=False):
        with self._db() as db:
            return observations.statistics(db, endpoint=endpoint, task_id=task_id, kind=kind, include_records=include_records)

    def reject(self, endpoint, reason):
        """Persist a preflight policy denial without granting a wire permit."""
        if reason not in {'DISABLED', 'ENDPOINT_OUT_OF_RELEASE', 'IMPLEMENTATION_NOT_READY'}:
            raise ValueError('known policy reason required')
        task_id = self.task_identity()
        selection = _selection.get()
        parent = _operation.get()
        with self._db() as db:
            task = self._get(db, 'state', 'task:' + task_id)
            ep = self._get(db, 'policies', 'endpoint:' + endpoint)
            provider = self._get(db, 'policies', 'provider:' + ep['provider']) if ep else None
            kind = 'probe' if task and task['lane'] == 'probe' else 'call'
            call_id = observations.begin(db, endpoint, task_id, kind, parent.call_id if parent else None,
                self.clock(), selection[1] if selection and selection[0] == self.path else None)
            if ep and provider:
                observations.bind(db, call_id, provider=ep['provider'], lane=task['lane'] if task else None,
                    versions=(ep['version'],provider['version']), deadline=task['deadline'] if task else self.clock())
            observations.finish(db,call_id,self.clock(),'policy_denied',{'error_code':reason,'error_type':'PolicyDenied'})
        raise PolicyDenied(reason)

    def publish_admission(self, endpoint, record, *, expected_version=0):
        """Offline reviewed receipt publication; never exposed by execution-policy UI.

        The publisher owns review of the referenced artifacts. Runtime checks
        their content hashes and expiry; this method does not perform that review.
        """
        from app.services.data_source_admission import prepare
        self._assert_endpoint_allowed(endpoint, enabled=True)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('expected_version')
        value = prepare(record)
        with self._db() as db:
            previous = self._get(db, 'source_admissions', endpoint)
            if (previous['version'] if previous else 0) != expected_version:
                raise PolicyDenied('ADMISSION_VERSION_CONFLICT')
            publication = {'version': expected_version + 1, 'record': value}
            self._put(db, 'source_admissions', endpoint, publication)
            db.execute('INSERT INTO source_admission_versions VALUES (?,?,?,?)',
                       (endpoint, publication['version'], json.dumps(publication, allow_nan=False), self.clock()))
            return publication['version']

    def admission(self, endpoint):
        from app.services.data_source_admission import decision
        with self._db() as db:
            publication = self._get(db, 'source_admissions', endpoint)
            reasons = decision(endpoint, publication, self.clock())
            return {'publication': publication, 'production_request_allowed': not reasons, 'block_reasons': reasons}

    def management_snapshot(self):
        with self._db() as db:
            return {"policies": {k: json.loads(b) for k, b in db.execute("SELECT key,body FROM policies")},
                    "admissions": {k: json.loads(b) for k, b in db.execute("SELECT key,body FROM source_admissions")},
                    "state": {k: json.loads(b) for k, b in db.execute("SELECT key,body FROM state WHERE key LIKE 'provider:%' OR key LIKE 'endpoint:%'")},
                    "statistics": observations.management_summary(db), "observed_at": self.clock()}

    def reconcile_expired(self):
        with self._db() as db:
            return observations.reconcile_expired(db, self.clock())

    def interrupt_task(self, task_id):
        """Stop admissions and finalize uncertain observations; does not kill code."""
        with self._db() as db:
            task = self._get(db, "state", "task:" + task_id)
            if not task:
                raise PolicyDenied("TASK_UNCONFIGURED")
            task["cancelled"] = True
            self._put(db, "state", "task:" + task_id, task)
            return observations.interrupt_task(db, task_id, self.clock())

    @contextmanager
    def selection(self, *, require_selected=False):
        context, op = _scope.get(), _operation.get()
        task_id = context[1] if context and context[0].path == self.path else op.task_id if op else None
        with self._db() as db:
            task = self._get(db, "state", "task:" + task_id) if task_id else None
            if not task:
                raise PolicyDenied("TASK_UNCONFIGURED")
            if task["cancelled"] or (context and context[2] and context[2]()):
                raise PolicyDenied("CANCELLED")
            if self.clock() >= task["deadline"]:
                raise PolicyDenied("DEADLINE")
            selection_id = observations.begin_selection(db, task_id, "probe" if task["lane"] == "probe" else "call",
                                                        min(task["deadline"], op.deadline if op else task["deadline"]), self.clock())
        token = _selection.set((self.path, selection_id))
        try:
            yield selection_id
        except BaseException as exc:
            with self._db() as db:
                category, details = observations.error_details(exc)
                observations.finish_selection(db, selection_id, self.clock(), "failed", {"error_status": category,
                    "error_code": details["error_code"], "error_type": details["error_type"]})
            raise
        else:
            with self._db() as db:
                record = observations.get(db, 'request_selections', selection_id)
                status = 'empty_result' if require_selected and record['selected_source'] is None else 'success'
                if not observations.finish_selection(db, selection_id, self.clock(), status):
                    raise PolicyDenied("OBSERVATION_CLOSED")
        finally:
            _selection.reset(token)

    def observe_source(self, event, source):
        self._name(source)
        selection = _selection.get()
        if selection is None or selection[0] != self.path:
            raise PolicyDenied("CONTROL_CONTEXT_MISMATCH")
        with self._db() as db:
            row = observations.get(db, "request_selections", selection[1])
            task = self._get(db, "state", "task:" + row["task_id"])
            context = _scope.get()
            if task["cancelled"] or (context and context[2] and context[2]()):
                raise PolicyDenied("CANCELLED")
            if self.clock() >= row["deadline"]:
                raise PolicyDenied("DEADLINE")
            if not observations.source_event(db, selection[1], event, source):
                raise PolicyDenied("OBSERVATION_CLOSED")

    def _load(self, db, endpoint, task_id):
        ep = self._get(db, "policies", "endpoint:" + endpoint)
        if ep is None:
            raise PolicyDenied("ENDPOINT_UNCONFIGURED")
        self._assert_endpoint_allowed(endpoint, enabled=True)
        provider = self._get(db, "policies", "provider:" + ep["provider"])
        if provider is None:
            raise PolicyDenied("PROVIDER_UNCONFIGURED")
        if not ep["policy"]["enabled"] or not provider["policy"]["enabled"]:
            raise PolicyDenied("DISABLED")
        validate_upstream_scope(ep['allowed_hosts'])
        task = self._get(db, "state", "task:" + task_id) if task_id else None
        if task is None:
            raise PolicyDenied("TASK_UNCONFIGURED")
        if task["cancelled"]:
            raise PolicyDenied("CANCELLED")
        mode = os.environ.get('DATA_SOURCE_EXECUTION_MODE', 'controlled')
        if mode not in {'controlled', 'production'}:
            raise PolicyDenied('EXECUTION_MODE_INVALID')
        if mode == 'production' and task['lane'] != 'probe':
            from app.services.data_source_admission import decision
            if decision(endpoint, self._get(db, 'source_admissions', endpoint), self.clock()):
                raise PolicyDenied('PRODUCTION_NOT_ADMITTED')
        return ep, provider, task

    def _check(self, db, op):
        if op.cancelled and op.cancelled():
            raise PolicyDenied("CANCELLED")
        ep, provider, task = self._load(db, op.endpoint, op.task_id)
        if observations.get(db, "request_calls", op.call_id)["status"] != "running":
            raise PolicyDenied("OBSERVATION_CLOSED")
        if (ep["version"], provider["version"]) != op.versions:
            raise PolicyDenied("CONFIG_CHANGED")
        if self.clock() >= min(op.deadline, task["deadline"]):
            raise PolicyDenied("DEADLINE")
        return ep, provider, task

    def _wait(self, op, delay):
        until = self.clock() + delay
        while self.clock() < until:
            with self._db() as db:
                self._check(db, op)
            self.sleep(min(.05, max(0, until - self.clock()), max(0, op.deadline - self.clock())))

    @staticmethod
    def _initial_state(now):
        return dict(next_at=0, failures=0, open_until=0, opens=0, generation=0, lease=None,
                    lease_until=0, window_at=now, used=0, lanes={})

    def _acquire(self, op):
        while True:
            with self._db() as db:
                ep, provider, task = self._check(db, op)
                now = self.clock()
                if task["used"] >= task["max_requests"]:
                    raise PolicyDenied("TASK_QUOTA")
                keys = ("provider:" + ep["provider"], "endpoint:" + op.endpoint)
                prepared, wait = [], 0
                for key, config in zip(keys, (provider, ep)):
                    p = config["policy"]
                    s = self._get(db, "state", key) or self._initial_state(now)
                    if now < s["window_at"]:
                        raise PolicyDenied("CLOCK_REWIND")
                    if now - s["window_at"] >= p["window_s"]:
                        s.update(window_at=now, used=0, lanes={})
                    lane_limit = p[task["lane"] + "_limit"]
                    # Incremental/probe allocations are reserved. Backfill is
                    # opportunistic and cannot borrow the unused reservation.
                    reserved = sum(max(0, (p[lane + "_limit"] or 0) - s["lanes"].get(lane, 0))
                                   for lane in ("incremental", "probe") if lane != task["lane"])
                    if (p["total_limit"] is not None and s["used"] >= p["total_limit"] - reserved) or (
                        lane_limit is not None and s["lanes"].get(task["lane"], 0) >= lane_limit):
                        raise PolicyDenied("REQUEST_QUOTA")
                    if s["open_until"] > now:
                        raise PolicyDenied("COOLDOWN")
                    if s["lease"] and s["lease_until"] > now:
                        raise PolicyDenied("HALF_OPEN_BUSY")
                    wait = max(wait, s["next_at"] - now)
                    prepared.append((key, p, s))
                if wait <= 0:
                    permits = []
                    for key, p, s in prepared:
                        if s["open_until"] or s["lease"]:
                            s["generation"] += 1
                            s["lease"] = uuid.uuid4().hex
                            s["lease_until"] = now + p["probe_lease_s"]
                        s["next_at"] = now + p["min_interval_s"]
                        s["used"] += 1
                        s["lanes"][task["lane"]] = s["lanes"].get(task["lane"], 0) + 1
                        self._put(db, "state", key, s)
                        permits.append((key, s["generation"], s["lease"], p))
                    task["used"] += 1
                    self._put(db, "state", "task:" + op.task_id, task)
                    attempt_id = observations.grant(db, op.call_id, op.round_no, op.transport, now)
                    if op.wire_phase:
                        record = observations.get(db, 'request_attempts', attempt_id)
                        record['wire_phase'] = op.wire_phase
                        observations.save(db, 'request_attempts', record)
                    return permits, attempt_id
            self._wait(op, wait)

    def _complete(self, permits, outcome, retry_after=0):
        valid = True
        with self._db() as db:
            now = self.clock()
            for key, generation, lease, p in permits:
                s = self._get(db, "state", key)
                if s["generation"] != generation or s["lease"] != lease:
                    valid = False
                    continue
                if lease and s["lease_until"] <= now:
                    # A timed-out probe cannot reopen a recovered/new generation.
                    valid = False
                    continue
                if outcome == "success":
                    s.update(failures=0, open_until=0, opens=0, lease=None)
                elif outcome in {"failure", "severe"}:
                    s["failures"] += 1
                    if lease or outcome == "severe" or s["failures"] >= p["failure_threshold"]:
                        delay = min(p["max_cooldown_s"], p["cooldown_s"] * (2 ** min(s["opens"], 20)))
                        s.update(open_until=now + max(delay, retry_after), opens=s["opens"] + 1,
                                 generation=s["generation"] + 1, lease=None)
                elif lease:
                    s.update(lease=None, open_until=now)
                self._put(db, "state", key, s)
        return valid

    def _invoke(self, op, func, *args, **kwargs):
        permits, attempt_id = self._acquire(op)
        def start():
            # Admission and actual dispatch may be separated by scheduling or
            # DB contention. Pace dispatch too, so queued grants cannot bunch.
            while True:
                with self._db() as db:
                    self._check(db, op)
                    now, wait, states = self.clock(), 0, []
                    for key, generation, lease, policy in permits:
                        state = self._get(db, 'state', key)
                        if state['generation'] != generation or state['lease'] != lease:
                            raise PolicyDenied('STALE_PERMIT')
                        wait = max(wait, state.get('last_started_at', 0) + policy['min_interval_s'] - now)
                        states.append((key, policy, state))
                    if wait <= 0:
                        for key, policy, state in states:
                            state.update(last_started_at=now, next_at=max(state['next_at'], now + policy['min_interval_s']))
                            self._put(db, 'state', key, state)
                        if not observations.start(db, attempt_id, now):
                            raise PolicyDenied('OBSERVATION_CLOSED')
                        return
                self._wait(op, wait)
        try:
            if op.transport in {"requests", "sdk"}:
                result = func(start)
            else:
                start()
                result = func(*args, **kwargs)
            with self._db() as db:
                self._check(db, op)
        except BaseException as exc:
            outcome, retry_after = failure_kind(exc)
            self._complete(permits, outcome, retry_after)
            status, details = observations.error_details(exc)
            with self._db() as db:
                observations.finish_attempt(db, attempt_id, self.clock(), status, details)
            raise
        outcome = 'neutral' if op.transport == 'sdk' and op.wire_phase == 'connect' else 'success'
        if not self._complete(permits, outcome):
            with self._db() as db:
                observations.finish_attempt(db, attempt_id, self.clock(), "policy_denied", {"error_code": "STALE_PERMIT"})
            raise PolicyDenied("STALE_PERMIT")
        with self._db() as db:
            observations.finish_attempt(db, attempt_id, self.clock())
        return result

    def execute(self, endpoint, func, *args, task_id=None, max_attempts=None, base_delay=1.0,
                timeout_s=None, cancelled=None, observation_kind=None, require_nonempty=False, **kwargs):
        _check_deployment_store(self)
        context, parent = _scope.get(), _operation.get()
        if task_id is None and context:
            task_id = context[1]
            cancelled = cancelled if cancelled is not None else context[2]
        if parent and parent.controller.path == self.path and parent.endpoint == endpoint:
            with self._db() as db:
                if task_id != parent.task_id:
                    raise PolicyDenied("CONTROL_CONTEXT_MISMATCH")
                if observation_kind is not None and observation_kind != observations.get(db, "request_calls", parent.call_id)["kind"]:
                    raise PolicyDenied("OBSERVATION_LANE_MISMATCH")
                self._check(db, parent)
            return func(*args, **kwargs)
        with self._db() as db:
            task = self._get(db, "state", "task:" + task_id) if task_id else None
            kind = observation_kind or ("probe" if task and task["lane"] == "probe" else "call")
            if kind not in {"call", "probe"}:
                raise ValueError("observation_kind")
            selection = _selection.get()
            call_id = observations.begin(db, endpoint, task_id, kind, parent.call_id if parent else None, self.clock(),
                                         selection[1] if selection and selection[0] == self.path else None)
            ep = self._get(db, "policies", "endpoint:" + endpoint)
            provider = self._get(db, "policies", "provider:" + ep["provider"]) if ep else None
            if ep and provider:
                observations.bind(db, call_id, provider=ep["provider"], lane=task["lane"] if task else None,
                                  versions=(ep["version"], provider["version"]),
                                  deadline=min(task["deadline"] if task else float("inf"), self.clock() + 30))
        try:
            result = self._execute(endpoint, func, *args, task_id=task_id, max_attempts=max_attempts,
                                   base_delay=base_delay, timeout_s=timeout_s, cancelled=cancelled, call_id=call_id, **kwargs)
            empty = result_is_empty(result)
        except BaseException as exc:
            status, details = observations.error_details(exc)
            with self._db() as db:
                observations.finish(db, call_id, self.clock(), status, details)
            raise
        with self._db() as db:
            if not observations.finish(db, call_id, self.clock(), "empty_result" if require_nonempty and empty else "success",
                                       result_status="empty" if empty else "returned"):
                raise PolicyDenied("OBSERVATION_CLOSED")
        return result

    def probe(self, endpoint, func, *args, **kwargs):
        return self.execute(endpoint, func, *args, observation_kind="probe", require_nonempty=True, **kwargs)

    def _execute(self, endpoint, func, *args, task_id=None, max_attempts=None, base_delay=1.0,
                 timeout_s=None, cancelled=None, call_id=None, **kwargs):
        _check_deployment_store(self)
        context = _scope.get()
        if context and context[0].path != self.path:
            raise PolicyDenied("CONTROL_CONTEXT_MISMATCH")
        if task_id is None and context:
            task_id = context[1]
            cancelled = cancelled or context[2]
        if max_attempts is not None and (type(max_attempts) is not int or max_attempts < 1):
            raise ValueError("max_attempts")
        if isinstance(base_delay, bool) or not math.isfinite(base_delay) or base_delay < 0:
            raise ValueError("base_delay")
        if timeout_s is not None and (isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s")
        with self._db() as db:
            ep, provider, task = self._load(db, endpoint, task_id)
            if (observations.get(db, "request_calls", call_id)["kind"] == "probe") != (task["lane"] == "probe"):
                raise PolicyDenied("OBSERVATION_LANE_MISMATCH")
        if ep["transport"] == "requests":
            from app.services.akshare_utils import ensure_controlled_http
            ensure_controlled_http()
            if endpoint == 'stock_news_em':
                from app.services.eastmoney_news_source import accounted_supplier
                func = accounted_supplier(endpoint, func)
        deadline = min(task["deadline"], self.clock() + min(ep["policy"]["timeout_s"], provider["policy"]["timeout_s"],
                                                          timeout_s if timeout_s is not None else float("inf")))
        parent = _operation.get()
        if parent:
            if parent.controller.path != self.path:
                raise PolicyDenied("CONTROL_CONTEXT_MISMATCH")
            deadline = min(deadline, parent.deadline)
        op = _Operation(self, endpoint, task_id, (ep["version"], provider["version"]), deadline,
                        cancelled, ep["transport"], tuple(ep["allowed_hosts"]), call_id)
        with self._db() as db:
            observations.bind(db, call_id, provider=ep["provider"], lane=task["lane"], versions=op.versions, deadline=deadline)
        attempts = min(ep["policy"]["max_attempts"], provider["policy"]["max_attempts"], max_attempts or 2**31)
        token = _operation.set(op)
        try:
            for attempt in range(attempts):
                op = replace(op, round_no=attempt + 1)
                _operation.set(op)
                try:
                    if op.transport == "callable":
                        return self._invoke(op, func, *args, **kwargs)
                    with self._db() as db:
                        self._check(db, op)
                        observations.round_started(db, call_id, op.round_no)
                    result = func(*args, **kwargs)
                    with self._db() as db:
                        self._check(db, op)
                    return result
                except Exception as exc:
                    kind, _ = failure_kind(exc)
                    if kind != "failure" or attempt == attempts - 1:
                        raise
                    self._wait(op, base_delay * 2**attempt)
        finally:
            _operation.reset(token)

    @contextmanager
    def scope(self, task_id, *, lane=None, max_requests=None, timeout_s=None, cancelled=None, allowed_lanes=None):
        # Attaching to an existing task never replenishes its budget/deadline.
        if lane is not None:
            if allowed_lanes is not None and lane not in allowed_lanes:
                raise PolicyDenied('TASK_LANE_MISMATCH')
            self.start_task(task_id, lane=lane, max_requests=max_requests, timeout_s=timeout_s)
        else:
            with self._db() as db:
                task = self._get(db, "state", "task:" + task_id)
                if not task:
                    raise PolicyDenied("TASK_UNCONFIGURED")
                if allowed_lanes is not None and task['lane'] not in allowed_lanes:
                    raise PolicyDenied('TASK_LANE_MISMATCH')
        parent = _scope.get()
        if parent and parent[0].path == self.path and parent[1] == task_id and parent[2]:
            if cancelled:
                previous, incoming = parent[2], cancelled
                cancelled = lambda: previous() or incoming()
            else:
                cancelled = parent[2]
        token = _scope.set((self, task_id, cancelled))
        try:
            yield self
        finally:
            _scope.reset(token)


@dataclass(frozen=True)
class _Operation:
    controller: RequestController
    endpoint: str
    task_id: str
    versions: tuple[int, int]
    deadline: float
    cancelled: Callable | None
    transport: str
    allowed_hosts: tuple[str, ...]
    call_id: str
    round_no: int = 1
    wire_phase: str | None = None


_scope = ContextVar("data_source_request_scope", default=None)
_operation = ContextVar("data_source_operation", default=None)
_selection = ContextVar("data_source_selection", default=None)
_physical_wire = ContextVar('data_source_physical_wire', default=None)


@contextmanager
def physical_transport():
    op = _operation.get()
    token = _physical_wire.set(op.call_id if op else None)
    try:
        yield
    finally:
        _physical_wire.reset(token)


@contextmanager
def business_control_io():
    """The fixed task-state callback may open a business DB connection."""
    token = _physical_wire.set('business-control-db')
    try:
        yield
    finally:
        _physical_wire.reset(token)


@contextmanager
def process_local_io():
    """Windows implements the standard socketpair used by event loops via TCP."""
    token = _physical_wire.set('process-local-pipe')
    try:
        yield
    finally:
        _physical_wire.reset(token)


def guard_socket_entry():
    op = _operation.get()
    if op is not None and _physical_wire.get() not in {op.call_id, 'business-control-db', 'process-local-pipe'}:
        raise PolicyDenied('UNSUPPORTED_TRANSPORT')


def result_is_empty(result):
    return result is None or bool(getattr(result, "empty", False)) or (isinstance(result, (list, tuple, dict)) and len(result) == 0)


def failure_kind(exc):
    """Classify types/status, never retry a data error because its text says timeout."""
    from app.services.data_contract_errors import DataContractError
    from http.client import RemoteDisconnected
    import requests
    if isinstance(exc, (PolicyDenied, DataContractError)):
        return "neutral", 0
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in (403, 429):
        from email.utils import parsedate_to_datetime
        value = response.headers.get("Retry-After", "0")
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 0
        return "severe", max(0, delay) if math.isfinite(delay) else 0
    if isinstance(exc, (ConnectionError, TimeoutError, RemoteDisconnected, requests.ConnectionError, requests.Timeout)) or (
        isinstance(status, int) and 500 <= status < 600):
        return "failure", 0
    return "neutral", 0


def current_controller():
    context = _scope.get()
    op = _operation.get()
    if context and op and context[0].path != op.controller.path:
        raise PolicyDenied("CONTROL_CONTEXT_MISMATCH")
    controller = context[0] if context else op.controller if op else None
    if controller:
        _check_deployment_store(controller)
        return controller
    if os.environ.get("DATA_SOURCE_CONTROL_DB"):
        raise PolicyDenied("TASK_CONTEXT_REQUIRED")
    return None


def _check_deployment_store(controller):
    configured = os.environ.get("DATA_SOURCE_CONTROL_DB")
    if configured and Path(configured).resolve() != Path(controller.path):
        raise PolicyDenied("CONTROL_STORE_MISMATCH")


@contextmanager
def controlled_sdk_session():
    """BaoStock private socket hooks; installed only inside its owned child.

    The parent process kills noncooperative SDK/EOF loops at the active call's
    deadline. The child accounts connections and protocol sends separately.
    """
    import socket
    import baostock.util.socketutil as sdk
    import baostock.common.context as context
    original_connect, original_message = socket.socket.connect, sdk.send_msg

    def operation():
        controller = current_controller()
        op = _operation.get()
        if controller is None or op is None or op.transport != 'sdk' or not op.endpoint.startswith('BAO:'):
            raise PolicyDenied('SDK_OPERATION_REQUIRED')
        return controller, op

    def connect(sock, address):
        controller, op = operation()
        if address[0] not in op.allowed_hosts:
            raise PolicyDenied('HOST_NOT_ALLOWED')
        def wire(start):
            start()
            with physical_transport():
                return original_connect(sock, address)
        return controller._invoke(replace(op, wire_phase='connect'), wire)

    def message(value):
        controller, op = operation()
        raw = getattr(context, 'default_socket', None)
        if raw is None:
            raise ConnectionError('SDK socket unavailable')
        def wire(start):
            failure = []
            class GuardedSocket:
                sent = False
                def send(self, *args, **kwargs):
                    try:
                        if self.sent:
                            raise PolicyDenied('SDK_MULTIPLE_SENDS')
                        start()
                        self.sent = True
                        return raw.send(*args, **kwargs)
                    except BaseException as exc:
                        failure.append(exc)
                        raise
                def __getattr__(self, name):
                    return getattr(raw, name)
            context.default_socket = GuardedSocket()
            try:
                result = original_message(value)
                if failure:
                    raise failure[0]
                if result is None:
                    raise ConnectionError('SDK response unavailable')
                return result
            finally:
                context.default_socket = raw
        return controller._invoke(replace(op, wire_phase='message'), wire)

    socket.socket.connect, sdk.send_msg = connect, message
    try:
        yield
    finally:
        socket.socket.connect, sdk.send_msg = original_connect, original_message


def request_checkpoint():
    controller = current_controller()
    if controller is not None:
        op = _operation.get()
        if op:
            with controller._db() as db:
                controller._check(db, op)
        else:
            controller.checkpoint()


def _configured_http_hosts(path):
    """Read bindings without creating/upgrading a store for unrelated HTTP."""
    db = None
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)
        if db.execute('SELECT version FROM request_control_meta').fetchone() != (1,):
            raise PolicyDenied('CONTROL_STORE_IDENTITY')
        hosts = set()
        for (body,) in db.execute("SELECT body FROM policies WHERE key LIKE 'endpoint:%'"):
            value = json.loads(body)
            if value.get('transport') == 'requests':
                hosts.update(value.get('allowed_hosts', ()))
        return hosts
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise PolicyDenied('CONTROL_STORE_UNAVAILABLE') from exc
    finally:
        if db is not None:
            db.close()


def controlled_send(session, request, kwargs, send):
    """Account each requests wire call; hidden retries/redirects cannot fan out."""
    from urllib.parse import urlsplit
    validate_upstream_scope((urlsplit(request.url).hostname,))
    if _operation.get() is None and _scope.get() is None:
        from urllib.parse import urlsplit
        path = os.environ.get('DATA_SOURCE_CONTROL_DB')
        if not path or urlsplit(request.url).hostname not in _configured_http_hosts(path):
            # Source entry points are mandatory and audited separately. The
            # deployment flag must not classify independent app APIs as data.
            return send(session, request, **kwargs)
        raise PolicyDenied('TASK_CONTEXT_REQUIRED')
    controller = current_controller()
    if controller is None:
        return send(session, request, **kwargs)
    op = _operation.get()
    if op is None:
        raise PolicyDenied("OPERATION_REQUIRED")
    if op.transport != "requests":
        raise PolicyDenied("TRANSPORT_MISMATCH")
    from urllib.parse import urlsplit
    target = urlsplit(request.url)
    if target.scheme not in {"http", "https"} or target.hostname not in op.allowed_hosts:
        raise PolicyDenied("HOST_NOT_ALLOWED")
    adapter = session.get_adapter(request.url)
    retries = getattr(adapter, "max_retries", None)
    # urllib3 total=None permits unbounded retries even if a sub-limit is zero.
    if retries is None or retries.total not in (0, False):
        raise PolicyDenied("TRANSPORT_RETRIES_UNSUPPORTED")
    options = dict(kwargs, allow_redirects=False)

    def wire(start):
        from urllib3.util import Timeout
        remaining = op.deadline - controller.clock()
        configured = options.get("timeout")
        if isinstance(configured, tuple):
            connect, read = configured
        elif isinstance(configured, Timeout):
            connect, read = configured.connect_timeout, configured.read_timeout
        else:
            connect = read = configured
        def clamp(value):
            return remaining if value is None else min(float(value), remaining)
        options["timeout"] = Timeout(total=remaining, connect=clamp(connect), read=clamp(read))
        # Keep ownership of body reads. Eager requests.send can otherwise stay
        # alive forever when a peer drips bytes faster than its idle timeout.
        options['stream'] = True
        start()
        with physical_transport():
            response = send(session, request, **options)
        try:
            if 300 <= response.status_code < 400:
                raise PolicyDenied("REDIRECT_REQUIRES_EXPLICIT_CALL")
            response.raise_for_status()
            if response.raw is not None and not response._content_consumed:
                read = getattr(response.raw, 'read1', None)
                if read is None:
                    raise PolicyDenied('BOUNDED_BODY_TRANSPORT_REQUIRED')
                chunks, size = [], 0
                while True:
                    with controller._db() as db:
                        controller._check(db, op)
                    chunk = read(65536, decode_content=True)
                    with controller._db() as db:
                        controller._check(db, op)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise PolicyDenied('RESPONSE_SIZE_LIMIT')
                    chunks.append(chunk)
                response._content = b''.join(chunks)
                response._content_consumed = True
                response.close()
        except BaseException:
            response.close()
            with controller._db() as db:
                controller._check(db, op)
            raise
        return response

    return controller._invoke(op, wire)
