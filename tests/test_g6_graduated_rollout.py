from __future__ import annotations

from datetime import date

from app.models.portfolio import Portfolio
from app.services.g5_dual_run_audit import latest_g5_summary, persist_g5_summary
from app.services.g5_dual_run_replay import get_g5_acceptance_policy
from app.services.g6_graduated_rollout import evaluate_g6_readiness


import pytest

pytestmark = pytest.mark.whitebox
def _portfolio(db_session, name: str) -> Portfolio:
    row = Portfolio(
        name=name,
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100000,
        investable_ratio=1,
        cash_reserve_ratio=0,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _g5(db, portfolio_id: int, *, eligible: bool = True) -> dict:
    from tests.g5_report_fixtures import passing_g5
    return passing_g5(db, portfolio_id, eligible=eligible)


def test_g6_requires_a_persisted_passing_g5_report(db_session):
    portfolio = _portfolio(db_session, "g6-no-report")
    readiness = evaluate_g6_readiness(db_session, portfolio_id=portfolio.id)
    assert readiness.eligible is False
    assert any("G5" in value for value in readiness.blockers)


def test_g6_rejects_verified_decision_archive_without_full_admission_proof(db_session):
    portfolio = _portfolio(db_session, "g6-ready")
    portfolio.last_decision_trade_date = date(2026, 8, 14)
    portfolio.last_reconciled_trade_date = date(2026, 8, 14)
    persist_g5_summary(db_session, _g5(db_session, portfolio.id))
    db_session.commit()
    # Actual persisted capture rows verify the archive's decision evidence.
    # They do not implement or prove the unavailable full dual-engine runner.
    summary = latest_g5_summary(db_session, portfolio_id=portfolio.id)
    assert summary["evidence_verified"] is True
    assert summary["evidence_scope"] == "decision_actions_only"
    assert summary["verdict"] == "EVIDENCE_VERIFIED_NOT_ADMISSIBLE"
    assert summary["g5_eligible_for_g6"] is False
    assert get_g5_acceptance_policy()["admission_supported"] is False
    assert set(summary["acceptance_blocker_codes"]) >= {
        "DEPLOYMENT_RUNNER_PROOF", "OPENING_ACCOUNT_SNAPSHOT", "ISOLATED_ORDER_PLAN",
        "RISK_INPUT_SNAPSHOT", "FACTOR_DEPENDENCY_SNAPSHOT",
    }
    readiness = evaluate_g6_readiness(db_session, portfolio_id=portfolio.id)
    assert readiness.eligible is False
    assert readiness.g5_report_id is not None
    assert any("G6 准入条件" in value for value in readiness.blockers)


def test_g6_blocks_unreconciled_decision_day(db_session):
    portfolio = _portfolio(db_session, "g6-gap")
    portfolio.last_decision_trade_date = date(2026, 8, 14)
    portfolio.last_reconciled_trade_date = date(2026, 8, 13)
    persist_g5_summary(db_session, _g5(db_session, portfolio.id))
    db_session.commit()
    readiness = evaluate_g6_readiness(db_session, portfolio_id=portfolio.id)
    assert readiness.eligible is False
    assert any("对账" in value for value in readiness.blockers)
