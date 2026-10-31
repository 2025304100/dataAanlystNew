"""ST-M04.3 through public execution, observation and real probe boundaries."""
import pytest

pytestmark = pytest.mark.whitebox


def configured(tmp_path, *, transport="callable", **policy_kwargs):
    from app.services.data_source_control import RequestController, RequestPolicy
    c = RequestController(tmp_path / "control.sqlite")
    p = RequestPolicy(failure_threshold=20, **policy_kwargs)
    c.set_provider("eastmoney", p)
    c.set_endpoint("stock_zh_a_hist", "eastmoney", p, transport=transport,
                   allowed_hosts=("127.0.0.1",) if transport == "requests" else ())
    c.start_task("job", lane="incremental", max_requests=20, timeout_s=60)
    return c


def test_success_is_one_logical_call_and_one_observed_attempt_without_db_argument(tmp_path):
    from app.services.akshare_utils import call_akshare_with_retry
    c = configured(tmp_path)
    with c.scope("job"):
        assert call_akshare_with_retry(lambda: 7, api_key="stock_zh_a_hist") == 7
    stats = c.statistics(include_records=True)
    assert stats["totals"]["logical_calls"] == 1
    assert stats["totals"]["success"] == 1
    assert stats["totals"]["admissions"] == stats["totals"]["attempts"] == 1
    assert stats["totals"]["retry_count"] == 0
    assert stats["calls"][0]["business_commit_status"] == "not_observed"
    assert stats["attempts"][0]["status"] == "success"


def test_expired_inflight_is_unknown_and_late_return_cannot_forge_success(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path, timeout_s=1)
    now = [1000.0]
    c.clock = lambda: now[0]
    started, release = threading.Event(), threading.Event()
    def supplier():
        started.set()
        assert release.wait(5)
        return "late"
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(c.execute, "stock_zh_a_hist", supplier, task_id="job")
        try:
            assert started.wait(5)
            assert c.statistics()["totals"]["running"] == 1
            now[0] += 2
            assert c.reconcile_expired() == {"calls": 1, "attempts": 1}
            assert c.reconcile_expired() == {"calls": 0, "attempts": 0}
        finally:
            release.set()
        with pytest.raises(PolicyDenied, match="OBSERVATION_CLOSED|DEADLINE"):
            future.result(timeout=5)
    total = c.statistics()["totals"]
    assert total["abandoned"] == total["attempt_unknown"] == 1
    assert total["success"] == total["supplier_error"] == total["attempt_success"] == 0


def test_probe_cannot_borrow_business_task_budget(tmp_path):
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    with pytest.raises(PolicyDenied, match="OBSERVATION_LANE_MISMATCH"):
        c.probe("stock_zh_a_hist", lambda: pytest.fail("probe used business quota"), task_id="job")
    stats = c.statistics()
    assert stats["by_kind"]["probe"]["policy_denied"] == 1
    assert stats["by_kind"]["call"]["logical_calls"] == 0
    assert stats["totals"]["attempts"] == 0


@pytest.mark.reap_workers
def test_public_probe_worker_uses_shared_ledger_and_separate_probe_counts(tmp_path, db_session, monkeypatch):
    import time
    import pandas as pd
    from app.api.routes import akshare_apis as api
    c = configured(tmp_path)
    monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", c.path)
    monkeypatch.setattr(api.ak, "stock_zh_a_hist", lambda **kwargs: pd.DataFrame([{"close": 1}]))
    accepted = api.submit_probe("stock_zh_a_hist")
    until = time.monotonic() + 15
    while True:
        status = api.get_probe_status(accepted.task_id)
        if status.status in {"done", "failed", "cancelled"}:
            break
        assert time.monotonic() < until, status
        time.sleep(.02)
    assert status.status == "done" and status.result.success is True
    stats = c.statistics(include_records=True)
    assert stats["by_kind"]["probe"]["logical_calls"] == 1
    assert stats["by_kind"]["probe"]["attempts"] == 1
    assert stats["by_kind"]["call"]["logical_calls"] == 0
    assert stats["calls"][0]["task_id"] == accepted.task_id
    assert stats["calls"][0]["lane"] == "probe"


