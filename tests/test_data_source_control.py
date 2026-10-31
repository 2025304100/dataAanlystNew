"""ST-M04.2 at public policy/execution and real local HTTP boundaries."""
import pytest
from dataclasses import replace
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

pytestmark = pytest.mark.whitebox


def test_disabled_endpoint_never_invokes_supplier(tmp_path):
    from app.services.data_source_control import RequestController, RequestPolicy, PolicyDenied
    controller = RequestController(tmp_path / "control.sqlite")
    controller.set_provider("eastmoney", RequestPolicy())
    controller.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(enabled=False))
    controller.start_task("task-1", lane="incremental", max_requests=5, timeout_s=10)
    calls = []
    with pytest.raises(PolicyDenied, match="DISABLED"):
        controller.execute("stock_zh_a_hist", lambda: calls.append(1), task_id="task-1")
    assert calls == []


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            action, self.on_sleep = self.on_sleep, None
            action()


def configured(tmp_path, *, policy=None, clock=None, transport="callable", hosts=()):
    from app.services.data_source_control import RequestController, RequestPolicy
    opts = dict(clock=clock, sleep=clock.sleep) if clock else {}
    c = RequestController(tmp_path / "control.sqlite", **opts)
    c.set_provider("eastmoney", policy or RequestPolicy())
    c.set_endpoint("stock_zh_a_hist", "eastmoney", policy or RequestPolicy(),
                   transport=transport, allowed_hosts=hosts)
    c.start_task("job", lane="incremental", max_requests=20, timeout_s=120)
    return c


