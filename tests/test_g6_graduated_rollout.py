from __future__ import annotations

from datetime import date

from app.models.portfolio import Portfolio
from app.services.g5_dual_run_audit import persist_g5_summary
from app.services.g6_graduated_rollout import evaluate_g6_readiness


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


def _g5(portfolio_id: int, *, eligible: bool = True) -> dict:
    dates = [f"2026-08-{day:02d}" for day in range(3, 13)]
    daily_reports = [
        {
            "trade_date": trade_date,
            "chain_a_meta": {
                "capture_mode": "legacy_dry_run",
                "source_run_id": f"scan-{trade_date}",
                "data_cutoff_at": f"{trade_date}T15:00:00+08:00",
            },
            "chain_b_meta": {
                "capture_mode": "unified_dry_run",
                "source_run_id": f"decision-{trade_date}",
                "data_cutoff_at": f"{trade_date}T15:00:00+08:00",
            },
        }
        for trade_date in dates
    ]
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
        "daily_reports": daily_reports,
        "provenance": {
            "capture_mode": "real_dry_run_export",
            "exported_at": "2026-08-22T10:00:00+08:00",
            "trading_calendar": "SSE",
            "source_manifest_sha256": "a" * 64,
            "expected_trade_dates": dates,
        },
    }


def test_g6_requires_a_persisted_passing_g5_report(db_session):
    portfolio = _portfolio(db_session, "g6-no-report")
    readiness = evaluate_g6_readiness(db_session, portfolio_id=portfolio.id)
    assert readiness.eligible is False
    assert any("G5" in value for value in readiness.blockers)


def test_g6_accepts_ready_reconciled_portfolio_with_passing_g5(db_session):
    portfolio = _portfolio(db_session, "g6-ready")
    portfolio.last_decision_trade_date = date(2026, 8, 14)
    portfolio.last_reconciled_trade_date = date(2026, 8, 14)
    persist_g5_summary(db_session, _g5(portfolio.id))
    db_session.commit()
    readiness = evaluate_g6_readiness(db_session, portfolio_id=portfolio.id)
    assert readiness.eligible is True
    assert readiness.g5_report_id is not None


def test_g6_blocks_unreconciled_decision_day(db_session):
    portfolio = _portfolio(db_session, "g6-gap")
    portfolio.last_decision_trade_date = date(2026, 8, 14)
    portfolio.last_reconciled_trade_date = date(2026, 8, 13)
    persist_g5_summary(db_session, _g5(portfolio.id))
    db_session.commit()
    readiness = evaluate_g6_readiness(db_session, portfolio_id=portfolio.id)
    assert readiness.eligible is False
    assert any("对账" in value for value in readiness.blockers)
