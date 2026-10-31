"""Observation ledger inside the shared control DB, independent of business sessions.

No arguments, URLs, response bodies or exception messages are persisted. A
successful supplier observation never certifies a business transaction commit.
"""
from __future__ import annotations

import json
import re
import uuid


CALL_STATUSES = ("running", "success", "empty_result", "supplier_error", "contract_error", "data_error",
                 "policy_denied", "invalid_request", "interrupted", "abandoned")


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS request_calls (id TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)))")
    db.execute("CREATE TABLE IF NOT EXISTS request_attempts (id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES request_calls(id), body TEXT NOT NULL CHECK(json_valid(body)))")
    db.execute("CREATE INDEX IF NOT EXISTS request_attempt_call ON request_attempts(call_id)")
    db.execute("CREATE INDEX IF NOT EXISTS request_call_endpoint ON request_calls(json_extract(body,'$.endpoint'))")
    db.execute("CREATE INDEX IF NOT EXISTS request_call_task ON request_calls(json_extract(body,'$.task_id'))")
    db.execute("CREATE TABLE IF NOT EXISTS request_selections (id TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)))")


def get(db, table, record_id):
    row = db.execute(f"SELECT body FROM {table} WHERE id=?", (record_id,)).fetchone()
    return json.loads(row[0]) if row else None


def save(db, table, record):
    db.execute(f"UPDATE {table} SET body=? WHERE id=?", (json.dumps(record, allow_nan=False), record["id"]))


def begin(db, endpoint, task_id, kind, parent_id, now, selection_id=None):
    record = dict(id=uuid.uuid4().hex, endpoint=endpoint, provider=None, task_id=task_id, kind=kind,
                  parent_call_id=parent_id, selection_id=selection_id, lane=None, config_version=None, started_at=now, finished_at=None,
                  deadline=now + 30, status="running", error_code=None, error_type=None,
                  execution_rounds=0, result_status="not_checked", business_commit_status="not_observed")
    db.execute("INSERT INTO request_calls VALUES (?,?)", (record["id"], json.dumps(record)))
    return record["id"]


def bind(db, call_id, *, provider, lane, versions, deadline):
    record = get(db, "request_calls", call_id)
    record.update(provider=provider, lane=lane, config_version={"endpoint": versions[0], "provider": versions[1]}, deadline=deadline)
    save(db, "request_calls", record)


def round_started(db, call_id, round_no):
    record = get(db, "request_calls", call_id)
    record["execution_rounds"] = max(record["execution_rounds"], round_no)
    save(db, "request_calls", record)


def grant(db, call_id, round_no, transport, now):
    record = dict(id=uuid.uuid4().hex, call_id=call_id, round_no=round_no, transport=transport,
                  admitted_at=now, started_at=None, finished_at=None, status="admitted", error_code=None,
                  error_type=None, http_status=None)
    db.execute("INSERT INTO request_attempts VALUES (?,?,?)", (record["id"], call_id, json.dumps(record)))
    return record["id"]


def start(db, attempt_id, now):
    record = get(db, "request_attempts", attempt_id)
    if record["status"] != "admitted":
        return False
    record.update(started_at=now, status="running")
    save(db, "request_attempts", record)
    round_started(db, record["call_id"], record["round_no"])
    return True


def finish_attempt(db, attempt_id, now, status="success", details=None):
    record = get(db, "request_attempts", attempt_id)
    if record["status"] not in {"admitted", "running"}:
        return False
    if record["started_at"] is None:
        status = "not_started"
    record.update(finished_at=now, status=status, **(details or {}))
    save(db, "request_attempts", record)
    return True


def finish(db, call_id, now, status, details=None, result_status="not_checked"):
    record = get(db, "request_calls", call_id)
    if record["status"] != "running":
        return False
    record.update(finished_at=now, status=status, result_status=result_status,
                  **{k: v for k, v in (details or {}).items() if k != "http_status"})
    save(db, "request_calls", record)
    return True