def test_disabled_policy_records_rejecting_versions_without_supplier_failure(tmp_path):
    from app.services.data_source_control import RequestPolicy, PolicyDenied
    c = configured(tmp_path)
    c.set_provider("eastmoney", RequestPolicy(enabled=False), expected_version=1)
    with pytest.raises(PolicyDenied, match="DISABLED"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("disabled"), task_id="job")
    stats = c.statistics(include_records=True)
    assert stats["totals"]["policy_denied"] == 1
    assert stats["totals"]["attempts"] == stats["totals"]["supplier_error"] == 0
    assert stats["calls"][0]["config_version"] == {"endpoint": 1, "provider": 2}
    assert stats["calls"][0]["provider"] == "eastmoney"


def test_worker_interruption_closes_observations_as_unknown_not_supplier_failure(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    started, release = threading.Event(), threading.Event()
    def supplier():
        started.set()
        assert release.wait(5)
        return 1
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(c.execute, "stock_zh_a_hist", supplier, task_id="job")
        try:
            assert started.wait(5)
            assert c.interrupt_task("job") == {"calls": 1, "attempts": 1}
            assert c.interrupt_task("job") == {"calls": 0, "attempts": 0}
        finally:
            release.set()
        with pytest.raises(PolicyDenied, match="CANCELLED|OBSERVATION_CLOSED"):
            future.result(timeout=5)
    total = c.statistics()["totals"]
    assert total["interrupted"] == total["attempt_unknown"] == 1
    assert total["success"] == total["supplier_error"] == 0


def test_two_failures_then_success_are_one_call_three_attempts_and_two_retries(tmp_path):
    c = configured(tmp_path)
    calls = []
    def supplier():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("temporary")
        return "ok"
    assert c.execute("stock_zh_a_hist", supplier, task_id="job", base_delay=0) == "ok"
    total = c.statistics()["totals"]
    assert total["logical_calls"] == total["success"] == 1
    assert total["execution_rounds"] == total["admissions"] == total["attempts"] == 3
    assert total["retry_count"] == total["attempt_supplier_error"] == 2
    assert total["supplier_error"] == 0


def test_final_transport_failure_is_not_counted_as_three_logical_calls(tmp_path):
    c = configured(tmp_path)
    with pytest.raises(ConnectionError):
        c.execute("stock_zh_a_hist", lambda: (_ for _ in ()).throw(ConnectionError("offline")), task_id="job", base_delay=0)
    total = c.statistics()["totals"]
    assert total["logical_calls"] == total["supplier_error"] == 1
    assert total["attempts"] == total["attempt_supplier_error"] == 3
    assert total["retry_count"] == 2


def test_business_rollback_does_not_erase_observation_or_certify_business_commit(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from app.services.akshare_utils import call_akshare_with_retry
    c = configured(tmp_path)
    engine = create_engine("sqlite:///" + (tmp_path / "business.sqlite").as_posix())
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE business_work (id INTEGER PRIMARY KEY)"))
        with Session(engine) as business:
            business.execute(text("INSERT INTO business_work VALUES (1)"))
            with c.scope("job"):
                assert call_akshare_with_retry(lambda: 7, api_key="stock_zh_a_hist", db=business) == 7
            business.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM business_work")) == 0
        stats = c.statistics(include_records=True)
        assert stats["totals"]["logical_calls"] == stats["totals"]["success"] == stats["totals"]["attempts"] == 1
        assert stats["calls"][0]["business_commit_status"] == "not_observed"
    finally:
        engine.dispose()


from contextlib import contextmanager


@contextmanager
def local_http(responder=None):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            status = responder(self.path, hits) if responder else 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"ok")
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
        thread.join(3)


def test_http_pages_and_logical_retry_are_separate_dimensions(tmp_path):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    c = configured(tmp_path, transport="requests", max_attempts=2)
    def responder(path, hits):
        return 503 if path == "/2" and hits.count("/2") == 1 else 200
    with local_http(responder) as (url, hits), c.scope("job"):
        def supplier():
            return [requests.get(url + page).text for page in ("/1", "/2")]
        assert call_akshare_with_retry(supplier, api_key="stock_zh_a_hist", base_delay=0) == ["ok", "ok"]
        assert hits == ["/1", "/2", "/1", "/2"]
    stats = c.statistics(include_records=True)
    assert stats["totals"]["logical_calls"] == stats["totals"]["success"] == 1
    assert stats["totals"]["execution_rounds"] == 2
    assert stats["totals"]["attempts"] == stats["totals"]["admissions"] == 4
    assert stats["totals"]["retry_count"] == stats["totals"]["attempt_supplier_error"] == 1
    assert [a["round_no"] for a in stats["attempts"]] == [1, 1, 2, 2]


def test_http_preflight_rejection_has_a_permit_but_no_physical_attempt(tmp_path):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    c = configured(tmp_path, transport="requests")
    with local_http() as (url, hits), c.scope("job"):
        with pytest.raises(ValueError):
            call_akshare_with_retry(lambda: requests.get(url, timeout=0), api_key="stock_zh_a_hist")
        assert hits == []
    total = c.statistics()["totals"]
    assert total["logical_calls"] == total["invalid_request"] == total["admissions"] == total["attempt_not_started"] == 1
    assert total["attempts"] == total["supplier_error"] == 0


def test_nested_wrapper_cannot_relabel_business_call_as_probe(tmp_path):
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    def supplier():
        return c.probe("stock_zh_a_hist", lambda: pytest.fail("relabelled"), task_id="job")
    with pytest.raises(PolicyDenied, match="OBSERVATION_LANE_MISMATCH"):
        c.execute("stock_zh_a_hist", supplier, task_id="job")
    stats = c.statistics()
    assert stats["by_kind"]["call"]["policy_denied"] == 1
    assert stats["by_kind"]["probe"]["logical_calls"] == 0


@pytest.mark.parametrize("case", ["contract", "format", "transport", "interrupt"])
def test_error_classes_remain_separate_and_messages_are_not_persisted(tmp_path, case):
    import json
    from app.services.data_contract_errors import DataContractError
    c = configured(tmp_path)
    secret = "SECRET-TEST-TOKEN-12345"
    error, expected = {"contract": (DataContractError(secret), "contract_error"),
                       "format": (ValueError(secret), "invalid_request"),
                       "transport": (ConnectionError(secret), "supplier_error"),
                       "interrupt": (KeyboardInterrupt(secret), "interrupted")}[case]
    with pytest.raises(type(error)):
        c.execute("stock_zh_a_hist", lambda: (_ for _ in ()).throw(error), task_id="job", max_attempts=1)
    stats = c.statistics(include_records=True)
    assert stats["totals"][expected] == stats["totals"]["attempt_" + expected] == 1
    assert stats["totals"]["retry_count"] == 0
    assert stats["totals"]["supplier_error"] == (1 if case == "transport" else 0)
    assert secret not in json.dumps(stats)


@pytest.mark.parametrize("shape", ["none", "list", "dataframe"])
def test_empty_probe_result_does_not_certify_data_quality_or_supplier_failure(tmp_path, shape):
    import pandas as pd
    c = configured(tmp_path)
    c.start_task("probe-job", lane="probe", max_requests=2, timeout_s=10)
    result = {"none": None, "list": [], "dataframe": pd.DataFrame()}[shape]
    with c.scope("probe-job"):
        c.probe("stock_zh_a_hist", lambda: result)
    stats = c.statistics(include_records=True)
    assert stats["by_kind"]["probe"]["empty_result"] == stats["totals"]["attempt_success"] == 1
    assert stats["by_kind"]["call"]["logical_calls"] == stats["totals"]["supplier_error"] == 0
    assert stats["calls"][0]["result_status"] == "empty"
    assert stats["calls"][0]["business_commit_status"] == "not_observed"


def test_same_endpoint_nested_wrapper_has_one_call_and_one_attempt(tmp_path):
    from app.services.akshare_utils import call_akshare_with_retry
    c = configured(tmp_path)
    with c.scope("job"):
        assert call_akshare_with_retry(lambda: call_akshare_with_retry(lambda: 7,
            api_key="stock_zh_a_hist"), api_key="stock_zh_a_hist") == 7
    total = c.statistics()["totals"]
    assert total["logical_calls"] == total["attempts"] == total["execution_rounds"] == 1


def test_parent_child_endpoints_are_linked_without_inventing_parent_http_attempt(tmp_path):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import RequestPolicy
    c = configured(tmp_path, transport="requests")
    c.set_endpoint("fund_etf_hist_em", "eastmoney", RequestPolicy(), transport="requests", allowed_hosts=("127.0.0.1",))
    with local_http() as (url, hits), c.scope("job"):
        call_akshare_with_retry(lambda: call_akshare_with_retry(lambda: requests.get(url),
            api_key="fund_etf_hist_em"), api_key="stock_zh_a_hist")
        assert len(hits) == 1
    stats = c.statistics(include_records=True)
    assert stats["totals"]["logical_calls"] == 2
    assert stats["totals"]["attempts"] == 1
    parent = next(r for r in stats["calls"] if r["endpoint"] == "stock_zh_a_hist")
    child = next(r for r in stats["calls"] if r["endpoint"] == "fund_etf_hist_em")
    assert child["parent_call_id"] == parent["id"]
    assert stats["attempts"][0]["call_id"] == child["id"]
    assert c.statistics(endpoint="stock_zh_a_hist")["totals"]["attempts"] == 0
    assert c.statistics(endpoint="fund_etf_hist_em", task_id="job", kind="call")["totals"]["logical_calls"] == 1


def test_quota_rejection_after_two_pages_does_not_become_supplier_failure(tmp_path):
    import requests
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path, transport="requests")
    c.start_task("two-pages", lane="backfill", max_requests=2, timeout_s=10)
    with local_http() as (url, hits), c.scope("two-pages"):
        with pytest.raises(PolicyDenied, match="TASK_QUOTA"):
            call_akshare_with_retry(lambda: [requests.get(url) for _ in range(3)], api_key="stock_zh_a_hist")
        assert len(hits) == 2
    total = c.statistics()["totals"]
    assert total["policy_denied"] == total["execution_rounds"] == 1
    assert total["attempts"] == total["attempt_success"] == 2
    assert total["supplier_error"] == total["attempt_supplier_error"] == 0


