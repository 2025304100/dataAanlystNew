"""Date presets reach the API without bypassing real provider range limits."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import external_data as routes
from app.services import external_data_sync_task as service


pytestmark = pytest.mark.whitebox


@pytest.fixture
def preview_client(monkeypatch):
    # Freeze only the clock; validate through the actual request schema and plan.
    monkeypatch.setattr(
        routes,
        "build_external_sync_plan",
        lambda dataset, payload: service.build_external_sync_plan(
            dataset, payload, today=date(2026, 10, 14)
        ),
    )
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("dataset", ["fundamental", "financial"])
@pytest.mark.parametrize(
    ("start", "span"),
    [
        ("2026-04-18", 180),
        ("2025-10-15", 365),
        ("2021-10-15", 1826),
        ("1990-01-01", 13436),
    ],
)
def test_preview_accepts_long_real_history_ranges(preview_client, dataset, start, span):
    response = preview_client.post(
        "/external-data/sync-plans/preview",
        json={
            "dataset": dataset,
            "mode": "backfill",
            "start_date": start,
            "end_date": "2026-10-14",
            "lookback_days": span,
        },
    )

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["requested_start_date"] == start
    assert plan["requested_end_date"] == "2026-10-14"
    assert plan["requested_span_days"] == span


@pytest.mark.parametrize(
    ("dataset", "span", "start"),
    [("lhb", 31, "2026-09-14"), ("capital_flow", 100, "2026-07-07")],
)
def test_preview_preserves_exact_provider_limit(preview_client, dataset, span, start):
    accepted = preview_client.post(
        "/external-data/sync-plans/preview",
        json={"dataset": dataset, "mode": "backfill", "lookback_days": span},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["requested_start_date"] == start
    assert accepted.json()["provider_history_limit_days"] == span

    rejected = preview_client.post(
        "/external-data/sync-plans/preview",
        json={"dataset": dataset, "mode": "backfill", "lookback_days": span + 1},
    )
    assert rejected.status_code == 400
    assert f"provider limit {span}" in rejected.json()["detail"]


@pytest.mark.parametrize("dataset", service.EXTERNAL_DATASETS)
def test_preview_rejects_future_end_date(preview_client, dataset):
    response = preview_client.post(
        "/external-data/sync-plans/preview",
        json={"dataset": dataset, "end_date": "2026-10-15", "lookback_days": 1},
    )

    assert response.status_code == 400
    assert "未来" in response.json()["detail"]


def test_preview_payload_has_bounded_date_span(preview_client):
    accepted = preview_client.post(
        "/external-data/sync-plans/preview",
        json={"dataset": "financial", "mode": "backfill", "lookback_days": 36500},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["requested_span_days"] == 36500

    for span in (0, 36501):
        rejected = preview_client.post(
            "/external-data/sync-plans/preview",
            json={"dataset": "financial", "mode": "backfill", "lookback_days": span},
        )
        assert rejected.status_code == 422