def error_details(exc):
    from app.services.data_source_control import PolicyDenied, failure_kind
    from app.services.data_contract_errors import DataContractError
    code = None
    if isinstance(exc, PolicyDenied):
        # Codes produced by our admission boundary; never save supplier messages.
        allowed = {"DISABLED", "CONFIG_CHANGED", "DEADLINE", "CANCELLED", "TASK_QUOTA", "REQUEST_QUOTA",
                   "COOLDOWN", "HALF_OPEN_BUSY", "STALE_PERMIT", "TASK_UNCONFIGURED", "ENDPOINT_UNCONFIGURED",
                   "PROVIDER_UNCONFIGURED", "CONTROL_CONTEXT_MISMATCH", "CONTROL_STORE_MISMATCH", "TASK_CONTEXT_REQUIRED",
                   "HOST_NOT_ALLOWED", "STREAMING_UNSUPPORTED", "TRANSPORT_MISMATCH", "TRANSPORT_RETRIES_UNSUPPORTED",
                   "REDIRECT_REQUIRES_EXPLICIT_CALL", "OPERATION_REQUIRED", "OBSERVATION_CLOSED", "CONTROL_STORE_UNAVAILABLE", "OBSERVATION_LANE_MISMATCH"}
        code = exc.args[0] if exc.args and exc.args[0] in allowed else "POLICY_DENIED"
        status = "policy_denied"
    elif isinstance(exc, DataContractError):
        status, code = "contract_error", "DATA_CONTRACT_ERROR"
    elif isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        status, code = "interrupted", "INTERRUPTED"
    elif failure_kind(exc)[0] in {"failure", "severe"}:
        status, code = "supplier_error", "SUPPLIER_TRANSPORT_ERROR"
    elif isinstance(exc, (TypeError, ValueError)):
        status, code = "invalid_request", "INVALID_REQUEST"
    else:
        status, code = "data_error", "DATA_OR_ADAPTER_ERROR"
    name = type(exc).__name__
    http_status = getattr(getattr(exc, "response", None), "status_code", None)
    return status, {"error_code": code, "error_type": name[:80] if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name) else "Exception",
                    "http_status": http_status if type(http_status) is int else None}


def reconcile_expired(db, now):
    expired = [json.loads(row[0]) for row in db.execute("SELECT body FROM request_calls")]
    ids = set()
    for call in expired:
        if call["status"] == "running" and call["deadline"] <= now:
            finish(db, call["id"], now, "abandoned", {"error_code": "OBSERVATION_DEADLINE_EXPIRED"})
            ids.add(call["id"])
    changed = 0
    for row in db.execute("SELECT body FROM request_attempts").fetchall():
        attempt = json.loads(row[0])
        if attempt["call_id"] in ids and attempt["status"] in {"admitted", "running"}:
            finish_attempt(db, attempt["id"], now, "unknown", {"error_code": "OUTCOME_NOT_OBSERVED"})
            changed += 1
    for row in db.execute("SELECT body FROM request_selections").fetchall():
        selection = json.loads(row[0])
        if selection["status"] == "running" and selection["deadline"] <= now:
            finish_selection(db, selection["id"], now, "abandoned")
    return {"calls": len(ids), "attempts": changed}


def interrupt_task(db, task_id, now):
    ids = set()
    for row in db.execute("SELECT body FROM request_calls").fetchall():
        call = json.loads(row[0])
        if call["status"] == "running" and call["task_id"] == task_id:
            finish(db, call["id"], now, "interrupted", {"error_code": "TASK_STOPPED"})
            ids.add(call["id"])
    changed = 0
    for row in db.execute("SELECT body FROM request_attempts").fetchall():
        attempt = json.loads(row[0])
        if attempt["call_id"] in ids and attempt["status"] in {"admitted", "running"}:
            finish_attempt(db, attempt["id"], now, "unknown", {"error_code": "OUTCOME_NOT_OBSERVED"})
            changed += 1
    for row in db.execute("SELECT body FROM request_selections").fetchall():
        selection = json.loads(row[0])
        if selection["status"] == "running" and selection["task_id"] == task_id:
            finish_selection(db, selection["id"], now, "interrupted")
    return {"calls": len(ids), "attempts": changed}


def begin_selection(db, task_id, kind, deadline, now):
    row = dict(id=uuid.uuid4().hex, task_id=task_id, kind=kind, deadline=deadline, started_at=now,
               finished_at=None, status="running", selected_source=None, fallback_used=False,
               considered_sources=[], attempted_sources=[], failed_sources=[], unavailable_sources=[], unsupported_sources=[],
               error_status=None, error_code=None, error_type=None)
    db.execute("INSERT INTO request_selections VALUES (?,?)", (row["id"], json.dumps(row)))
    return row["id"]


def source_event(db, selection_id, event, source):
    row = get(db, "request_selections", selection_id)
    if row["status"] != "running":
        return False
    if event == "selected":
        row.update(selected_source=source, fallback_used=bool(row["failed_sources"] or row["unavailable_sources"]))
    else:
        key = {"considered": "considered_sources", "attempted": "attempted_sources", "failed": "failed_sources",
               "unavailable": "unavailable_sources", "unsupported": "unsupported_sources"}[event]
        row[key].append(source)
    save(db, "request_selections", row)
    return True


def finish_selection(db, selection_id, now, status, details=None):
    row = get(db, "request_selections", selection_id)
    if row["status"] != "running":
        return False
    row.update(status=status, finished_at=now, **(details or {}))
    save(db, "request_selections", row)
    return True


