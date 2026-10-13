"""Real validated binding seeds for tests of automatic matching/accounting."""
from datetime import datetime

from app.models.decision_engine import PortfolioFactorUsage
from app.models.portfolio import Portfolio
from tests.test_stage3_factor_binding_contract import _activate, _ready_model


def bind_applied_snapshot(db, snapshot):
    """Keep matching fixtures eligible without mocking the entry governor."""
    model_id = snapshot.factor_model_run_id or f"model-{snapshot.id}"
    set_id = f"fs-{model_id}"
    _ready_model(db, set_id=set_id, model_id=model_id)
    _activate(db, model_id)
    usage = PortfolioFactorUsage(
        id=f"usage-{snapshot.id}", portfolio_id=snapshot.portfolio_id,
        factor_model_run_id=model_id, factor_set_id=set_id,
        run_mode="production_sim", pit_mode="strict_pit_safe", status="active",
        content_hash=f"binding-{snapshot.id}", effective_from=datetime(2026, 1, 1),
    )
    db.add(usage)
    snapshot.portfolio_factor_usage_id = usage.id
    snapshot.factor_model_run_id = model_id
    snapshot.factor_set_id = set_id
    snapshot.snapshot_type = "save_and_apply"
    # These account/matching fixtures exercise an already approved portfolio.
    db.get(Portfolio, snapshot.portfolio_id).last_decision_trade_date = datetime(2026, 1, 1).date()