def test_statistics_unavailable_after_supplier_success_cannot_record_success(tmp_path):
    import sqlite3
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    blocker = sqlite3.connect(c.path)
    def supplier():
        blocker.execute("BEGIN IMMEDIATE")
        return 7
    try:
        with pytest.raises(PolicyDenied, match="CONTROL_STORE_UNAVAILABLE"):
            c.execute("stock_zh_a_hist", supplier, task_id="job")
    finally:
        blocker.rollback()
        blocker.close()
    total = c.statistics()["totals"]
    assert total["logical_calls"] == total["running"] == total["attempts"] == 1
    assert total["success"] == total["attempt_success"] == 0
    c.clock = lambda: 10**12
    assert c.reconcile_expired() == {"calls": 1, "attempts": 1}
    assert c.statistics()["totals"]["attempt_unknown"] == 1


def test_threaded_admissions_and_terminal_statistics_do_not_lose_increments(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path, total_limit=10)
    calls = []
    def invoke(_):
        try:
            return c.execute("stock_zh_a_hist", lambda: calls.append(1), task_id="job")
        except PolicyDenied as exc:
            assert str(exc) == "REQUEST_QUOTA"
            return "denied"
    with ThreadPoolExecutor(5) as pool:
        result = list(pool.map(invoke, range(20)))
    total = c.statistics()["totals"]
    assert len(calls) == total["success"] == total["attempts"] == 10
    assert result.count("denied") == total["policy_denied"] == 10
    assert total["logical_calls"] == 20
    assert total["running"] == total["supplier_error"] == 0


