"""Unsupported alert options must never be accepted as operational rules."""
import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db


@pytest.fixture
def client(db_session):
    from app.main import app

    def session_override():
        yield db_session

    app.dependency_overrides[get_db] = session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("alert_type", ["indicator_trigger", "watchlist_signal"])
def test_unavailable_alert_types_cannot_be_created_enabled(client, alert_type):
    response = client.post("/api/v1/alerts/rules", json={
        "name": "unavailable", "alert_type": alert_type, "enabled": True,
    })
    assert response.status_code == 400
    assert client.get("/api/v1/alerts/rules").json() == []
