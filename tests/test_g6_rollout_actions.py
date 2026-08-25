from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models.portfolio import (
    AUTO_TRADE_SOURCE_LEGACY_SCAN,
    AUTO_TRADE_SOURCE_MEMBERS_ONLY,
    Portfolio,
)
from app.models.audit import DataGovernanceAuditEvent
from app.services.g5_dual_run_audit import persist_g5_summary
from app.services.g6_graduated_rollout import rollback_g6_rollout, start_g6_rollout


def _portfolio(db, name: str) -> Portfolio:
    row = Portfolio(
        name=name, account_type="simulated", asset_scope="mixed",
        total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
    )
    db.add(row)
    db.flush()
    return row


def _passing_g5(pid: int) -> dict:
    dates = [f"2026-08-{d:02d}" for d in range(3, 13)]
    return {
        "portfolio_id": pid, "start_date": "2026-08-03", "end_date": "2026-08-14",
        "total_days": 10, "days_replayed": 10, "skipped_days": [],
        "total_p0_unexplained": 0, "total_p1_hold_noaction_flip": 0,
        "g5_eligible_for_g6": True,
        "daily_reports": [
            {"trade_date": d,
             "chain_a_meta": {"capture_mode": "legacy_dry_run", "source_run_id": f"old-{d}", "data_cutoff_at": f"{d}T15:00:00+08:00"},
             "chain_b_meta": {"capture_mode": "unified_dry_run", "source_run_id": f"new-{d}", "data_cutoff_at": f"{d}T15:00:00+08:00"}}
            for d in dates
        ],
        "provenance": {
            "capture_mode": "real_dry_run_export", "exported_at": "2026-08-22T10:00:00+08:00",
            "trading_calendar": "SSE", "source_manifest_sha256": "a" * 64,
            "expected_trade_dates": dates,
        },
    }


def _ready_with_g5(db, name="g6-actions"):
    p = _portfolio(db, name)
    p.last_decision_trade_date = date(2026, 8, 14)
    p.last_reconciled_trade_date = date(2026, 8, 14)
    persist_g5_summary(db, _passing_g5(p.id))
    db.commit()
    return p


def test_g6_start_is_idempotent_and_audited(db_session):
    p = _ready_with_g5(db_session)
    first = start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", correlation_id="c1")
    db_session.commit()
    second = start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", correlation_id="c2")
    assert first.status == "STARTED"
    assert second.status == "NOOP"
    assert p.auto_trade_source_mode == AUTO_TRADE_SOURCE_MEMBERS_ONLY
    events = db_session.execute(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.portfolio_id == p.id)).scalars().all()
    assert [e.action for e in events].count("G6_ROLLOUT_STARTED") == 1
    assert events[0].operator_id == "alice"


def test_g6_rollback_persists_old_mode_and_reason(db_session):
    p = _ready_with_g5(db_session, "g6-rollback")
    start_g6_rollout(db_session, portfolio_id=p.id)
    db_session.commit()
    result = rollback_g6_rollout(db_session, portfolio_id=p.id, operator_id="ops", reason="差异回滚")
    db_session.commit()
    assert result.status == "ROLLED_BACK"
    assert p.auto_trade_source_mode == AUTO_TRADE_SOURCE_LEGACY_SCAN
    event = db_session.execute(
        select(DataGovernanceAuditEvent)
        .where(DataGovernanceAuditEvent.action == "G6_ROLLOUT_ROLLED_BACK")
    ).scalar_one()
    assert event.operator_id == "ops"
    assert "差异回滚" in (event.attributes_json or "")


def test_g6_start_failure_rolls_back_mode_and_audit(db_session, monkeypatch):
    p = _ready_with_g5(db_session, "g6-failure")
    import app.services.g6_graduated_rollout as rollout

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(rollout, "write_audit_event", fail)
    with pytest.raises(RuntimeError):
        start_g6_rollout(db_session, portfolio_id=p.id)
    db_session.rollback()
    db_session.refresh(p)
    assert p.auto_trade_source_mode != AUTO_TRADE_SOURCE_MEMBERS_ONLY
    assert db_session.execute(
        select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.portfolio_id == p.id)
    ).scalars().all() == []
