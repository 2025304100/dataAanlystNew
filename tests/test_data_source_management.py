"""ST-M04.4 at the management HTTP API and real shared execution boundary."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.whitebox


@pytest.fixture(autouse=True)
def preserve_legacy_cache():
    from app.services.akshare_registry import snapshot_config_cache, restore_config_cache
    before = snapshot_config_cache()
    yield
    restore_config_cache(before)


@pytest.fixture
def client(db_session):
    from app.api.routes import akshare_apis
    from app.db.session import get_db
    app = FastAPI()
    app.include_router(akshare_apis.router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def test_catalog_displays_planned_sources_without_claiming_production_readiness(client):
    response = client.get("/external-data/apis")
    assert response.status_code == 200
    rows = {r["key"]: r for r in response.json()}
    assert len(rows) == 71
    for key, access, auth in [("PLAN:Fuyao", "permission_unverified", "unconfigured"),
                              ("PLAN:iFinD", "permission_unverified", "unconfigured"),
                              ("PLAN:BSE", "planned_public", "not_required")]:
        g = rows[key]["governance"]
        assert g["access_status"] == access and g["auth_status"] == auth
        assert g["in_current_release"] is False
        assert g["admission_status"] == "not_reviewed"
        assert g["production_request_allowed"] is False
        assert g["can_probe"] is False and g["can_edit"] is False


@pytest.fixture
def shared(tmp_path, monkeypatch):
    from app.services.data_source_control import RequestController, RequestPolicy
    c = RequestController(tmp_path / "control.sqlite")
    c.set_provider("eastmoney", RequestPolicy(max_attempts=1))
    c.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(max_attempts=1),
                   transport="requests", allowed_hosts=("127.0.0.1",))
    monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", c.path)
    return c


def test_shared_view_uses_authoritative_config_and_keeps_old_counts_separate(client, db_session, shared):
    from app.models.akshare_api_config import AkshareApiConfig
    from app.services.data_source_control import RequestPolicy
    db_session.add(AkshareApiConfig(api_key="stock_zh_a_hist", enabled=False, total_calls=99, total_failures=50))
    db_session.commit()
    shared.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(), expected_version=1)
    shared.start_task("call", lane="incremental", max_requests=3, timeout_s=20)
    shared.execute("stock_zh_a_hist", lambda: 7, task_id="call")
    shared.start_task("probe", lane="probe", max_requests=2, timeout_s=20)
    shared.probe("stock_zh_a_hist", lambda: [7], task_id="probe")
    row = next(r for r in client.get("/external-data/apis").json() if r["key"] == "stock_zh_a_hist")
    g = row["governance"]
    assert row["enabled"] is True
    assert g["control_mode"] == "shared"
    assert g["config_version"] == {"endpoint": 2, "provider": 1}
    assert g["statistics"]["by_kind"]["call"]["logical_calls"] == 1
    assert g["statistics"]["by_kind"]["probe"]["logical_calls"] == 1
    assert g["legacy_statistics"]["total_calls"] == 99
    assert row["total_calls"] == 1
    assert g["health_status"] == "observed_success"
    assert g["admission_status"] == "not_reviewed" and g["production_request_allowed"] is False


def test_policy_publication_is_versioned_atomic_and_rejects_stale_clients(client, db_session, shared):
    payload = {"scope": "endpoint", "expected_version": {"endpoint": 1, "provider": 1},
               "policy": {"enabled": False, "max_attempts": 2, "timeout_s": 4}}
    response = client.put("/external-data/apis/stock_zh_a_hist/policy", json=payload)
    assert response.status_code == 200, response.text
    g = response.json()["governance"]
    assert g["config_version"] == {"endpoint": 2, "provider": 1}
    assert g["control"]["endpoint_policy"]["max_attempts"] == 2
    from app.services.data_source_control import RequestController, PolicyDenied
    other = RequestController(shared.path)
    other.start_task("later", lane="incremental", max_requests=2, timeout_s=20)
    with pytest.raises(PolicyDenied, match="DISABLED"):
        other.execute("stock_zh_a_hist", lambda: pytest.fail("published disable not enforced"), task_id="later")
    stale = client.put("/external-data/apis/stock_zh_a_hist/policy", json=payload)
    assert stale.status_code == 409
    history = client.get("/external-data/apis/stock_zh_a_hist/configuration-history?scope=endpoint")
    assert history.status_code == 200
    assert [r["version"] for r in history.json()] == [2, 1]
    assert history.json()[1]["configuration"]["policy"]["enabled"] is True


@pytest.mark.parametrize("key", ["PLAN:Fuyao", "PLAN:iFinD", "PLAN:BSE", "stock_zh_a_hist_min_em"])
def test_out_of_release_sources_cannot_be_enabled_or_probed(client, key):
    assert client.put(f"/external-data/apis/{key}", json={"enabled": True}).status_code in {403, 404}
    assert client.post(f"/external-data/apis/{key}/probe").status_code in {403, 404}
    from app.services.akshare_registry import is_api_enabled
    assert is_api_enabled(key) is False


def test_shared_mode_rejects_old_unversioned_configuration_write(client, shared):
    r = client.put("/external-data/apis/stock_zh_a_hist", json={"enabled": False})
    assert r.status_code == 409
    assert shared.configuration("stock_zh_a_hist")["endpoint"]["version"] == 1


def test_provider_change_and_restore_publish_new_versions_without_erasing_history(client, shared):
    first = {"scope": "provider", "expected_version": {"endpoint": 1, "provider": 1}, "policy": {"enabled": False}}
    r = client.put("/external-data/apis/stock_zh_a_hist/policy", json=first)
    assert r.status_code == 200 and r.json()["enabled"] is False
    stale = {"scope": "endpoint", "expected_version": {"endpoint": 1, "provider": 1}, "policy": {"enabled": False}}
    assert client.put("/external-data/apis/stock_zh_a_hist/policy", json=stale).status_code == 409
    restore = {"scope": "provider", "expected_version": {"endpoint": 1, "provider": 2}, "restore_version": 1}
    result = client.put("/external-data/apis/stock_zh_a_hist/policy", json=restore)
    assert result.status_code == 200 and result.json()["enabled"] is True
    assert result.json()["governance"]["config_version"] == {"endpoint": 1, "provider": 3}
    history = client.get("/external-data/apis/stock_zh_a_hist/configuration-history?scope=provider").json()
    assert [r["version"] for r in history] == [3, 2, 1]
    assert history[1]["configuration"]["policy"]["enabled"] is False
    assert history[0]["origin"] == "restore:1"


@pytest.mark.parametrize("patch", [{"max_attempts": True}, {"timeout_s": 0}, {"min_interval_s": -1},
    {"total_limit": -1}, {"production_enabled": True}, {"auth_status": "verified"}, {"unit_multiplier": 100},
    {"cooldown_s": 1000, "max_cooldown_s": 1}])
def test_invalid_or_readonly_policy_changes_never_publish(client, shared, patch):
    payload = {"expected_version": {"endpoint": 1, "provider": 1}, "policy": patch}
    assert client.put("/external-data/apis/stock_zh_a_hist/policy", json=payload).status_code == 400
    assert shared.configuration("stock_zh_a_hist")["endpoint"]["version"] == 1
    assert len(shared.configuration_history("stock_zh_a_hist")) == 1


def test_policy_failure_rolls_back_pointer_and_history_together(client, shared):
    import sqlite3
    with sqlite3.connect(shared.path) as db:
        db.execute("CREATE TRIGGER reject_history BEFORE INSERT ON request_policy_versions WHEN NEW.version > 1 BEGIN SELECT RAISE(ABORT,'test history failure'); END")
    payload = {"expected_version": {"endpoint": 1, "provider": 1}, "policy": {"enabled": False}}
    assert client.put("/external-data/apis/stock_zh_a_hist/policy", json=payload).status_code == 503
    assert shared.configuration("stock_zh_a_hist")["endpoint"]["policy"]["enabled"] is True
    assert shared.configuration_history("stock_zh_a_hist")[0]["version"] == 1


def test_missing_store_is_not_created_or_replaced_by_legacy_config(client, tmp_path, monkeypatch):
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("DATA_SOURCE_CONTROL_DB", str(missing))
    assert client.get("/external-data/apis").status_code == 503
    assert not missing.exists()


def test_local_tdx_and_fred_csv_do_not_get_fake_account_or_network_metrics(client):
    rows = {r["key"]: r["governance"] for r in client.get("/external-data/apis").json()}
    assert rows["TDX:day"]["auth_status"] == "not_required"
    assert rows["TDX:day"]["network_metrics_applicable"] is False
    assert rows["TDX:day"]["supplier_quota_status"] is None
    assert rows["fred:CPIAUCSL"]["auth_required"] is False
    assert rows["fred:CPIAUCSL"]["auth_status"] == "not_required"
    assert rows["fred:CPIAUCSL"]["supplier_quota_remaining"] is None


def test_shared_publication_does_not_depend_on_business_session_and_view_labels_unavailable_history(client, shared):
    from app.db.session import get_db
    from sqlalchemy.exc import OperationalError
    class BrokenBusiness:
        def execute(self, *args):
            raise OperationalError("SECRET-DB-DIAGNOSTIC", {}, Exception("private"))
        def rollback(self):
            pass
    client.app.dependency_overrides[get_db] = lambda: BrokenBusiness()
    payload = {"expected_version": {"endpoint": 1, "provider": 1}, "policy": {"timeout_s": 12}}
    assert client.put("/external-data/apis/stock_zh_a_hist/policy", json=payload).status_code == 200
    response = client.get("/external-data/apis")
    assert response.status_code == 200 and "SECRET" not in response.text
    row = next(r for r in response.json() if r["key"] == "stock_zh_a_hist")
    assert row["governance"]["legacy_statistics"]["available"] is False
    assert row["governance"]["legacy_statistics"]["total_calls"] is None


def test_policy_restore_preserves_request_usage(client, shared):
    from app.services.data_source_control import PolicyDenied, RequestPolicy
    shared.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(), expected_version=1)
    shared.start_task("one", lane="incremental", max_requests=1, timeout_s=30)
    shared.execute("stock_zh_a_hist", lambda: 7, task_id="one")
    payload = {"expected_version": {"endpoint": 2, "provider": 1}, "restore_version": 1}
    assert client.put("/external-data/apis/stock_zh_a_hist/policy", json=payload).status_code == 200
    with pytest.raises(PolicyDenied, match="TASK_QUOTA"):
        shared.execute("stock_zh_a_hist", lambda: pytest.fail("restore replenished budget"), task_id="one")
    assert shared.snapshot()["state"]["task:one"]["used"] == 1
    assert shared.configuration("stock_zh_a_hist")["endpoint"]["transport"] == "callable"


def test_old_store_records_only_current_configuration_not_imagined_past(shared):
    import sqlite3
    from app.services.data_source_control import RequestController, RequestPolicy
    shared.set_endpoint("stock_zh_a_hist", "eastmoney", RequestPolicy(max_attempts=2), expected_version=1)
    with sqlite3.connect(shared.path) as db:
        db.execute("DROP TABLE request_policy_versions")
    reopened = RequestController(shared.path)
    history = reopened.configuration_history("stock_zh_a_hist")
    assert [r["version"] for r in history] == [2]
    assert history[0]["origin"] == "import_current_only"


def test_concurrent_publishers_have_one_winner_and_one_history_version(client, shared):
    from concurrent.futures import ThreadPoolExecutor
    def publish(value):
        return client.put("/external-data/apis/stock_zh_a_hist/policy", json={"expected_version": {"endpoint": 1, "provider": 1}, "policy": {"timeout_s": value}}).status_code
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(publish, [10, 20]))
    assert sorted(statuses) == [200, 409]
    assert [r["version"] for r in shared.configuration_history("stock_zh_a_hist")] == [2, 1]


def test_legacy_custom_strategy_partial_update_validates_effective_range(client):
    assert client.put("/external-data/apis/stock_zh_a_hist", json={"anti_risk_strategy": "custom", "delay_min_ms": 100, "delay_max_ms": 200}).status_code == 200
    assert client.put("/external-data/apis/stock_zh_a_hist", json={"delay_min_ms": 500}).status_code == 400


def test_old_control_residue_cannot_execute_planned_sources(shared):
    import sqlite3
    from app.services.data_source_control import PolicyDenied
    with sqlite3.connect(shared.path) as db:
        body = db.execute("SELECT body FROM policies WHERE key='endpoint:stock_zh_a_hist'").fetchone()[0]
        db.execute("INSERT INTO policies VALUES ('endpoint:PLAN:Fuyao',?)", (body,))
    shared.start_task("old-residue", lane="incremental", max_requests=3, timeout_s=20)
    with pytest.raises(PolicyDenied, match="ENDPOINT_OUT_OF_RELEASE"):
        shared.execute("PLAN:Fuyao", lambda: pytest.fail("planned source executed"), task_id="old-residue")