def statistics(db, *, endpoint=None, task_id=None, kind=None, include_records=False):
    if kind is not None and kind not in {"call", "probe"}:
        raise ValueError("kind")
    predicates, values = [], []
    for key, value in (("endpoint", endpoint), ("task_id", task_id), ("kind", kind)):
        if value is not None:
            predicates.append(f"json_extract(c.body,'$.{key}')=?")
            values.append(value)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    calls = [json.loads(row[0]) for row in db.execute("SELECT c.body FROM request_calls c" + where, values)]
    attempts = [json.loads(row[0]) for row in db.execute("SELECT a.body FROM request_attempts a JOIN request_calls c ON a.call_id=c.id" + where, values)]
    selected_ids = {row["selection_id"] for row in calls if row.get("selection_id")}
    selections = [json.loads(row[0]) for row in db.execute("SELECT body FROM request_selections")]
    selections = [s for s in selections if (task_id is None or s["task_id"] == task_id) and (kind is None or s["kind"] == kind)
                  and (endpoint is None or s["id"] in selected_ids)]
    def counts(selected, selected_kind=None):
        selected_ids = {c["id"] for c in selected}
        observations = [a for a in attempts if a["call_id"] in selected_ids]
        result = {status: sum(c["status"] == status for c in selected) for status in CALL_STATUSES}
        result.update(logical_calls=len(selected), execution_rounds=sum(c["execution_rounds"] for c in selected),
                      retry_count=sum(max(0, c["execution_rounds"] - 1) for c in selected), admissions=len(observations),
                      attempts=sum(a["started_at"] is not None for a in observations))
        for status in ("success", "supplier_error", "contract_error", "data_error", "policy_denied", "invalid_request", "interrupted", "running", "unknown", "not_started", "admitted"):
            result["attempt_" + status] = sum(a["status"] == status for a in observations)
        choices = [s for s in selections if selected_kind is None or s["kind"] == selected_kind]
        result.update(selections=len(choices), fallbacks=sum(s["status"] == "success" and s["fallback_used"] for s in choices),
                      fallback_attempts=sum(len(s["attempted_sources"]) > 1 or bool(s["unavailable_sources"]) for s in choices),
                      selection_failures=sum(s["status"] == "failed" for s in choices),
                      selection_contract_errors=sum(s.get("error_status") == "contract_error" for s in choices),
                      selection_policy_denials=sum(s.get("error_status") == "policy_denied" for s in choices))
        return result
    result = {"schema_version": "request-observations-v1", "totals": counts(calls),
              "by_kind": {k: counts([c for c in calls if c["kind"] == k], k) for k in ("call", "probe")}}
    if include_records:
        result.update(calls=sorted(calls, key=lambda c: (c["started_at"], c["id"])),
                      attempts=sorted(attempts, key=lambda a: (a["admitted_at"], a["id"])), selections=selections)
    return result


def management_summary(db):
    """One consistent UI read; aggregate in SQL, don't load all observation bodies."""
    result = {}
    statuses = ("success", "empty_result", "supplier_error", "contract_error", "data_error", "policy_denied", "invalid_request", "interrupted", "abandoned", "running")
    expressions = ["COUNT(*)", "SUM(json_extract(body,'$.execution_rounds'))",
                   "SUM(MAX(0,json_extract(body,'$.execution_rounds')-1))"]
    expressions += [f"SUM(json_extract(body,'$.status')='{status}')" for status in statuses]
    for row in db.execute("SELECT json_extract(body,'$.endpoint'),json_extract(body,'$.kind')," + ",".join(expressions) +
                          " FROM request_calls GROUP BY 1,2"):
        key, kind, *values = row
        entry = result.setdefault(key, {"by_kind": {}, "latest": {}})
        entry["by_kind"][kind] = dict(zip(("logical_calls", "execution_rounds", "retry_count", *statuses), values))
    for key, kind, admissions, attempts, failures in db.execute("SELECT json_extract(c.body,'$.endpoint'),json_extract(c.body,'$.kind'),COUNT(*),"
            "SUM(json_extract(a.body,'$.started_at') IS NOT NULL),SUM(json_extract(a.body,'$.status')='supplier_error') "
            "FROM request_attempts a JOIN request_calls c ON a.call_id=c.id GROUP BY 1,2"):
        result[key]["by_kind"][kind].update(admissions=admissions, attempts=attempts, attempt_supplier_error=failures)
    for (body,) in db.execute("SELECT body FROM (SELECT body,ROW_NUMBER() OVER (PARTITION BY json_extract(body,'$.endpoint'),"
            "json_extract(body,'$.kind') ORDER BY COALESCE(json_extract(body,'$.finished_at'),json_extract(body,'$.started_at')) DESC,id DESC) AS n "
            "FROM request_calls) WHERE n=1"):
        row = json.loads(body)
        result[row["endpoint"]]["latest"][row["kind"]] = row
    for entry in result.values():
        for kind in ("call", "probe"):
            data = entry["by_kind"].setdefault(kind, {})
            for key in ("logical_calls", "execution_rounds", "retry_count", "admissions", "attempts", "attempt_supplier_error", *statuses):
                data.setdefault(key, 0)
    return result
