from __future__ import annotations

from datetime import date
import json

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


pytestmark = pytest.mark.whitebox
def _portfolio(db, name: str) -> Portfolio:
    row = Portfolio(
        name=name, account_type="simulated", asset_scope="mixed",
        total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
    )
    db.add(row)
    db.flush()
    return row


def _passing_g5(db, portfolio_id: int) -> dict:
    from tests.g5_report_fixtures import passing_g5
    return passing_g5(db, portfolio_id)


def _ready_with_g5(db, monkeypatch, name="g6-actions"):
    """Unit-test a FUTURE complete adapter boundary, never a runner acceptance.

    Current real G5 archives are decision_actions_only and cannot admit G6.
    This controlled return isolates atomic switch/audit behavior from that
    intentionally unavailable production capability; no fake report is saved.
    """
    p = _portfolio(db, name)
    p.last_decision_trade_date = date(2026, 8, 14)
    p.last_reconciled_trade_date = date(2026, 8, 14)
    from app.services.g5_admission_governance import get_g5_admission_state, set_g5_admission_freeze
    from app.services.g5_legacy_inventory import build_g5_legacy_inventory
    from app.models.g5_admission_governance import G5AdmissionControl, G5AdmissionEvent
    for table in (G5AdmissionControl.__table__, G5AdmissionEvent.__table__):
        table.create(db.connection(), checkfirst=True)
    inventory = build_g5_legacy_inventory(db)
    set_g5_admission_freeze(db, frozen=False, expected_version=get_g5_admission_state(db)["version"],
        inventory_snapshot_id=inventory["snapshot_id"], operator_id="unit-reviewer", request_key=f"unit-g6-review-{p.id}",
        review_note="隔离单测审核空库存，只用于测试未来完整校验边界", acknowledge_only_new_admission=True)
    import app.services.g6_graduated_rollout as rollout
    monkeypatch.setattr(rollout, "latest_g5_summary", lambda *args, **kwargs: {
        "audit_report_id": 777, "audit_report_hash": "e" * 64, "evidence_verified": True,
        "g5_eligible_for_g6": True, "applicable_scope": {"from_source_mode": "portfolio"},
        "evidence_scope": "future_complete_adapter_unit_contract"})
    db.commit()
    return p


def _review_fields(db, p):
    from app.services.g6_graduated_rollout import evaluate_g6_readiness
    readiness = evaluate_g6_readiness(db, portfolio_id=p.id)
    return dict(expected_g5_report_id=readiness.g5_report_id, expected_g5_report_hash=readiness.g5_report_hash,
        expected_source_mode=readiness.current_source_mode, review_note="本人已核对冻结报告和来源切换影响，同意本次切换")


def test_g6_start_is_idempotent_and_audited(db_session, monkeypatch):
    p = _ready_with_g5(db_session, monkeypatch)
    first = start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", correlation_id="c1", **_review_fields(db_session, p))
    db_session.commit()
    second = start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", correlation_id="c2", **_review_fields(db_session, p))
    assert first.status == "STARTED"
    assert second.status == "NOOP"
    assert p.auto_trade_source_mode == AUTO_TRADE_SOURCE_MEMBERS_ONLY
    events = db_session.execute(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.portfolio_id == p.id)).scalars().all()
    assert [e.action for e in events].count("G6_ROLLOUT_STARTED") == 1
    event = next(e for e in events if e.action == "G6_ROLLOUT_STARTED")
    assert event.operator_id == "alice" and "本人已核对" in event.note
    assert json.loads(event.attributes_json)["g5_report_hash"] == "e" * 64


def test_g6_rollback_persists_old_mode_and_reason(db_session, monkeypatch):
    p = _ready_with_g5(db_session, monkeypatch, "g6-rollback")
    start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", **_review_fields(db_session, p))
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
    p = _ready_with_g5(db_session, monkeypatch, "g6-failure")
    import app.services.g6_graduated_rollout as rollout

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(rollout, "write_audit_event", fail)
    with pytest.raises(RuntimeError):
        start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", **_review_fields(db_session, p))
    db_session.rollback()
    db_session.refresh(p)
    assert p.auto_trade_source_mode != AUTO_TRADE_SOURCE_MEMBERS_ONLY
    assert db_session.execute(
        select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.portfolio_id == p.id,
            DataGovernanceAuditEvent.action == "G6_ROLLOUT_STARTED")
    ).scalars().all() == []


@pytest.mark.parametrize("field,value", [("expected_g5_report_id", 778), ("expected_g5_report_hash", "f" * 64),
    ("expected_source_mode", "legacy_scan")])
def test_g6_final_switch_refuses_changed_review_snapshot(db_session, monkeypatch, field, value):
    p = _ready_with_g5(db_session, monkeypatch, "g6-stale-review")
    payload = _review_fields(db_session, p) | {field: value}
    with pytest.raises(ValueError, match="G5_ADMISSION_SNAPSHOT_CHANGED"):
        start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", **payload)
    assert p.auto_trade_source_mode == "portfolio"
    assert db_session.scalar(select(DataGovernanceAuditEvent).where(DataGovernanceAuditEvent.action == "G6_ROLLOUT_STARTED")) is None


@pytest.mark.parametrize("actor,note", [("system", "本人审核冻结报告与来源切换的影响"), ("alice", " ")])
def test_g6_report_pass_cannot_replace_personal_switch_review(db_session, monkeypatch, actor, note):
    p = _ready_with_g5(db_session, monkeypatch)
    with pytest.raises(ValueError, match="G5_PERSONAL_SWITCH_REVIEW_REQUIRED"):
        start_g6_rollout(db_session, portfolio_id=p.id, operator_id=actor, **(_review_fields(db_session, p) | {"review_note": note}))
    assert p.auto_trade_source_mode == "portfolio"


def test_current_decision_action_archive_never_admits_real_g6(db_session):
    from app.services.g6_graduated_rollout import evaluate_g6_readiness
    p = _portfolio(db_session, "real-archive-not-complete")
    persist_g5_summary(db_session, _passing_g5(db_session, p.id)); db_session.commit()
    readiness = evaluate_g6_readiness(db_session, portfolio_id=p.id)
    assert not readiness.eligible
    assert any("未达到" in reason for reason in readiness.blockers)
    with pytest.raises(ValueError, match="G6 rollout blocked"):
        start_g6_rollout(db_session, portfolio_id=p.id, operator_id="alice", **_review_fields(db_session, p))
    assert p.auto_trade_source_mode == "portfolio"
