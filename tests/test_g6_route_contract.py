from __future__ import annotations

from app.api.routes.auto_trade import (
    G6RolloutRequest,
    rollback_g6_rollout_route,
    start_g6_rollout_route,
)
from app.models.portfolio import Portfolio
from app.services.g5_dual_run_audit import persist_g5_summary

from tests.test_g6_rollout_actions import _passing_g5


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
    persist_g5_summary(db, _passing_g5(row.id))
    db.commit()
    return row


def test_g6_start_route_returns_audited_result_and_is_idempotent(db_session):
    row = _ready(db_session, "g6-route-start")
    payload = G6RolloutRequest(operator_id="route-operator", correlation_id="route-c1")

    first = start_g6_rollout_route(row.id, payload, db_session)
    second = start_g6_rollout_route(row.id, payload, db_session)

    assert first["status"] == "STARTED"
    assert first["audit_event_id"] is not None
    assert second["status"] == "NOOP"
    assert second["audit_event_id"] is None


def test_g6_start_route_blocks_unready_portfolio(db_session):
    row = _portfolio(db_session, "g6-route-blocked")

    try:
        start_g6_rollout_route(row.id, G6RolloutRequest(operator_id="operator"), db_session)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert "G6 rollout blocked" in str(getattr(exc, "detail", exc))
    else:
        raise AssertionError("unready G6 rollout must return HTTP 409")


def test_g6_rollback_route_returns_old_mode_and_reason(db_session):
    row = _ready(db_session, "g6-route-rollback")
    start_g6_rollout_route(row.id, G6RolloutRequest(operator_id="operator"), db_session)

    result = rollback_g6_rollout_route(
        row.id,
        G6RolloutRequest(operator_id="operator", reason="route drill"),
        db_session,
    )

    assert result["status"] == "ROLLED_BACK"
    assert result["to_source_mode"] == "legacy_scan"
    assert result["reason"] == "route drill"