@pytest.mark.reap_workers
@pytest.mark.parametrize("case", ["disabled", "empty", "empty_dict", "none", "error"])
def test_public_probe_failure_categories_are_not_business_call_failures(tmp_path, db_session, monkeypatch, case):
    import time
    from app.api.routes import akshare_apis as api
    from app.services.data_source_control import RequestPolicy
    c = configured(tmp_path, max_attempts=1)
    if case == "disabled":
        c.set_provider("eastmoney", RequestPolicy(enabled=False), expected_version=1)
    secret = "PRIVATE-URL-KEY-DO-NOT-STORE"
    def supplier(**kwargs):
        if case == "disabled":
            pytest.fail("disabled probe supplier was called")
        if case == "error":
            raise ValueError(secret)
        if case == "empty_dict":
            return {}
        if case == "none":
            return None
        return []
    monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", c.path)
    monkeypatch.setattr(api.ak, "stock_zh_a_hist", supplier)
    accepted = api.submit_probe("stock_zh_a_hist")
    until = time.monotonic() + 15
    while True:
        status = api.get_probe_status(accepted.task_id)
        if status.status in {"done", "failed", "cancelled"}:
            break
        assert time.monotonic() < until
        time.sleep(.02)
    assert status.result is not None and status.result.success is False
    assert secret not in status.model_dump_json()
    total = c.statistics(kind="probe")["totals"]
    assert total[{"disabled": "policy_denied", "empty": "empty_result", "empty_dict": "empty_result", "none": "empty_result", "error": "invalid_request"}[case]] == 1
    assert total["attempts"] == (0 if case == "disabled" else 1)
    assert total["supplier_error"] == c.statistics(kind="call")["totals"]["logical_calls"] == 0


