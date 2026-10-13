"""User confirmations validate current evidence and atomically retain review history."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect, select, text

from app.api.routes import portfolio_governance as api
from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
from app.models.portfolio import Portfolio
from app.services.data_governance_audit import DataGovernanceAuditEvent
from app.services.portfolio_binding_review import build_binding_review
from app.services.portfolio_reconciliation import ReconciliationDiff, ReconciliationReport
from tests.test_stage3_factor_binding_contract import _activate, _portfolio, _ready_model

pytestmark = pytest.mark.whitebox


def _legacy_state(db, portfolio, state):
    # The legacy state column belongs to the isolated fixture, never the app's
    # read paths or production schema migration.
    if not any(c["name"] == "portfolio_status" for c in inspect(db.connection()).get_columns("portfolios")):
        db.execute(text("ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"))
    db.execute(text("UPDATE portfolios SET portfolio_status=:state WHERE id=:id"), {"state": state, "id": portfolio.id})


def _snapshot(db, portfolio, *, model_id, set_id, number=1, snapshot_type="save_and_apply"):
    now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    usage = PortfolioFactorUsage(
        id=f"review-usage-{number}", portfolio_id=portfolio.id,
        factor_model_run_id=model_id, factor_set_id=set_id,
        run_mode="production_sim", pit_mode="strict_pit_safe", status="active",
        content_hash=f"usage-hash-{number}", effective_from=now,
    )
    snapshot = StrategyExecutionSnapshot(
        id=f"review-snapshot-{number}", snapshot_no=number, portfolio_id=portfolio.id,
        portfolio_factor_usage_id=usage.id,
        factor_model_run_id=model_id, factor_set_id=set_id,
        decision_clock_json="{}", member_snapshot_json="[]",
        snapshot_type=snapshot_type, snapshot_hash=f"hash-{number}",
        effective_from=now + timedelta(seconds=number),
    )
    db.add_all([usage, snapshot])
    db.flush()
    return usage, snapshot


@pytest.fixture
def binding(db_session):
    db = db_session
    portfolio = _portfolio(db)
    portfolio.status_model = "MODEL_INACTIVE"
    portfolio.last_decision_trade_date = date(2026, 9, 1)
    model = _ready_model(db, set_id="fs-review", model_id="model-review")
    _activate(db, model.id)
    usage, snapshot = _snapshot(db, portfolio, model_id=model.id, set_id="fs-review")
    db.commit()
    return db, portfolio, model, usage, snapshot


def _confirm(db, portfolio, snapshot, **fields):
    return api.confirm_portfolio_model_binding(
        portfolio.id,
        api.ConfirmModelBindingRequest(
            snapshot_id=snapshot.id, model_run_id=snapshot.factor_model_run_id,
            ack=True, review_note="  已查看新模型和策略摘要，确认接受本次变更  ", **fields,
        ), db, "owner",
    )


def test_binding_review_is_read_only_and_first_acceptance_has_no_invented_before(binding, monkeypatch):
    db, portfolio, _, _, snapshot = binding
    monkeypatch.setattr(db, "flush", lambda *a, **k: pytest.fail("read review flushed DB"))
    monkeypatch.setattr(db, "commit", lambda *a, **k: pytest.fail("read review committed DB"))
    result = build_binding_review(db, portfolio)
    assert result["can_confirm"] is True
    assert result["first_confirmation"] is True
    assert result["before"] is None
    assert result["after"]["snapshot_id"] == snapshot.id
    assert result["model_name"] and "ridge" in result["model_name"]
    assert "首次确认" in result["change_summary"]


@pytest.mark.parametrize("invalid", ["preflight", "retired", "expired_usage", "future_usage", "future_snapshot", "missing_usage", "mismatched_usage", "research_mode", "inactive_global_model"])
def test_invalid_binding_cannot_be_confirmed(binding, invalid):
    db, portfolio, model, usage, snapshot = binding
    if invalid == "preflight":
        snapshot.snapshot_type = "preflight"
    elif invalid == "retired":
        model.status = "retired"
    elif invalid == "expired_usage":
        usage.effective_to = datetime.now() - timedelta(days=1)
    elif invalid == "future_usage":
        usage.effective_from = datetime.now() + timedelta(days=1)
    elif invalid == "future_snapshot":
        snapshot.effective_from = datetime.now() + timedelta(days=1)
    elif invalid == "missing_usage":
        snapshot.portfolio_factor_usage_id = None
    elif invalid == "mismatched_usage":
        usage.factor_set_id = "other-set"
    elif invalid == "research_mode":
        usage.run_mode = "research"
    elif invalid == "inactive_global_model":
        from app.models.factor_runtime import FactorRuntimeState
        other = _ready_model(db, set_id="fs-review-other", model_id="model-review-other")
        db.get(FactorRuntimeState, 1).active_model_run_id = other.id
    db.commit()
    review = build_binding_review(db, portfolio)
    assert review["can_confirm"] is False
    assert review["blockers"]
    with pytest.raises(HTTPException) as error:
        _confirm(db, portfolio, snapshot)
    assert error.value.status_code == 409
    db.refresh(portfolio)
    assert portfolio.status_model == "MODEL_INACTIVE"
    assert db.scalar(select(DataGovernanceAuditEvent.id)) is None


def test_confirmation_rejects_snapshot_changed_since_review(binding):
    db, portfolio, _, usage, snapshot = binding
    _snapshot(db, portfolio, model_id=snapshot.factor_model_run_id, set_id=snapshot.factor_set_id, number=2)
    usage.status = "deprecated"
    db.commit()
    with pytest.raises(HTTPException) as error:
        _confirm(db, portfolio, snapshot)
    assert error.value.status_code == 409
    assert error.value.detail["error_code"] == "BINDING_REVIEW_CHANGED"
    assert portfolio.status_model == "MODEL_INACTIVE"


@pytest.mark.parametrize("legacy_state", ["READY", "ADMIN_PAUSED", "MODEL_INACTIVE"])
def test_model_confirmation_preserves_other_locks_and_records_note(binding, legacy_state):
    db, portfolio, _, _, snapshot = binding
    _legacy_state(db, portfolio, legacy_state)
    portfolio.status_score = "SCORE_STALE"
    portfolio.status_data = "DATA_INCOMPLETE_PAUSED"
    portfolio.status_reconciliation = "RECONCILIATION_BLOCKED"
    db.commit()
    response = _confirm(db, portfolio, snapshot)
    db.refresh(portfolio)
    assert portfolio.status_model == "READY"
    assert (portfolio.status_score, portfolio.status_data, portfolio.status_reconciliation) == (
        "SCORE_STALE", "DATA_INCOMPLETE_PAUSED", "RECONCILIATION_BLOCKED",
    )
    assert response["current_state"] == ("ADMIN_PAUSED" if legacy_state == "ADMIN_PAUSED" else "RECONCILIATION_BLOCKED")
    event = db.get(DataGovernanceAuditEvent, response["audit_event_id"])
    assert event.operator_id == "owner"
    assert event.note == "已查看新模型和策略摘要，确认接受本次变更"
    assert json.loads(event.before_json)["accepted_binding"] is None
    assert json.loads(event.after_json)["accepted_binding"]["snapshot_id"] == snapshot.id
    assert response["binding_review"]["requires_confirmation"] is False


def test_review_uses_last_explicit_acceptance_as_before(binding):
    db, portfolio, _, usage, first = binding
    _confirm(db, portfolio, first)
    usage.status = "deprecated"
    _, second = _snapshot(db, portfolio, model_id=first.factor_model_run_id, set_id=first.factor_set_id, number=2)
    portfolio.status_model = "MODEL_INACTIVE"
    db.commit()
    review = build_binding_review(db, portfolio)
    assert review["before"]["snapshot_id"] == first.id
    assert review["after"]["snapshot_id"] == second.id
    assert review["first_confirmation"] is False


def test_model_confirmation_audit_failure_rolls_back_unlock(binding, monkeypatch):
    db, portfolio, _, _, snapshot = binding
    _legacy_state(db, portfolio, "MODEL_INACTIVE")
    db.commit()
    def fail(*a, **k):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(api, "write_audit_event", fail)
    with pytest.raises(RuntimeError):
        _confirm(db, portfolio, snapshot)
    db.refresh(portfolio)
    assert portfolio.status_model == "MODEL_INACTIVE"
    assert api._psm_get_status(db, portfolio) == "MODEL_INACTIVE"


def test_apply_locks_only_model_dimension_and_same_snapshot_replay_keeps_acceptance(binding, monkeypatch):
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services import factor_usage_service as service
    db, portfolio, _, _, _ = binding
    portfolio.status_model = "READY"
    portfolio.status_data = "DATA_INCOMPLETE_PAUSED"
    portfolio.status_score = "SCORE_STALE"
    portfolio.status_reconciliation = "RECONCILIATION_BLOCKED"
    _legacy_state(db, portfolio, "ADMIN_PAUSED")
    db.commit()
    fixed_now = datetime(2026, 9, 2, 7, 0)
    monkeypatch.setattr(service, "_utcnow_naive", lambda: fixed_now)
    payload = FactorUsageBindRequest(
        factor_model_run_id="model-review", factor_set_id="fs-review",
        run_mode="production_sim", pit_mode="strict_pit_safe",
    )
    applied = service.save_and_apply_usage(db, portfolio.id, payload, "owner")
    db.refresh(portfolio)
    assert portfolio.status_model == "MODEL_INACTIVE"
    assert portfolio.status_data == "DATA_INCOMPLETE_PAUSED"
    assert portfolio.status_score == "SCORE_STALE"
    assert portfolio.status_reconciliation == "RECONCILIATION_BLOCKED"
    assert api._psm_get_status(db, portfolio) == "ADMIN_PAUSED"
    snapshot = db.get(StrategyExecutionSnapshot, applied.strategy_snapshot_id)
    _confirm(db, portfolio, snapshot)
    replay = service.save_and_apply_usage(db, portfolio.id, payload, "owner")
    db.refresh(portfolio)
    assert replay.strategy_snapshot_id == applied.strategy_snapshot_id
    assert portfolio.status_model == "READY"
    assert api._psm_get_status(db, portfolio) == "ADMIN_PAUSED"


@pytest.mark.parametrize("field,state", [
    ("status_model", "MODEL_INACTIVE"),
    ("status_reconciliation", "RECONCILIATION_BLOCKED"),
    ("status_data", "DATA_INCOMPLETE_PAUSED"),
])
def test_cron_preflight_cannot_bypass_dimension_locks(binding, field, state):
    db, portfolio, _, _, _ = binding
    portfolio.status_model = "READY"
    setattr(portfolio, field, state)
    _legacy_state(db, portfolio, "READY")
    db.commit()
    response = api.preflight_auto_simulation(
        portfolio.id,
        api.AutoSimulationPreflightRequest(trade_date=date(2026, 9, 2), decision_at=datetime(2026, 9, 2, 21, 0)),
        db, "cron_worker",
    )
    assert response.proceed is False
    assert response.skip_reason == "PORTFOLIO_NOT_READY"
    assert api._psm_get_status(db, portfolio) == "READY"
    assert getattr(portfolio, field) == state


@pytest.fixture
def reconciliation(db_session):
    db = db_session
    portfolio = _portfolio(db)
    portfolio.status_reconciliation = "RECONCILIATION_BLOCKED"
    portfolio.last_decision_trade_date = date(2026, 9, 1)
    _legacy_state(db, portfolio, "RECONCILIATION_BLOCKED")
    db.commit()
    return db, portfolio


@pytest.mark.parametrize("ack_field", ["ack", "acknowledge_all_diffs_cleared"])
@pytest.mark.parametrize("note", [None, "", "   ", "  少于十个字  "])
def test_reconciliation_requires_trimmed_note_for_both_contracts(reconciliation, ack_field, note):
    db, portfolio = reconciliation
    with pytest.raises(HTTPException) as error:
        api.confirm_portfolio_reconciliation(
            portfolio.id, api.ConfirmReconciliationRequest(**{ack_field: True}, review_note=note), db, "owner",
        )
    assert error.value.status_code == 422
    assert api._psm_get_status(db, portfolio) == "RECONCILIATION_BLOCKED"
    assert portfolio.last_reconciled_trade_date is None
    assert db.scalar(select(DataGovernanceAuditEvent.id)) is None


@pytest.mark.parametrize("flags", [{"force_skip": True}, {"force_skip_re_reconcile": True}, {"force_rerun_before": False}])
def test_reconciliation_cannot_bypass_rerun(reconciliation, flags):
    db, portfolio = reconciliation
    with pytest.raises(HTTPException) as error:
        api.confirm_portfolio_reconciliation(
            portfolio.id, api.ConfirmReconciliationRequest(ack=True, review_note="已核对成交持仓资金并修复所有差异", **flags), db, "owner",
        )
    assert error.value.status_code == 400
    assert api._psm_get_status(db, portfolio) == "RECONCILIATION_BLOCKED"


@pytest.mark.parametrize("report_status,diffs", [("BLOCKED", []), ("PASSED", [ReconciliationDiff(symbol_id=None, kind="NOT_CHECKED")])])
def test_reconciliation_only_unlocks_passed_zero_diffs(reconciliation, monkeypatch, report_status, diffs):
    db, portfolio = reconciliation
    monkeypatch.setattr("app.services.portfolio_reconciliation.reconcile_trade_date", lambda *a, **k: ReconciliationReport(
        portfolio_id=portfolio.id, trade_date=portfolio.last_decision_trade_date,
        decision_run_id=None, status=report_status, differences=diffs,
    ))
    with pytest.raises(HTTPException) as error:
        api.confirm_portfolio_reconciliation(
            portfolio.id, api.ConfirmReconciliationRequest(ack=True, review_note="已核对成交持仓资金并修复所有差异"), db, "owner",
        )
    assert error.value.status_code == 409
    assert portfolio.status_reconciliation == "RECONCILIATION_BLOCKED"


def test_reconciliation_saves_trimmed_note_and_preserves_model_lock(reconciliation, monkeypatch):
    db, portfolio = reconciliation
    portfolio.status_model = "MODEL_INACTIVE"
    db.commit()
    seen_dates = []
    def passed(db, pid, td, **kwargs):
        seen_dates.append(td)
        return ReconciliationReport(portfolio_id=pid, trade_date=td, decision_run_id=None, status="PASSED")
    monkeypatch.setattr("app.services.portfolio_reconciliation.reconcile_trade_date", passed)
    response = api.confirm_portfolio_reconciliation(
        portfolio.id, api.ConfirmReconciliationRequest(ack=True, review_note="  已核对成交持仓资金并修复所有差异  "), db, "owner",
    )
    assert seen_dates == [portfolio.last_decision_trade_date]
    assert response.portfolio_now_ready is False
    assert portfolio.status_reconciliation == "READY"
    assert portfolio.status_model == "MODEL_INACTIVE"
    event = db.get(DataGovernanceAuditEvent, response.audit_event_id)
    assert event.action == "DATA_BLOCK_RESOLUTION"
    assert event.note == "已核对成交持仓资金并修复所有差异"
    assert event.operator_id == "owner"
    assert json.loads(event.attributes_json)["confirmation_type"] == "RECONCILIATION_CONFIRMED"


@pytest.mark.parametrize("legacy_state", ["READY", "ADMIN_PAUSED", "INTERRUPTED", "PENDING_INITIAL_REVIEW", "MODEL_INACTIVE"])
def test_dimension_only_reconciliation_confirmation_preserves_independent_runtime_state(reconciliation, monkeypatch, legacy_state):
    from app.services.portfolio_reconciliation import _finalize_report
    db, portfolio = reconciliation
    _legacy_state(db, portfolio, legacy_state)
    db.commit()
    def passed(db, pid, td, **kwargs):
        report = ReconciliationReport(portfolio_id=pid, trade_date=td, decision_run_id=None, status="PASSED")
        # Exercise the real report finalizer, including its audit/cursor write.
        _finalize_report(db, portfolio, report, kwargs["operator_id"], kwargs["correlation_id"])
        return report
    monkeypatch.setattr("app.services.portfolio_reconciliation.reconcile_trade_date", passed)
    response = api.confirm_portfolio_reconciliation(
        portfolio.id, api.ConfirmReconciliationRequest(ack=True, review_note="已核对成交持仓资金并修复所有差异"), db, "owner",
    )
    assert portfolio.status_reconciliation == "READY"
    assert response.from_state == legacy_state
    assert response.to_state == legacy_state
    assert api._psm_get_status(db, portfolio) == legacy_state
    assert response.portfolio_now_ready is (legacy_state == "READY")


def test_reconciliation_audit_failure_rolls_back_state_and_cursor(reconciliation, monkeypatch):
    db, portfolio = reconciliation
    monkeypatch.setattr("app.services.portfolio_reconciliation.reconcile_trade_date", lambda db, pid, td, **kwargs: ReconciliationReport(
        portfolio_id=pid, trade_date=td, decision_run_id=None, status="PASSED",
    ))
    def fail(*a, **k):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr("app.services.data_governance_audit.write_audit_event", fail)
    with pytest.raises(RuntimeError):
        api.confirm_portfolio_reconciliation(
            portfolio.id, api.ConfirmReconciliationRequest(ack=True, review_note="已核对成交持仓资金并修复所有差异"), db, "owner",
        )
    db.refresh(portfolio)
    assert portfolio.status_reconciliation == "RECONCILIATION_BLOCKED"
    assert portfolio.last_reconciled_trade_date is None
    assert api._psm_get_status(db, portfolio) == "RECONCILIATION_BLOCKED"
