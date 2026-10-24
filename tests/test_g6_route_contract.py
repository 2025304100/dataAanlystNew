from __future__ import annotations

from app.api.routes.auto_trade import (
    G6RolloutRequest,
    rollback_g6_rollout_route,
    start_g6_rollout_route,
)
from app.models.portfolio import Portfolio
from app.services.g5_dual_run_audit import persist_g5_summary

from tests.test_g6_rollout_actions import _passing_g5, _ready_with_g5, _review_fields


import pytest

pytestmark = pytest.mark.whitebox
def _portfolio(db, name: str, *, account_type: str = "simulated") -> Portfolio:
    row = Portfolio(
        name=name,
        account_type=account_type,
        asset_scope="mixed",
        total_capital=100000,
        investable_ratio=1,
        cash_reserve_ratio=0,
    )
    db.add(row)
    db.flush()
    return row


def _ready(db, name: str) -> Portfolio:
    from datetime import date

    row = _portfolio(db, name)
    row.last_decision_trade_date = date(2026, 8, 14)
    row.last_reconciled_trade_date = date(2026, 8, 14)
    persist_g5_summary(db, _passing_g5(db, row.id))
    db.commit()
    return row


def test_g6_start_route_returns_audited_result_and_is_idempotent(db_session, monkeypatch):
    row = _ready_with_g5(db_session, monkeypatch, "g6-route-start")
    payload = G6RolloutRequest(operator_id="ignored-untrusted-payload-actor", correlation_id="route-c1", **_review_fields(db_session, row))

    first = start_g6_rollout_route(row.id, payload, db_session, actor="route-operator")
    second_payload = G6RolloutRequest(correlation_id="route-c2", **_review_fields(db_session, row))
    second = start_g6_rollout_route(row.id, second_payload, db_session, actor="route-operator")

    assert first["status"] == "STARTED"
    assert first["audit_event_id"] is not None
    assert first["operator_id"] == "route-operator"
    assert second["status"] == "NOOP"
    assert second["audit_event_id"] is None


def test_g6_start_route_blocks_unready_portfolio(db_session):
    row = _portfolio(db_session, "g6-route-blocked")

    try:
        start_g6_rollout_route(row.id, G6RolloutRequest(operator_id="operator"), db_session, actor="operator")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert "G5_NEW_ADMISSION_FROZEN" in str(getattr(exc, "detail", exc))
    else:
        raise AssertionError("unready G6 rollout must return HTTP 409")


def test_g6_rollback_route_returns_old_mode_and_reason(db_session, monkeypatch):
    row = _ready_with_g5(db_session, monkeypatch, "g6-route-rollback")
    start_g6_rollout_route(row.id, G6RolloutRequest(**_review_fields(db_session, row)), db_session, actor="operator")

    result = rollback_g6_rollout_route(
        row.id,
        G6RolloutRequest(operator_id="forged-admin", reason="route drill"),
        db_session,
        actor="actual-reviewer",
    )

    assert result["status"] == "ROLLED_BACK"
    assert result["to_source_mode"] == "legacy_scan"
    assert result["reason"] == "route drill"
    assert result["operator_id"] == "actual-reviewer"
