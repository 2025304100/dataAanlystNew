"""T26/F1 HTTP 接口契约测试（3 接口：存回 / 抽取 / related）。

路由层按项目范式无 prefix；测试内建小 app 挂载单路由 +
dependency_overrides[get_db]（不碰真实 MySQL，照 candidate_pool 先例）。
"""
from __future__ import annotations

import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import factor_experience as route_mod
from app.db.session import get_db


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _payload(window: int = 20, field: str = "close", **overrides) -> dict:
    payload = {
        "formula_ast": {
            "type": "Expression",
            "body": {
                "type": "Call", "func": "mean",
                "args": [
                    {"type": "Name", "id": field},
                    {"type": "Constant", "value": window},
                ],
                "keywords": [],
            },
        },
        "source": "enumerated",
        "category": "trend",
    }
    payload.update(overrides)
    return payload


class TestPostStore:
    def test_store_created(self, client):
        resp = client.post("/factor-experience", json=_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert len(body["experience_id"]) == 64

    def test_store_duplicate_idempotent(self, client):
        first = client.post("/factor-experience", json=_payload()).json()
        # 同构不同参数（20 → 60）→ 同指纹 → duplicate
        second = client.post(
            "/factor-experience", json=_payload(window=60)).json()
        assert second["status"] == "duplicate"
        assert second["experience_id"] == first["experience_id"]

    def test_invalid_source_422(self, client):
        resp = client.post(
            "/factor-experience", json=_payload(source="hacker"))
        assert resp.status_code == 422

    def test_invalid_category_422(self, client):
        resp = client.post(
            "/factor-experience", json=_payload(category="alpha"))
        assert resp.status_code == 422

    def test_negative_sample_accepted(self, client):
        resp = client.post(
            "/factor-experience", json=_payload(is_negative_sample=1))
        assert resp.status_code == 200


class TestGetSample:
    def test_sample_returns_items(self, client):
        client.post("/factor-experience", json=_payload())
        resp = client.get(
            "/factor-experience/sample",
            params={"count": 1, "market_env": "bull"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert "{n" not in item["formula"]
        assert item["category"] == "trend"
        assert item["score"] >= 0

    def test_sample_field_scope_filter(self, client):
        client.post("/factor-experience", json=_payload())
        resp = client.get(
            "/factor-experience/sample",
            params={"field_scope": "volume"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_sample_empty_db(self, client):
        resp = client.get("/factor-experience/sample")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_sample_count_bounds(self, client):
        client.post("/factor-experience", json=_payload())
        assert client.get(
            "/factor-experience/sample", params={"count": 0}).status_code == 422
        assert client.get(
            "/factor-experience/sample", params={"count": 999}).status_code == 422


class TestGetRelated:
    def test_related_excludes_self(self, client):
        base = client.post("/factor-experience", json=_payload()).json()
        high = client.post(
            "/factor-experience",
            json=_payload(field="volume")).json()
        resp = client.get(
            "/factor-experience/related",
            params={"experience_id": base["experience_id"]})
        assert resp.status_code == 200
        ids = [i["experience_id"] for i in resp.json()["items"]]
        assert high["experience_id"] in ids
        assert base["experience_id"] not in ids

    def test_related_requires_experience_id(self, client):
        assert client.get(
            "/factor-experience/related").status_code == 422