def test_success_and_attempt_cap_and_task_budget(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = configured(tmp_path, policy=RequestPolicy(max_attempts=2, failure_threshold=20))
    calls = []
    def fail():
        calls.append(1)
        raise ConnectionError("offline")
    with pytest.raises(ConnectionError):
        c.execute("stock_zh_a_hist", fail, task_id="job", max_attempts=100, base_delay=0)
    assert len(calls) == 2
    c.start_task("tiny", lane="incremental", max_requests=1, timeout_s=10)
    assert c.execute("stock_zh_a_hist", lambda: 7, task_id="tiny") == 7
    with pytest.raises(PolicyDenied, match="TASK_QUOTA"):
        c.execute("stock_zh_a_hist", lambda: calls.append(3), task_id="tiny")
    assert c.snapshot()["state"]["task:tiny"]["used"] == 1


def test_wait_rechecks_policy_and_does_not_consume_request(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    clock = Clock()
    p = RequestPolicy(min_interval_s=1)
    c = configured(tmp_path, policy=p, clock=clock)
    c.execute("stock_zh_a_hist", lambda: None, task_id="job")
    clock.on_sleep = lambda: c.set_endpoint("stock_zh_a_hist", "eastmoney", replace(p, enabled=False), expected_version=1)
    with pytest.raises(PolicyDenied, match="DISABLED|CONFIG_CHANGED"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("disabled wire call"), task_id="job")
    assert c.snapshot()["state"]["task:job"]["used"] == 1


def test_deadline_cancellation_and_late_result(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    clock = Clock()
    c = configured(tmp_path, policy=RequestPolicy(min_interval_s=1), clock=clock)
    c.start_task("short", lane="incremental", max_requests=5, timeout_s=.2)
    c.execute("stock_zh_a_hist", lambda: None, task_id="short")
    with pytest.raises(PolicyDenied, match="DEADLINE"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("expired"), task_id="short")
    c.cancel_task("job")
    with pytest.raises(PolicyDenied, match="CANCELLED"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("cancelled"), task_id="job")
    c.start_task("late", lane="incremental", max_requests=5, timeout_s=10)
    def slow():
        clock.now += 20
        return "late"
    with pytest.raises(PolicyDenied, match="DEADLINE"):
        c.execute("stock_zh_a_hist", slow, task_id="late")


def test_breaker_recovers_and_contract_error_is_not_retried(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    from app.services.data_contract_errors import DataContractError
    clock = Clock()
    c = configured(tmp_path, policy=RequestPolicy(failure_threshold=1, cooldown_s=1), clock=clock)
    with pytest.raises(ConnectionError):
        c.execute("stock_zh_a_hist", lambda: (_ for _ in ()).throw(ConnectionError("offline")), task_id="job", max_attempts=1)
    with pytest.raises(PolicyDenied, match="COOLDOWN"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("cooldown"), task_id="job")
    clock.now += 1.1
    assert c.execute("stock_zh_a_hist", lambda: "recovered", task_id="job") == "recovered"
    calls = []
    def invalid():
        calls.append(1)
        raise DataContractError("timeout unit")
    with pytest.raises(DataContractError):
        c.execute("stock_zh_a_hist", invalid, task_id="job")
    assert len(calls) == 1
    assert c.snapshot()["state"]["endpoint:stock_zh_a_hist"]["failures"] == 0


@contextmanager
def http_server(status=200, headers=None, delay=0, hold=None):
    import time
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, time.time()))
            if hold is not None:
                hold.wait(15)
            if delay:
                time.sleep(delay)
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except (ConnectionError, OSError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_wrapper_charges_each_real_http_page_and_rejects_over_budget(tmp_path):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path, transport="requests", hosts=("127.0.0.1",))
    c.start_task("pages", lane="backfill", max_requests=2, timeout_s=10)
    with http_server() as (url, hits), c.scope("pages"):
        def supplier():
            return [requests.get(url + f"/{i}", timeout=2).text for i in range(3)]
        with pytest.raises(PolicyDenied, match="TASK_QUOTA"):
            call_akshare_with_retry(supplier, api_key="stock_zh_a_hist", max_attempts=5, base_delay=0)
        assert len(hits) == 2
    assert c.snapshot()["state"]["task:pages"]["used"] == 2


def test_backfill_cannot_spend_incremental_reserve_and_new_window_releases_quota(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    clock = Clock()
    c = configured(tmp_path, policy=RequestPolicy(total_limit=4, incremental_limit=2, window_s=10), clock=clock)
    c.start_task("repair", lane="backfill", max_requests=10, timeout_s=100)
    for _ in range(2):
        c.execute("stock_zh_a_hist", lambda: 1, task_id="repair")
    with pytest.raises(PolicyDenied, match="REQUEST_QUOTA"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("borrowed incremental reserve"), task_id="repair")
    for _ in range(2):
        assert c.execute("stock_zh_a_hist", lambda: 1, task_id="job") == 1
    with pytest.raises(PolicyDenied, match="REQUEST_QUOTA"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("overflow"), task_id="job")
    clock.now += 10
    assert c.execute("stock_zh_a_hist", lambda: 2, task_id="repair") == 2


def test_registry_in_scope_reads_shared_version_instead_of_old_cache(tmp_path):
    from app.services.data_source_control import RequestController, RequestPolicy, PolicyDenied
    from app.services.akshare_registry import get_runtime_config, is_api_enabled, get_max_retries
    c = configured(tmp_path)
    other_process_view = RequestController(c.path)
    other_process_view.set_provider("eastmoney", RequestPolicy(enabled=False, max_attempts=1), expected_version=1)
    with c.scope("job"):
        assert is_api_enabled("stock_zh_a_hist") is False
        cfg = get_runtime_config("stock_zh_a_hist")
        assert cfg["config_version"] == {"endpoint": 1, "provider": 2}
        assert get_max_retries("stock_zh_a_hist") == 1
    with pytest.raises(PolicyDenied, match="CONFIG_VERSION_CONFLICT"):
        c.set_provider("eastmoney", RequestPolicy(), expected_version=1)


def test_deployment_cannot_switch_to_a_different_control_store(tmp_path, monkeypatch):
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", str(tmp_path / "deployed.sqlite"))
    with c.scope("job"), pytest.raises(PolicyDenied, match="CONTROL_STORE_MISMATCH"):
        call_akshare_with_retry(lambda: pytest.fail("alternate quota store"), api_key="stock_zh_a_hist")


def test_expired_half_open_result_cannot_override_new_probe(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    from concurrent.futures import ThreadPoolExecutor
    clock = Clock()
    c = configured(tmp_path, policy=RequestPolicy(failure_threshold=1, cooldown_s=1, probe_lease_s=1), clock=clock)
    with pytest.raises(ConnectionError):
        c.execute("stock_zh_a_hist", lambda: (_ for _ in ()).throw(ConnectionError()), task_id="job", max_attempts=1)
    clock.now += 1.1
    started, release = threading.Event(), threading.Event()
    def late_probe():
        started.set()
        assert release.wait(5)
        return "obsolete"
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(c.execute, "stock_zh_a_hist", late_probe, task_id="job")
        try:
            assert started.wait(5)
            clock.now += 1.1
            assert c.execute("stock_zh_a_hist", lambda: "new", task_id="job") == "new"
        finally:
            release.set()
        with pytest.raises(PolicyDenied, match="STALE_PERMIT"):
            future.result(timeout=5)
    assert c.execute("stock_zh_a_hist", lambda: "healthy", task_id="job") == "healthy"


@pytest.mark.parametrize("endpoint", ["PLAN:Fuyao", "PLAN:iFinD", "PLAN:BSE", "stock_zh_a_hist_min_em", "PLAN:CSI_constituents"])
def test_unimplemented_or_excluded_endpoint_cannot_be_enabled(tmp_path, endpoint):
    from app.services.data_source_control import RequestController, RequestPolicy, PolicyDenied
    c = RequestController(tmp_path / "policy.sqlite")
    c.set_provider("planned", RequestPolicy())
    with pytest.raises(PolicyDenied, match="OUT_OF_RELEASE|IMPLEMENTATION_NOT_READY"):
        c.set_endpoint(endpoint, "planned", RequestPolicy())


@pytest.mark.parametrize("case", ["host", "stream", "redirect", "transport_retry", "missing_operation", "missing_task"])
def test_http_cannot_fan_out_or_bypass_admission(tmp_path, monkeypatch, case):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path, transport="requests", hosts=("127.0.0.1",))
    with http_server(302 if case == "redirect" else 200, {"Location": "/hidden"}) as (url, hits):
        def supplier():
            session = requests.Session()
            if case == "transport_retry":
                session.mount("http://", requests.adapters.HTTPAdapter(max_retries=3))
            return session.get(url.replace("127.0.0.1", "localhost") if case == "host" else url,
                               stream=case == "stream", timeout=1)
        codes = {"host": "HOST_NOT_ALLOWED", "stream": "STREAMING_UNSUPPORTED", "redirect": "REDIRECT",
                 "transport_retry": "TRANSPORT_RETRIES", "missing_operation": "OPERATION_REQUIRED", "missing_task": "TASK_CONTEXT_REQUIRED"}
        if case == "missing_task":
            monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", c.path)
            with pytest.raises(PolicyDenied, match=codes[case]):
                supplier()
        elif case == 'stream':
            with c.scope('job'):
                response = call_akshare_with_retry(supplier, api_key='stock_zh_a_hist')
            assert response._content_consumed is True
            assert c.statistics()['totals']['attempts'] == 1
            assert len(hits) == 1
            return
        else:
            with c.scope("job"), pytest.raises(PolicyDenied, match=codes[case]):
                if case == "missing_operation":
                    supplier()
                else:
                    call_akshare_with_retry(supplier, api_key="stock_zh_a_hist")
        assert len(hits) == (1 if case == "redirect" else 0)


@pytest.mark.parametrize("status,expected", [(503, 2), (429, 1), (403, 1)])
def test_http_failures_retry_or_cool_down_without_changing_auth(tmp_path, status, expected):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = configured(tmp_path, policy=RequestPolicy(max_attempts=2, failure_threshold=20),
                   transport="requests", hosts=("127.0.0.1",))
    with http_server(status, {"Retry-After": "1200"}) as (url, hits), c.scope("job"):
        with pytest.raises(requests.HTTPError):
            call_akshare_with_retry(lambda: requests.get(url, timeout=1), api_key="stock_zh_a_hist", base_delay=0)
        assert len(hits) == expected
        if status != 503:
            import time
            assert c.snapshot()["state"]["provider:eastmoney"]["open_until"] >= time.time() + 1190
            with pytest.raises(PolicyDenied, match="COOLDOWN"):
                call_akshare_with_retry(lambda: requests.get(url), api_key="stock_zh_a_hist")
            assert len(hits) == 1
    assert "auth_status" not in c.configuration("stock_zh_a_hist")["provider"]


def test_http_read_timeout_is_bounded_by_remaining_budget(tmp_path):
    import requests
    import time
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = configured(tmp_path, policy=RequestPolicy(timeout_s=.08), transport="requests", hosts=("127.0.0.1",))
    with http_server(delay=.3) as (url, hits), c.scope("job"):
        start = time.monotonic()
        with pytest.raises((PolicyDenied, requests.Timeout)):
            call_akshare_with_retry(lambda: requests.get(url, timeout=60), api_key="stock_zh_a_hist", base_delay=0)
        assert time.monotonic() - start < .6
        assert len(hits) == 1


def test_provider_interval_applies_across_endpoints_and_threads(tmp_path):
    from app.services.data_source_control import RequestPolicy
    from concurrent.futures import ThreadPoolExecutor
    import time
    c = configured(tmp_path, policy=RequestPolicy(min_interval_s=.1))
    c.set_endpoint("fund_etf_hist_em", "eastmoney", RequestPolicy())
    with ThreadPoolExecutor(4) as pool:
        futures = [pool.submit(c.execute, ep, time.time, task_id="job")
                   for ep in ["stock_zh_a_hist", "fund_etf_hist_em"] * 2]
        times = sorted(f.result(timeout=5) for f in futures)
    assert all(b - a >= .08 for a, b in zip(times, times[1:]))
    assert c.snapshot()["state"]["provider:eastmoney"]["used"] == 4


def wait_for(predicate, timeout=30):
    import time
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= until:
            raise AssertionError("worker coordination timed out")
        time.sleep(.01)


def _process_request(path, folder, index, hold):
    import os
    import json
    from pathlib import Path
    import requests
    from app.services.data_source_control import RequestController, PolicyDenied
    from app.services.akshare_utils import call_akshare_with_retry
    try:
        folder = Path(folder)
        c = RequestController(path)
        (folder / f"ready-{index}").write_text(str(os.getpid()))
        wait_for(lambda: (folder / "go").exists())
        def supplier():
            if hold:
                (folder / f"started-{index}").write_text(str(os.getpid()))
                wait_for(lambda: (folder / "release").exists())
            # The URL is supplied in task-local test environment, never source config.
            return requests.get(os.environ["M042_LOCAL_URL"], timeout=5).status_code
        with c.scope(os.environ.get("M042_LOCAL_TASK", "job")):
            result = call_akshare_with_retry(supplier, api_key="stock_zh_a_hist", max_attempts=1)
        result = ("ok", result)
    except PolicyDenied as exc:
        result = ("denied", str(exc))
    except BaseException as exc:
        result = ("error", repr(exc))
    temporary = folder / f"result-{index}.tmp"
    temporary.write_text(json.dumps(result))
    temporary.replace(folder / f"result-{index}.json")


@pytest.mark.parametrize("mode", ["quota", "task_quota", "endpoint_quota", "pacing", "half_open", "disable"])
def test_processes_share_real_http_quota_pacing_and_probe(tmp_path, monkeypatch, mode):
    import subprocess
    import sys
    import json
    from pathlib import Path
    import time
    from app.services.data_source_control import RequestPolicy
    p = RequestPolicy(min_interval_s=.15 if mode == "pacing" else 0,
                      total_limit=3 if mode == "quota" else None,
                      failure_threshold=1, cooldown_s=.15, probe_lease_s=15)
    c = configured(tmp_path, policy=p, transport="requests", hosts=("127.0.0.1",))
    if mode == "task_quota":
        c.start_task("process-job", lane="incremental", max_requests=3, timeout_s=120)
    if mode == "endpoint_quota":
        c.set_endpoint("stock_zh_a_hist", "eastmoney", replace(p, total_limit=3),
                       transport="requests", allowed_hosts=("127.0.0.1",), expected_version=1)
    if mode == "half_open":
        import requests
        from app.services.akshare_utils import call_akshare_with_retry
        with http_server(503) as (url, _), c.scope("job"), pytest.raises(requests.HTTPError):
            call_akshare_with_retry(lambda: requests.get(url), api_key="stock_zh_a_hist", max_attempts=1)
        time.sleep(.2)
    folder = tmp_path / "coordination"
    folder.mkdir()
    count = 5 if mode == "quota" else 4
    hold = threading.Event() if mode == "half_open" else None
    with http_server(hold=hold) as (url, hits):
        monkeypatch.setenv("M042_LOCAL_URL", url)
        monkeypatch.setenv("M042_LOCAL_TASK", "process-job" if mode == "task_quota" else "job")
        workers, logs = [], []
        results = []
        try:
            for index in range(count):
                log = (folder / f"worker-{index}.log").open("w")
                logs.append(log)
                workers.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--control-worker", c.path, str(folder), str(index), "0"],
                    stdout=log, stderr=subprocess.STDOUT))
            wait_for(lambda: len(list(folder.glob("ready-*"))) == count)
            assert len({p.read_text() for p in folder.glob("ready-*")}) == count
            if mode == "disable":
                c.set_provider("eastmoney", replace(p, enabled=False), expected_version=1)
            (folder / "go").write_text("start")
            if mode == "half_open":
                # Keep the granted probe pending until all competitors are rejected.
                wait_for(lambda: len(list(folder.glob("result-*.json"))) == count - 1 and len(hits) >= 1, timeout=10)
                assert len(hits) == 1
                hold.set()
            wait_for(lambda: len(list(folder.glob("result-*.json"))) == count)
            results = [json.loads(p.read_text()) for p in folder.glob("result-*.json")]
            for worker in workers:
                assert worker.wait(timeout=20) == 0
        finally:
            if hold is not None:
                hold.set()
            (folder / "release").write_text("release")
            for worker in workers:
                if worker.poll() is None:
                    worker.terminate()
                worker.wait(timeout=5)
            for log in logs:
                log.close()
        assert not [r for r in results if r[0] == "error"], results
        expected = {"quota": 3, "task_quota": 3, "endpoint_quota": 3, "pacing": 4, "half_open": 1, "disable": 0}[mode]
        assert sum(r[0] == "ok" for r in results) == len(hits) == expected, results
        if mode == "pacing":
            times = sorted(t for _, t in hits)
            assert all(b - a >= .11 for a, b in zip(times, times[1:]))
        if mode == "half_open":
            assert all(r[1] == "HALF_OPEN_BUSY" for r in results if r[0] == "denied")
        (tmp_path / "observations.json").write_text(json.dumps({"mode": mode,
            "worker_pids": sorted(int(p.read_text()) for p in folder.glob("ready-*")),
            "results": results, "http_hits": hits, "admission_state": c.snapshot()["state"]}, indent=2))


@pytest.mark.parametrize("level", ["provider", "endpoint"])
def test_breaker_scope_does_not_block_unrelated_providers(tmp_path, level):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = configured(tmp_path, policy=RequestPolicy(failure_threshold=20))
    if level == "provider":
        c.set_provider("eastmoney", RequestPolicy(failure_threshold=1), expected_version=1)
    else:
        c.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(failure_threshold=1), expected_version=1)
    c.set_endpoint("fund_etf_hist_em", "eastmoney", RequestPolicy())
    c.set_provider("sina", RequestPolicy())
    c.set_endpoint("stock_zh_a_daily", "sina", RequestPolicy())
    with pytest.raises(ConnectionError):
        c.execute("stock_zh_a_hist", lambda: (_ for _ in ()).throw(ConnectionError()), task_id="job", max_attempts=1)
    with pytest.raises(PolicyDenied, match="COOLDOWN"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("open"), task_id="job")
    if level == "provider":
        with pytest.raises(PolicyDenied, match="COOLDOWN"):
            c.execute("fund_etf_hist_em", lambda: pytest.fail("shared provider open"), task_id="job")
    else:
        assert c.execute("fund_etf_hist_em", lambda: "other endpoint", task_id="job") == "other endpoint"
    assert c.execute("stock_zh_a_daily", lambda: "sina", task_id="job") == "sina"


