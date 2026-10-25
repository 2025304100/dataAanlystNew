"""Approved P/M/O regression seams: real HTTP writes and read-back, isolated DB."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.symbol import Symbol

pytestmark = pytest.mark.whitebox


@pytest.fixture()
def api(db_session):
    from app.main import app
    def override():
        yield db_session
    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def symbols(db_session):
    rows = [Symbol(symbol=f"60098{i}", name=f"Regression {i}", asset_type="stock", market="cn") for i in range(2)]
    db_session.add_all(rows)
    db_session.commit()
    return [row.id for row in rows]


def make_portfolio(api, **changes):
    payload = dict(name="Combination regression", account_type="manual", total_capital=100000,
                   investable_ratio=.8, cash_reserve_ratio=.2)
    payload.update(changes)
    response = api.post("/api/v1/portfolios", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def read_portfolio(api, portfolio_id):
    rows = api.get("/api/v1/portfolios?include_test=true&auto_tag_tests=false").json()
    return next(row for row in rows if row["id"] == portfolio_id)


def test_p07_p08_key_members_roundtrip_and_explicit_clear(api, symbols):
    p = make_portfolio(api, key_members_json=list(reversed(symbols)))
    path = f"/api/v1/portfolios/{p['id']}"
    assert read_portfolio(api, p["id"])["key_members_json"] == list(reversed(symbols))
    assert api.put(path, json={"key_members_json": symbols[:1]}).status_code == 200
    assert api.put(path, json={"name": "Renamed regression"}).status_code == 200
    assert read_portfolio(api, p["id"])["key_members_json"] == symbols[:1]
    assert api.put(path, json={"key_members_json": []}).json()["key_members_json"] == []
    assert api.put(path, json={"key_members_json": None}).json()["key_members_json"] is None


def test_m03_m05_execution_mode_is_canonical_and_blank_cannot_mutate(api, symbols):
    p = make_portfolio(api)
    path = f"/api/v1/portfolios/{p['id']}/members"
    for empty in ("", "  "):
        rejected = api.post(path, json={"symbol_id": symbols[0], "execution_mode": empty})
        assert rejected.status_code == 422, rejected.text
        assert api.get(path).json() == []
    member = api.post(path, json={"symbol_id": symbols[0], "execution_mode": " AUTO "}).json()
    assert member["execution_mode"] == "auto"
    member_path = f"{path}/{member['id']}"
    for empty in ("", "  "):
        assert api.patch(member_path, json={"execution_mode": empty, "note": "must not save"}).status_code == 422
        assert api.get(path).json()[0]["execution_mode"] == "auto"
        assert api.get(path).json()[0]["note"] is None
    assert api.patch(member_path, json={"execution_mode": " Manual "}).json()["execution_mode"] == "manual"
    assert api.patch(member_path, json={"execution_mode": None}).json()["execution_mode"] == "manual"
    for unavailable in ("confirm", "CONFIRM", " confirm "):
        assert api.patch(member_path, json={"execution_mode": unavailable}).status_code == 503


@pytest.mark.parametrize("field,value", [
    ("total_capital", -1), ("total_capital", 1e308), ("total_capital", True),
    ("investable_ratio", 1.01), ("cash_reserve_ratio", -.01),
    ("buy_fee_pct", -.001), ("sell_fee_pct", "NaN"),
    ("default_single_position_pct", 1.01), ("name", "  "), ("name", "x" * 129),
    ("currency", "not-a-currency"),
    ("benchmark_code", "x" * 33), ("benchmark_code", "  "),
])
def test_p03_p06_invalid_portfolio_fields_are_rejected_atomically(api, field, value):
    p = make_portfolio(api)
    before = read_portfolio(api, p["id"])
    response = api.put(f"/api/v1/portfolios/{p['id']}", json={"name": "must not save", field: value})
    assert response.status_code == 422, response.text
    assert read_portfolio(api, p["id"]) == before
    create = dict(name="Invalid second portfolio", account_type="manual", total_capital=100000,
                  investable_ratio=.8, cash_reserve_ratio=.2)
    create[field] = value
    assert api.post("/api/v1/portfolios", json=create).status_code == 422


def test_text_storage_boundaries_reject_complete_requests_and_preserve_unicode(api, symbols, db_session):
    import json
    from app.models.portfolio import Portfolio
    from app.services.portfolio_members import create_member, update_member

    p = make_portfolio(api, benchmark_code=" x " )
    base = f"/api/v1/portfolios/{p['id']}"
    assert p["benchmark_code"] == "x"
    assert api.put(base, json={"benchmark_code": "x" * 32}).json()["benchmark_code"] == "x" * 32
    note_at_limit = "中" * 21845
    member = api.post(f"{base}/members", json={"symbol_id": symbols[0], "note": note_at_limit}).json()
    assert member["note"] == note_at_limit
    before = api.get(f"{base}/members?include_archived=true").json()
    too_long = note_at_limit + "x"
    assert api.patch(f"{base}/members/{member['id']}", json={"note": too_long, "status": "paused"}).status_code == 422
    assert api.post(f"{base}/members", json={"symbol_id": symbols[1], "note": too_long}).status_code == 422
    assert api.get(f"{base}/members?include_archived=true").json() == before
    with pytest.raises(ValueError, match="65535"):
        update_member(db_session, member_id=member["id"], status="paused", note=too_long)
    with pytest.raises(ValueError, match="65535"):
        create_member(db_session, portfolio_id=p["id"], symbol_id=symbols[1], note=too_long)
    assert api.get(f"{base}/members?include_archived=true").json() == before
    oversized_keys = list(range(1, 12000))
    assert len(json.dumps(oversized_keys).encode("utf-8")) > 65535
    before_portfolio = read_portfolio(api, p["id"])
    rejected = api.put(base, json={"name": "must not save", "key_members_json": oversized_keys})
    assert rejected.status_code == 422
    assert "65535" in rejected.text
    assert read_portfolio(api, p["id"]) == before_portfolio
    count = db_session.query(Portfolio).count()
    rejected_create = api.post("/api/v1/portfolios", json=dict(name="must not create", account_type="manual",
        total_capital=0, investable_ratio=1, cash_reserve_ratio=.05, key_members_json=oversized_keys))
    assert rejected_create.status_code == 422
    assert db_session.query(Portfolio).count() == count


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "1e309"])
def test_raw_json_nonfinite_numbers_return_422_without_any_write(api, db_session, literal):
    from app.models.portfolio import Portfolio

    p = make_portfolio(api)
    before = read_portfolio(api, p["id"])
    headers = {"Content-Type": "application/json"}
    rejected = api.put(f"/api/v1/portfolios/{p['id']}",
        content='{"name":"must not save","total_capital":' + literal + '}', headers=headers)
    assert rejected.status_code == 422, rejected.text
    assert read_portfolio(api, p["id"]) == before
    count = db_session.query(Portfolio).count()
    rejected = api.post("/api/v1/portfolios", content='{"name":"must not create","account_type":"manual",'
        '"investable_ratio":1,"cash_reserve_ratio":0.05,"total_capital":' + literal + '}', headers=headers)
    assert rejected.status_code == 422, rejected.text
    assert db_session.query(Portfolio).count() == count


def test_archived_create_accepts_explicit_timezone_without_losing_interval(api, symbols):
    p = make_portfolio(api)
    response = api.post(f"/api/v1/portfolios/{p['id']}/members", json={"symbol_id": symbols[0],
        "status": "archived", "effective_from": "2026-01-01T08:00:00+08:00"})
    assert response.status_code == 200, response.text
    assert response.json()["effective_from"].startswith("2026-01-01T00:00:00")
    assert response.json()["effective_to"] is not None


def test_p09_key_members_must_be_unambiguous_existing_symbols(api, symbols):
    p = make_portfolio(api, key_members_json=symbols)
    before = read_portfolio(api, p["id"])
    for invalid in ([symbols[0], symbols[0]], [999999999], [0], [-1], [str(symbols[0])], [True]):
        response = api.put(f"/api/v1/portfolios/{p['id']}", json={"name": "must not save", "key_members_json": invalid})
        assert response.status_code == 422, response.text
        assert read_portfolio(api, p["id"]) == before


def test_m07_patch_cannot_archive_a_holding_or_partially_save(api, symbols):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}"
    member = api.post(f"{base}/members", json={"symbol_id": symbols[0]}).json()
    assert api.post(f"{base}/positions", json={"symbol_id": symbols[0], "quantity": 100, "avg_cost": 10, "latest_price": 10}).status_code == 200
    before_members = api.get(f"{base}/members?include_archived=true").json()
    before_positions = api.get(f"{base}/positions").json()
    response = api.patch(f"{base}/members/{member['id']}", json={"status": "archived", "note": "must not save"})
    assert response.status_code == 409, response.text
    assert api.get(f"{base}/members?include_archived=true").json() == before_members
    assert api.get(f"{base}/positions").json() == before_positions


def test_m08_m10_archive_retains_history_and_reactivation_cannot_conflict(api, symbols):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}/members"
    old = api.post(base, json={"symbol_id": symbols[0], "execution_mode": "auto"}).json()
    closed = api.patch(f"{base}/{old['id']}", json={"status": "archived"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["effective_to"] is not None
    assert api.get(base).json() == []
    current = api.post(base, json={"symbol_id": symbols[0]}).json()
    assert current["id"] != old["id"]
    before = api.get(f"{base}?include_archived=true").json()
    assert len(before) == 2
    for status in ("active", "paused"):
        assert api.patch(f"{base}/{old['id']}", json={"status": status, "note": "must not save"}).status_code == 409
    assert api.post(f"{base}/{old['id']}/restore").status_code == 409
    assert api.get(f"{base}?include_archived=true").json() == before


def test_m11_priority_bounds_match_create_and_update(api, symbols):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}/members"
    member = api.post(base, json={"symbol_id": symbols[0]}).json()
    for priority in (-1, 101):
        response = api.patch(f"{base}/{member['id']}", json={"priority": priority, "note": "must not save"})
        assert response.status_code == 422, response.text
        assert api.get(base).json()[0]["note"] is None
    for priority in (0, 100):
        assert api.patch(f"{base}/{member['id']}", json={"priority": priority}).json()["priority"] == priority


def test_m15_duplicate_member_offers_edit_instead_of_retry(api, symbols):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}/members"
    member = api.post(base, json={"symbol_id": symbols[0]}).json()
    response = api.post(base, json={"symbol_id": symbols[0]})
    assert response.status_code == 409
    error = response.json()
    assert error["error_code"] == "MEMBER_ALREADY_EXISTS"
    assert error["retryable"] is False
    assert error["extras"]["existing_member_id"] == member["id"]
    assert error["extras"]["existing_member_status"] == "active"
    assert all(action["action_type"] != "retry" and "readiness" not in action["label"] for action in error["next_actions"])
    assert any("成员" in action["label"] for action in error["next_actions"])


def test_member_lifecycle_aliases_keep_guards_and_report_conflicts(api, symbols):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}"
    assert api.post(f"{base}/positions", json={"symbol_id": symbols[0], "quantity": 100, "avg_cost": 10}).status_code == 200
    blocked = api.post(f"{base}/members", json={"symbol_id": symbols[0], "status": "archived"})
    assert blocked.status_code == 409
    assert api.get(f"{base}/members?include_archived=true").json() == []
    old = api.post(f"{base}/members", json={"symbol_id": symbols[1]}).json()
    api.post(f"{base}/members/{old['id']}/archive")
    api.post(f"{base}/members", json={"symbol_id": symbols[1]})
    before = api.get(f"{base}/members?include_archived=true").json()
    assert api.post(f"{base}/members/{old['id']}/pause").status_code == 409
    assert api.get(f"{base}/members?include_archived=true").json() == before


@pytest.mark.parametrize("invalid", [True, 1.0, "1", 0, -1])
def test_member_symbol_identity_cannot_be_coerced_to_another_target(api, symbols, invalid):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}/members"
    response = api.post(base, json={"symbol_id": invalid, "note": "must not save"})
    assert response.status_code == 422, response.text
    assert api.get(f"{base}?include_archived=true").json() == []


@pytest.mark.parametrize("invalid", [True, 1.0, "1"])
def test_member_priority_requires_an_integer_without_partial_create_or_update(api, symbols, invalid):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}/members"
    rejected = api.post(base, json={"symbol_id": symbols[0], "priority": invalid, "note": "must not save"})
    assert rejected.status_code == 422, rejected.text
    assert api.get(base).json() == []
    member = api.post(base, json={"symbol_id": symbols[0]}).json()
    before = api.get(base).json()
    rejected = api.patch(f"{base}/{member['id']}", json={"status": "paused", "priority": invalid, "note": "must not save"})
    assert rejected.status_code == 422, rejected.text
    assert api.get(base).json() == before


@pytest.mark.parametrize("field", ["source_id", "entry_rule_version_id", "exit_rule_version_id"])
def test_member_optional_identity_rejects_boolean_without_changing_other_contracts(api, symbols, field):
    p = make_portfolio(api)
    base = f"/api/v1/portfolios/{p['id']}/members"
    for value in (True, False):
        rejected = api.post(base, json={"symbol_id": symbols[0], field: value, "note": "must not save"})
        assert rejected.status_code == 422, rejected.text
        assert api.get(base).json() == []
    if field != "source_id":
        member = api.post(base, json={"symbol_id": symbols[0]}).json()
        before = api.get(base).json()
        for value in (True, False):
            rejected = api.patch(f"{base}/{member['id']}", json={field: value, "status": "paused", "note": "must not save"})
            assert rejected.status_code == 422, rejected.text
            assert api.get(base).json() == before
