"""Regression checks for the governance UI/API contract bridge."""

from app.api.routes.portfolio_governance import (
    ConfirmReconciliationRequest,
    ReconcileRequest,
    TransitionStateRequest,
)


def test_ui_payload_aliases_are_accepted():
    assert ReconcileRequest(as_of_trade_date="2026-08-21").as_of_trade_date.isoformat() == "2026-08-21"
    transition = TransitionStateRequest(target_state="READY", trigger_reason="initial review")
    assert transition.target_state == "READY"
    assert transition.trigger_reason == "initial review"
    confirm = ConfirmReconciliationRequest(ack=True, review_note="verified all differences")
    assert confirm.ack is True
    assert confirm.review_note == "verified all differences"