def test_in_flight_config_change_discards_result_and_preserves_quota(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = configured(tmp_path)
    def supplier():
        c.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(max_attempts=1), expected_version=1)
        return "old policy"
    with pytest.raises(PolicyDenied, match="CONFIG_CHANGED"):
        c.execute("stock_zh_a_hist", supplier, task_id="job")
    assert c.snapshot()["state"]["task:job"]["used"] == 1
    assert c.snapshot()["state"]["endpoint:stock_zh_a_hist"]["failures"] == 0
    assert c.execute("stock_zh_a_hist", lambda: "new policy", task_id="job") == "new policy"


def test_store_failure_and_missing_task_fail_before_supplier(tmp_path):
    import sqlite3
    from app.services.data_source_control import RequestController, PolicyDenied
    c = configured(tmp_path)
    with pytest.raises(PolicyDenied, match="TASK_UNCONFIGURED"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("missing task"))
    with pytest.raises(PolicyDenied, match="TASK_ALREADY_EXISTS"):
        c.start_task("job", lane="incremental", max_requests=100, timeout_s=500)
    db = sqlite3.connect(c.path)
    try:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(PolicyDenied, match="CONTROL_STORE_UNAVAILABLE"):
            c.execute("stock_zh_a_hist", lambda: pytest.fail("store failure"), task_id="job")
    finally:
        db.rollback()
        db.close()
    foreign = tmp_path / "business.sqlite"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE existing_business (id INTEGER)")
    before = foreign.read_bytes()
    with pytest.raises(PolicyDenied, match="CONTROL_STORE_IDENTITY"):
        RequestController(foreign)
    assert foreign.read_bytes() == before


