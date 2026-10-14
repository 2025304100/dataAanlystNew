"""Real source admission and all-or-nothing portfolio strategy application."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.routes.portfolios import StrategyConfigurationRequest, save_strategy_configuration
from app.api.routes.factor_models import FactorModelTrainRequest, train_factor_model
from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
from app.models.factor_weight_snapshot import FactorWeightSnapshot
from app.models.portfolio import PortfolioRule
from app.models.signal_rule import SignalRule
from app.services.factor_model_contract import assess_factor_model_readiness
from app.services.factors.runtime import activate_factor_model
from tests.test_stage3_factor_binding_contract import _ready_model
from tests.test_whitebox_portfolio_confirmation import binding

pytestmark = pytest.mark.whitebox


def _request(model, fs, *, active=True, auto=None):
    return StrategyConfigurationRequest.model_validate({
        "rule": {"rule_name": "真实训练策略", "max_single_position_pct": 0.15,
                 "max_sector_position_pct": 0.4, "max_stock_position_pct": 0.8,
                 "max_etf_position_pct": 0.5, "max_loss_per_trade_pct": 0.05,
                 "max_open_positions": 5, "stage_limits_json": {}, "is_active": active},
        "signal_rule": {"rule_name": "新信号", "quality_tolerance": 15},
        "binding": {"factor_model_run_id": model, "factor_set_id": fs,
                    "run_mode": "production_sim", "pit_mode": "strict_pit_safe"},
        "auto_trade_enabled": auto,
    })


def _live_rule(db, portfolio, usage, snapshot):
    payload = _request(snapshot.factor_model_run_id, snapshot.factor_set_id).rule
    from app.api.routes.portfolios import _persist_portfolio_rule
    rule = _persist_portfolio_rule(db, portfolio.id, payload)
    usage.rule_id = snapshot.rule_id = rule.id
    usage.rule_version = snapshot.rule_version = 1
    db.commit()
    return rule


@pytest.mark.parametrize("mode", ["offline_minimal", "demo", "unknown"])
def test_demo_or_unknown_training_cannot_be_released(db_session, mode):
    model = _ready_model(db_session, set_id="fs-source", model_id="model-source")
    hp = json.loads(model.hyperparameters_json)
    hp.update(mode=mode, train_mode=mode)
    model.hyperparameters_json = json.dumps(hp)
    db_session.flush()
    result = assess_factor_model_readiness(db_session, model_run_id=model.id)
    assert not result.ready and result.code == "MODEL_TRAINING_SOURCE_UNVERIFIED"
    with pytest.raises(ValueError, match="SOURCE_UNVERIFIED"):
        activate_factor_model(db_session, model.id, mode="ridge")


@pytest.mark.parametrize("broken,code", [
    ("missing_snapshot", "MODEL_WEIGHT_SNAPSHOT_MISSING"),
    ("coefficient", "MODEL_WEIGHT_SNAPSHOT_MISMATCH"),
    ("nonfinite", "MODEL_WEIGHTS_INVALID"),
    ("batch", "MODEL_TRAINING_EVIDENCE_MISSING"),
])
def test_historical_artifact_damage_requires_retraining(db_session, broken, code):
    model = _ready_model(db_session, set_id="fs-artifact", model_id="model-artifact")
    assert assess_factor_model_readiness(db_session, model_run_id=model.id).ready
    artifact = db_session.get(FactorWeightSnapshot, model.id)
    if broken == "missing_snapshot":
        db_session.delete(artifact)
    elif broken == "coefficient":
        artifact.weights_raw_json = json.dumps({model.weights[0].factor_code: 9.0})
    elif broken == "nonfinite":
        model.weights[0].normalized_weight = float("inf")
    else:
        hp = json.loads(model.hyperparameters_json)
        hp.pop("target_calc_batch_id")
        model.hyperparameters_json = json.dumps(hp)
    db_session.flush()
    assert assess_factor_model_readiness(db_session, model_run_id=model.id).code == code


def test_draft_preserves_live_rule_signal_auto_and_snapshot(binding):
    db, portfolio, _, usage, old = binding
    rule = _live_rule(db, portfolio, usage, old)
    portfolio.auto_trade_enabled = 1
    signal = SignalRule(portfolio_id=portfolio.id, rule_name="生效信号", is_active=1)
    db.add(signal)
    db.commit()
    response = save_strategy_configuration(portfolio.id, _request(old.factor_model_run_id, old.factor_set_id, active=False, auto=False), db)
    assert not response.applied and not response.requires_confirmation
    assert portfolio.auto_trade_enabled == 1 and rule.is_active == 1
    assert signal.is_active == 1 and signal.rule_name == "生效信号"
    assert db.scalar(select(func.count()).select_from(StrategyExecutionSnapshot)) == 1
    draft = db.get(PortfolioRule, response.rule_id)
    assert draft.is_active == 0
    assert json.loads(draft.stage_limits_json)["_draft_signal_rule"]["rule_name"] == "新信号"


def test_apply_freezes_real_binding_and_requires_personal_confirmation(binding):
    db, portfolio, _, usage, old = binding
    rule = _live_rule(db, portfolio, usage, old)
    portfolio.auto_trade_enabled = 0
    db.commit()
    response = save_strategy_configuration(portfolio.id, _request(old.factor_model_run_id, old.factor_set_id), db)
    assert response.applied and response.requires_confirmation
    assert portfolio.auto_trade_enabled == 0 and portfolio.status_model == "MODEL_INACTIVE"
    assert rule.is_active == 0
    applied = db.get(StrategyExecutionSnapshot, response.binding_result.strategy_snapshot_id)
    assert applied.rule_id == response.rule_id and applied.factor_model_run_id == old.factor_model_run_id
    versions = json.loads(applied.versions_json)
    assert versions["signal_policy"] == {"score_source": "model_alpha", "score_scale": 100,
                                         "buy_threshold": 70, "sell_threshold": 35}
    assert versions["signal_rule"]["rule_name"] == "新信号"
    assert versions["rule_config"]["position_config"]["value"] == 0.15
    assert versions["factor_member_versions"]
    assert versions["decision_clock_policy"]["cutoff_time"] == "20:00"
    repeated = save_strategy_configuration(portfolio.id, _request(old.factor_model_run_id, old.factor_set_id), db)
    assert repeated.binding_result.strategy_snapshot_id == applied.id
    assert db.scalar(select(func.count()).select_from(StrategyExecutionSnapshot)) == 2


def test_failed_binding_rolls_back_rule_signal_auto_and_usage(binding):
    db, portfolio, _, old_usage, old = binding
    rule = _live_rule(db, portfolio, old_usage, old)
    signal = SignalRule(portfolio_id=portfolio.id, rule_name="原信号", is_active=1)
    db.add(signal)
    portfolio.auto_trade_enabled = 0
    db.commit()
    with pytest.raises(HTTPException):
        save_strategy_configuration(portfolio.id, _request("missing-model", old.factor_set_id, auto=True), db)
    assert db.scalar(select(func.count()).select_from(PortfolioRule)) == 1
    assert db.scalar(select(func.count()).select_from(StrategyExecutionSnapshot)) == 1
    assert db.scalar(select(func.count()).select_from(PortfolioFactorUsage)) == 1
    assert portfolio.auto_trade_enabled == 0 and rule.is_active == 1 and old_usage.status == "active"
    assert signal.rule_name == "原信号"


def test_manual_apply_is_explicitly_blocked_and_can_be_drafted(binding):
    db, portfolio, _, _, old = binding
    request = _request(old.factor_model_run_id, old.factor_set_id).model_copy(update={"binding": None})
    with pytest.raises(HTTPException, match="PRODUCTION_BINDING_REQUIRED"):
        save_strategy_configuration(portfolio.id, request, db)
    request.rule.is_active = False
    assert not save_strategy_configuration(portfolio.id, request, db).applied


def test_native_training_default_submits_real_task_without_creating_demo(db_session, monkeypatch):
    from app.services.factors import __facade__
    _ready_model(db_session, set_id="fs-submit", model_id="model-submit")
    received = {}
    def submit(**kwargs):
        received.update(kwargs)
        return {"id": "real-training-task", "status": "pending"}
    monkeypatch.setattr(__facade__, "create_scoring_task", submit)
    response = train_factor_model(FactorModelTrainRequest(factor_set_id="fs-submit"), SimpleNamespace(headers={}), db_session)
    assert response.status_code == 202
    assert received["train_model"] and not received["mirror_only"]
    assert received["hyperparameters"]["mode"] == "warehouse"
    assert received["factor_set_id"] == "fs-submit"
    with pytest.raises(HTTPException, match="DEMO_TRAINING_DISABLED"):
        train_factor_model(FactorModelTrainRequest(factor_set_id="fs-submit", mode="offline_minimal"), SimpleNamespace(headers={}), db_session)


def test_custom_sla_survives_snapshot_loading(binding):
    from app.services.decision_engine import _stub_snapshot_loader
    db, portfolio, _, _, old = binding
    request = _request(old.factor_model_run_id, old.factor_set_id)
    request.binding.score_sla_coverage_pct = 80
    request.binding.score_sla_max_age_days = 7
    result = save_strategy_configuration(portfolio.id, request, db)
    loaded = _stub_snapshot_loader(db, result.binding_result.strategy_snapshot_id)
    assert loaded.versions["score_sla_coverage_pct"] == 80
    assert loaded.versions["score_sla_max_age_days"] == 7


@pytest.mark.parametrize("changed", ["signal", "expired_usage", "future_usage"])
def test_reapply_checks_live_signal_and_usage_interval(binding, changed):
    from datetime import datetime, timedelta, timezone
    db, portfolio, _, _, old = binding
    request = _request(old.factor_model_run_id, old.factor_set_id)
    first = save_strategy_configuration(portfolio.id, request, db)
    usage = db.get(PortfolioFactorUsage, first.binding_result.factor_usage.id)
    if changed == "signal":
        db.scalar(select(SignalRule).where(SignalRule.portfolio_id == portfolio.id)).quality_tolerance = 22
    elif changed == "expired_usage":
        usage.effective_to = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    else:
        usage.effective_from = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    db.commit()
    second = save_strategy_configuration(portfolio.id, request, db)
    assert second.binding_result.strategy_snapshot_id != first.binding_result.strategy_snapshot_id
    assert db.scalar(select(SignalRule).where(SignalRule.portfolio_id == portfolio.id)).quality_tolerance == 15


def test_snapshot_includes_authorized_candidates_and_exit_only_holdings(binding):
    from datetime import date, datetime
    from app.models.symbol import Symbol
    from app.models.portfolio import Position
    from app.models.portfolio_candidate import PortfolioCandidate
    from app.models.portfolio_member import PortfolioMember
    db, portfolio, _, _, old = binding
    symbols = [Symbol(symbol=f"00{i:04d}", name=f"symbol-{i}", asset_type="stock", market="sz", is_active=1) for i in range(1, 5)]
    db.add_all(symbols)
    db.flush()
    for index, symbol in enumerate(symbols[:3]):
        db.add(PortfolioCandidate(portfolio_id=portfolio.id, symbol_id=symbol.id,
                                 effective_from=date(2026, 1, 1), auto_authorized_flag=1,
                                 removed_manually_flag=int(index == 2)))
    db.add(PortfolioMember(portfolio_id=portfolio.id, symbol_id=symbols[1].id,
                           status="active", execution_mode="manual", effective_from=datetime(2026, 1, 1)))
    db.add(Position(portfolio_id=portfolio.id, symbol_id=symbols[3].id, quantity=100, avg_cost=10,
                    latest_price=10, market_value=1000, asset_type="stock"))
    db.commit()
    request = _request(old.factor_model_run_id, old.factor_set_id)
    first = save_strategy_configuration(portfolio.id, request, db)
    snapshot = db.get(StrategyExecutionSnapshot, first.binding_result.strategy_snapshot_id)
    members = {row["symbol_id"]: row for row in json.loads(snapshot.member_snapshot_json)}
    assert set(members) == {symbols[0].id, symbols[1].id, symbols[3].id}
    assert members[symbols[0].id]["source_type"] == "candidate"
    assert members[symbols[1].id]["execution_mode"] == "manual"
    assert members[symbols[3].id]["entry_authorized"] is False
    assert len(json.loads(snapshot.candidate_pool_json)) == 3
    # An exact retry reuses the snapshot; changing the universe needs new review.
    assert save_strategy_configuration(portfolio.id, request, db).binding_result.strategy_snapshot_id == snapshot.id
    portfolio.auto_trade_source_mode = "members_only"
    db.commit()
    second = save_strategy_configuration(portfolio.id, request, db)
    revised = db.get(StrategyExecutionSnapshot, second.binding_result.strategy_snapshot_id)
    assert {r["symbol_id"] for r in json.loads(revised.member_snapshot_json)} == {symbols[1].id, symbols[3].id}


@pytest.mark.parametrize("invalid", ["invalid", "NaN", "Infinity"])
def test_malformed_risk_policy_rolls_back_with_actionable_error(binding, invalid):
    db, portfolio, _, _, old = binding
    request = _request(old.factor_model_run_id, old.factor_set_id)
    request.rule.stage_limits_json["price_stop_loss_pct"] = invalid
    with pytest.raises(HTTPException, match="RISK_POLICY_INVALID"):
        save_strategy_configuration(portfolio.id, request, db)
    assert db.scalar(select(func.count()).select_from(StrategyExecutionSnapshot)) == 1