def test_additive_control_store_upgrade_preserves_old_usage_without_fabricating_history(tmp_path):
    import sqlite3
    import json
    import time
    from app.services.data_source_control import RequestController, RequestPolicy
    path = tmp_path / "v1-control.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE request_control_meta (version INTEGER NOT NULL)")
        db.execute("INSERT INTO request_control_meta VALUES (1)")
        db.execute("CREATE TABLE policies (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("CREATE TABLE state (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO state VALUES (?,?)", ("task:old-job", json.dumps({"lane": "incremental",
            "max_requests": 10, "timeout_s": 60, "deadline": time.time() + 60, "used": 5, "cancelled": False})))
    c = RequestController(path)
    assert c.statistics()["totals"]["logical_calls"] == 0
    c.set_provider("eastmoney", RequestPolicy())
    c.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy())
    c.execute("stock_zh_a_hist", lambda: 7, task_id="old-job")
    assert c.snapshot()["state"]["task:old-job"]["used"] == 6
    assert c.statistics()["totals"]["logical_calls"] == c.statistics()["totals"]["attempts"] == 1


def test_deadline_rejects_late_result_without_reporting_transport_failure(tmp_path):
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path, timeout_s=1)
    now = [1000.0]
    c.clock = lambda: now[0]
    def supplier():
        now[0] += 2
        return 7
    with pytest.raises(PolicyDenied, match="DEADLINE"):
        c.execute("stock_zh_a_hist", supplier, task_id="job")
    total = c.statistics()["totals"]
    assert total["policy_denied"] == total["attempt_policy_denied"] == total["attempts"] == 1
    assert total["success"] == total["supplier_error"] == 0


def test_unknown_endpoint_and_missing_task_have_zero_actual_attempts(tmp_path):
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    with pytest.raises(PolicyDenied, match="ENDPOINT_UNCONFIGURED"):
        c.execute("unknown", lambda: pytest.fail("unknown endpoint"), task_id="job")
    with pytest.raises(PolicyDenied, match="TASK_UNCONFIGURED"):
        c.execute("stock_zh_a_hist", lambda: pytest.fail("missing task"))
    total = c.statistics()["totals"]
    assert total["logical_calls"] == total["policy_denied"] == 2
    assert total["admissions"] == total["attempts"] == total["supplier_error"] == 0


def test_scope_cancellation_callback_survives_statistics_wrapper(tmp_path):
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    with c.scope("job", cancelled=lambda: True), pytest.raises(PolicyDenied, match="CANCELLED"):
        call_akshare_with_retry(lambda: pytest.fail("lost cancellation context"), api_key="stock_zh_a_hist")
    total = c.statistics()["totals"]
    assert total["policy_denied"] == 1
    assert total["attempts"] == total["admissions"] == 0


