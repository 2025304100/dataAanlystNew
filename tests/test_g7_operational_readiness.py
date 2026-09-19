"""G7 operational readiness contract for expansion, pause, and recovery."""
from __future__ import annotations

from datetime import date, datetime

from app.models.alert import AlertEvent
from app.models.portfolio import Portfolio
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.g5_dual_run_audit import persist_g5_summary
from app.services.portfolio_state_machine import _set_status


def _portfolio(db_session, name: str) -> Portfolio:
    row = Portfolio(
        name=name,
        account_type="simulated",
        asset_scope="mixed",
        total_capital=100000,
        investable_ratio=1,
        cash_reserve_ratio=0,
        last_decision_trade_date=date(2026, 8, 14),
        last_reconciled_trade_date=date(2026, 8, 14),
    )
    db_session.add(row)
    db_session.flush()
    _set_status(db_session, row, "READY")
    db_session.flush()
    return row


def _passing_g5(portfolio_id: int) -> dict:
    dates = [f"2026-08-{day:02d}" for day in range(3, 13)]
    return {
        "portfolio_id": portfolio_id,
        "start_date": "2026-08-03",
        "end_date": "2026-08-14",
        "total_days": 10,
        "days_replayed": 10,
        "skipped_days": [],
        "total_p0_unexplained": 0,
        "total_p1_hold_noaction_flip": 0,
        "g5_eligible_for_g6": True,
        "daily_reports": [
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
        ],
        "provenance": {
            "capture_mode": "real_dry_run_export",
            "exported_at": "2026-08-22T10:00:00+08:00",
            "trading_calendar": "SSE",
            "source_manifest_sha256": "b" * 64,
            "expected_trade_dates": dates,
        },
    }


def _order(portfolio_id: int, *, status: str, review_status: str | None = None) -> SimOrder:
    return SimOrder(
        portfolio_id=portfolio_id,
        symbol_id=1,
        side="buy",
        quantity=100,
        submitted_price=10,
        status=status,
        review_status=review_status,
    )


def _active_alert(portfolio_id: int) -> AlertEvent:
    return AlertEvent(
        rule_id=1,
        alert_type="task_failed",
        severity="error",
        title="G7 alert",
        message="requires operator review",
        acknowledged=0,
        portfolio_id=portfolio_id,
        dedupe_key=f"g7-{portfolio_id}",
        incident_no=1,
        status="ACTIVE",
        severity_level="L1",
        window_start_at=datetime(2026, 8, 14),
    )


def test_g7_blocks_expansion_without_persisted_g5_report(db_session):
    from app.services.g7_operational_readiness import evaluate_g7_operational_status

    portfolio = _portfolio(db_session, "g7-no-g5")

    status = evaluate_g7_operational_status(db_session, portfolio_id=portfolio.id)

    assert status.g6_eligible is False
    assert status.ready_for_expansion is False
    assert any("G5" in blocker for blocker in status.operational_blockers)


def test_g7_reports_admin_pause_as_an_effective_new_buy_stop(db_session):
    from app.services.g7_operational_readiness import evaluate_g7_operational_status

    portfolio = _portfolio(db_session, "g7-paused")
    persist_g5_summary(db_session, _passing_g5(portfolio.id))
    _set_status(db_session, portfolio, "ADMIN_PAUSED")
    db_session.commit()

    status = evaluate_g7_operational_status(db_session, portfolio_id=portfolio.id)

    assert status.current_state == "ADMIN_PAUSED"
    assert status.new_buys_stopped is True
    assert status.can_resume is True


def test_g7_resume_is_independent_from_g5_expansion_gate(db_session):
    from app.services.g7_operational_readiness import evaluate_g7_operational_status

    portfolio = _portfolio(db_session, "g7-paused-no-g5")
    _set_status(db_session, portfolio, "ADMIN_PAUSED")
    db_session.commit()

    status = evaluate_g7_operational_status(db_session, portfolio_id=portfolio.id)

    assert status.g6_eligible is False
    assert status.ready_for_expansion is False
    assert status.can_resume is True


def test_g7_uses_composite_buy_gate_for_stale_score(db_session):
    from app.services.g7_operational_readiness import evaluate_g7_operational_status

    portfolio = _portfolio(db_session, "g7-score-stale")
    portfolio.status_score = "SCORE_STALE"
    db_session.commit()

    status = evaluate_g7_operational_status(db_session, portfolio_id=portfolio.id)

    assert status.current_state == "READY"
    assert status.new_buys_stopped is True


def test_g7_counts_partial_and_pending_confirmation_orders(db_session):
    from app.services.g7_operational_readiness import evaluate_g7_operational_status

    portfolio = _portfolio(db_session, "g7-pending-orders")
    db_session.add(Symbol(symbol="G7-SYMBOL", name="G7 Symbol", market="SH", asset_type="stock"))
    db_session.flush()
    symbol_id = db_session.query(Symbol).filter_by(symbol="G7-SYMBOL").one().id
    db_session.add_all([
        SimOrder(portfolio_id=portfolio.id, symbol_id=symbol_id, side="buy", quantity=100, submitted_price=10, status="partial"),
        SimOrder(portfolio_id=portfolio.id, symbol_id=symbol_id, side="buy", quantity=100, submitted_price=10, status="pending_confirmation"),
        SimOrder(portfolio_id=portfolio.id, symbol_id=symbol_id, side="buy", quantity=100, submitted_price=10, status="filled"),
    ])
    db_session.commit()

    status = evaluate_g7_operational_status(db_session, portfolio_id=portfolio.id)

    assert status.pending_order_count == 2
    assert status.pending_orders_by_status == {
        "partial": 1,
        "pending_confirmation": 1,
    }


def test_g7_surfaces_unacknowledged_portfolio_alerts_and_blocks_resume(db_session):
    from app.services.g7_operational_readiness import evaluate_g7_operational_status

    portfolio = _portfolio(db_session, "g7-active-alert")
    persist_g5_summary(db_session, _passing_g5(portfolio.id))
    _set_status(db_session, portfolio, "ADMIN_PAUSED")
    db_session.add(_active_alert(portfolio.id))
    db_session.commit()

    status = evaluate_g7_operational_status(db_session, portfolio_id=portfolio.id)

    assert status.active_alert_count == 1
    assert status.can_resume is False
    assert any("未确认告警" in blocker for blocker in status.resume_blockers)


def test_g7_route_returns_the_public_operational_contract(db_session):
    from app.api.routes.auto_trade import get_g7_operational_status

    portfolio = _portfolio(db_session, "g7-route")

    payload = get_g7_operational_status(portfolio_id=portfolio.id, db=db_session)

    assert payload["portfolio_id"] == portfolio.id
    assert {
        "current_state",
        "g6_eligible",
        "new_buys_stopped",
        "pending_order_count",
        "active_alert_count",
        "ready_for_expansion",
        "can_resume",
        "operational_blockers",
        "resume_blockers",
    } <= payload.keys()
