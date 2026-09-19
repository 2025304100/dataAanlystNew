from __future__ import annotations

from datetime import date

from app.services.g5_dual_run_audit import latest_g5_summary, persist_g5_summary
from app.models.portfolio import Portfolio


def _summary(portfolio_id: int, *, eligible: bool = True) -> dict:
    return {
        "portfolio_id": portfolio_id,
        "start_date": "2026-08-03",
        "end_date": "2026-08-14",
        "total_days": 10,
        "days_replayed": 10,
        "skipped_days": [],
        "total_p0_unexplained": 0,
        "total_p1_hold_noaction_flip": 0,
        "g5_eligible_for_g6": eligible,
        "daily_reports": [],
    }


def test_g5_summary_is_hash_deduplicated_and_readable(db_session):
    portfolio = Portfolio(
        name="g5-audit-portfolio",
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100000,
        investable_ratio=1,
        cash_reserve_ratio=0,
    )
    db_session.add(portfolio)
    db_session.flush()
    first = persist_g5_summary(db_session, _summary(portfolio.id))
    second = persist_g5_summary(db_session, _summary(portfolio.id))
    db_session.commit()
    assert first.id == second.id
    payload = latest_g5_summary(db_session, portfolio_id=portfolio.id)
    assert payload is not None
    assert payload["audit_report_id"] == first.id
    assert payload["audit_report_hash"] == first.report_hash
    assert payload["g5_eligible_for_g6"] is False
    assert "g5_validation_errors" in payload