def test_source_fallback_is_one_selection_linked_to_two_calls_not_a_retry(tmp_path):
    from datetime import date
    from types import SimpleNamespace
    import pandas as pd
    from app.services.market_data_sources.chain import SourceChain
    from app.services.market_data_sources.sources.base import SourceUnavailable
    from app.services.akshare_utils import call_akshare_with_retry
    from app.services.data_source_control import RequestPolicy
    c = configured(tmp_path, max_attempts=1)
    c.set_provider("sina", RequestPolicy(max_attempts=1))
    c.set_endpoint("stock_zh_a_daily", "sina", RequestPolicy(max_attempts=1))
    class Supplier:
        def __init__(self, name, endpoint, fails):
            self.name, self.endpoint, self.fails = name, endpoint, fails
        def available(self):
            return True
        def supports(self, symbol):
            return True
        def fetch(self, *args):
            def fetch():
                if self.fails:
                    raise ConnectionError("offline")
                return pd.DataFrame([{"close": 7}])
            try:
                return call_akshare_with_retry(fetch, api_key=self.endpoint, base_delay=0)
            except ConnectionError as exc:
                raise SourceUnavailable("offline") from exc
    chain = SourceChain([Supplier("em_stock", "stock_zh_a_hist", True), Supplier("sina_stock", "stock_zh_a_daily", False)])
    with c.scope("job"):
        result = chain.fetch_with_lineage(SimpleNamespace(symbol="600000"), date(2026, 1, 1), date(2026, 1, 2), "")
    assert result.fallback_used is True and result.source_name == "sina_stock"
    stats = c.statistics(include_records=True)
    assert stats["totals"]["selections"] == stats["totals"]["fallbacks"] == 1
    assert stats["totals"]["logical_calls"] == stats["totals"]["attempts"] == 2
    assert stats["totals"]["retry_count"] == 0
    selection = stats["selections"][0]
    assert selection["attempted_sources"] == ["em_stock", "sina_stock"]
    assert selection["failed_sources"] == ["em_stock"]
    assert all(call["selection_id"] == selection["id"] for call in stats["calls"])


def test_source_selection_does_not_swallow_statistics_write_failure_as_unavailability(tmp_path):
    import sqlite3
    import threading
    from datetime import date
    from types import SimpleNamespace
    from app.services.market_data_sources.chain import SourceChain
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    blocker = sqlite3.connect(c.path, check_same_thread=False)
    timer = None
    def unavailable():
        nonlocal timer
        blocker.execute("BEGIN IMMEDIATE")
        timer = threading.Timer(.7, blocker.rollback)
        timer.start()
        return False
    first = SimpleNamespace(name="first", available=unavailable)
    second = SimpleNamespace(name="second", available=lambda: True, supports=lambda symbol: True,
                             fetch=lambda *args: pytest.fail("statistics failure became source failover"))
    try:
        with c.scope("job"), pytest.raises(PolicyDenied, match="CONTROL_STORE_UNAVAILABLE"):
            SourceChain([first, second]).fetch_with_lineage(SimpleNamespace(symbol="600000"), date(2026, 1, 1), date(2026, 1, 2), "")
    finally:
        if timer:
            timer.join(3)
        blocker.close()