@pytest.mark.parametrize("field,value", [("enabled", 1), ("max_attempts", True), ("max_attempts", 0),
    ("timeout_s", 0), ("timeout_s", float("nan")), ("window_s", float("inf")),
    ("min_interval_s", -1), ("total_limit", -1), ("incremental_limit", 1.5)])
def test_invalid_policy_is_not_persisted(field, value):
    from app.services.data_source_control import RequestPolicy
    with pytest.raises(ValueError):
        RequestPolicy(**{field: value})


def test_cancel_during_wait_never_sends_and_scope_propagates_explicitly(tmp_path, monkeypatch):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    from app.services.akshare_utils import call_akshare_with_retry
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    clock = Clock()
    c = configured(tmp_path, policy=RequestPolicy(min_interval_s=1), clock=clock)
    c.execute("stock_zh_a_hist", lambda: None, task_id="job")
    clock.on_sleep = lambda: c.cancel_task("job")
    with pytest.raises(PolicyDenied, match="CANCELLED"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("cancelled waiting"), task_id="job")
    assert c.snapshot()["state"]["task:job"]["used"] == 1
    c.start_task("thread", lane="incremental", max_requests=2, timeout_s=10)
    monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", c.path)
    with c.scope("thread"), ThreadPoolExecutor(1) as pool:
        # Thread pools do not copy ContextVars. Lost context is fail-closed in deployment mode.
        future = pool.submit(call_akshare_with_retry, lambda: pytest.fail("unscoped"), api_key="stock_zh_a_hist")
        with pytest.raises(PolicyDenied, match="TASK_CONTEXT_REQUIRED"):
            future.result(timeout=5)
        ctx = copy_context()
        future = pool.submit(ctx.run, call_akshare_with_retry, lambda: "thread result", api_key="stock_zh_a_hist")
        assert future.result(timeout=5) == "thread result"


if __name__ == "__main__":
    import sys
    if sys.argv[1] != "--control-worker":
        raise SystemExit(2)
    _process_request(sys.argv[2], sys.argv[3], int(sys.argv[4]), bool(int(sys.argv[5])))