@pytest.mark.parametrize("case", ["unsupported", "unavailable", "all_failed", "contract", "cancelled"])
def test_selection_outcomes_preserve_skip_failure_and_contract_meanings(tmp_path, case):
    from datetime import date
    from types import SimpleNamespace
    import pandas as pd
    from app.services.market_data_sources.chain import SourceChain
    from app.services.market_data_sources.sources.base import SourceUnavailable
    from app.services.data_contract_errors import DataContractError
    from app.services.data_source_control import PolicyDenied
    c = configured(tmp_path)
    cancelled = [False]
    def fetch_first(*args):
        if case == "contract":
            raise DataContractError("SECRET-CONTRACT-TEXT")
        if case == "cancelled":
            cancelled[0] = True
            return pd.DataFrame([{"close": 7}])
        raise SourceUnavailable("SECRET-UNAVAILABLE-TEXT")
    def fetch_second(*args):
        if case == "all_failed":
            raise SourceUnavailable("SECRET-UNAVAILABLE-TEXT")
        return pd.DataFrame([{"close": 7}])
    first = SimpleNamespace(name="first", available=lambda: case != "unavailable",
                            supports=lambda symbol: case != "unsupported", fetch=fetch_first)
    second = SimpleNamespace(name="second", available=lambda: True, supports=lambda symbol: True, fetch=fetch_second)
    with c.scope("job", cancelled=lambda: cancelled[0]):
        chain = SourceChain([first, second])
        args = (SimpleNamespace(symbol="600000"), date(2026, 1, 1), date(2026, 1, 2), "")
        if case in {"all_failed", "contract", "cancelled"}:
            with pytest.raises({"all_failed": RuntimeError, "contract": DataContractError, "cancelled": PolicyDenied}[case]):
                chain.fetch_with_lineage(*args)
        else:
            assert chain.fetch_with_lineage(*args).fallback_used is (case == "unavailable")
    stats = c.statistics(include_records=True)
    total = stats["totals"]
    assert total["selections"] == 1
    assert total["logical_calls"] == total["attempts"] == 0  # fake HistorySources do not invent HTTP attempts
    assert total["fallbacks"] == (1 if case == "unavailable" else 0)
    assert total["selection_contract_errors"] == (1 if case == "contract" else 0)
    assert total["selection_policy_denials"] == (1 if case == "cancelled" else 0)
    import json
    assert "SECRET-" not in json.dumps(stats)


def stats_process_worker(path, folder, index, url):
    import json
    import os
    import time
    from pathlib import Path
    import requests
    from app.services.data_source_control import RequestController
    from app.services.akshare_utils import call_akshare_with_retry
    folder = Path(folder)
    c = RequestController(path)
    (folder / f"ready-{index}").write_text(str(os.getpid()))
    until = time.monotonic() + 20
    while not (folder / "go").exists():
        if time.monotonic() > until:
            raise RuntimeError("coordination timed out")
        time.sleep(.01)
    with c.scope("job"):
        for _ in range(2):
            assert call_akshare_with_retry(lambda: requests.get(url).text, api_key="stock_zh_a_hist") == "ok"
    (folder / f"done-{index}").write_text(json.dumps({"pid": os.getpid()}))


def test_independent_processes_account_for_actual_http_requests_exactly_once(tmp_path):
    import json
    import subprocess
    import sys
    import time
    from pathlib import Path
    c = configured(tmp_path, transport="requests")
    folder = tmp_path / "processes"
    folder.mkdir()
    workers, logs = [], []
    with local_http() as (url, hits):
        try:
            for i in range(4):
                log = (folder / f"worker-{i}.log").open("w")
                logs.append(log)
                workers.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--stats-worker", c.path, str(folder), str(i), url], stdout=log, stderr=subprocess.STDOUT))
            until = time.monotonic() + 25
            while len(list(folder.glob("ready-*"))) < 4:
                assert time.monotonic() < until
                time.sleep(.01)
            (folder / "go").write_text("go")
            for worker in workers:
                assert worker.wait(timeout=20) == 0
            assert len(hits) == 8
            pids = [json.loads(p.read_text())["pid"] for p in folder.glob("done-*")]
            assert len(set(pids)) == 4
            stats = c.statistics(include_records=True)
            assert stats["totals"]["logical_calls"] == stats["totals"]["success"] == stats["totals"]["attempts"] == 8
            assert stats["totals"]["running"] == stats["totals"]["retry_count"] == 0
            assert len({a["call_id"] for a in stats["attempts"]}) == len({a["id"] for a in stats["attempts"]}) == 8
            (tmp_path / "observations.json").write_text(json.dumps({"worker_pids": pids, "http_hits": len(hits),
                "statistics": stats, "task_used": c.snapshot()["state"]["task:job"]["used"]}, indent=2))
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.terminate()
                worker.wait(timeout=5)
            for log in logs:
                log.close()


if __name__ == "__main__":
    import sys
    if sys.argv[1] != "--stats-worker":
        raise SystemExit(2)
    stats_process_worker(sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5])
