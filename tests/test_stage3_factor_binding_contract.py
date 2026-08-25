"""Stage 3 public contracts for FactorSet -> model -> Score -> strategy binding.

These tests deliberately exercise public service/API seams rather than the
private implementation details of any individual pipeline component.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
from app.models.async_task import AsyncTaskRecord
from app.models.factor import Factor
from app.models.factor_evaluation import FactorSet, FactorSetMember
from app.models.factor_model import FactorModelRun, FactorVersion, FactorWeightSnapshot
from app.models.factor_runtime import FactorRuntimeState, FactorSystemConfig
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol


def _portfolio(db_session) -> Portfolio:
    portfolio = Portfolio(
        name="stage3-factor-binding",
        account_type="sim",
        asset_scope="mixed",
        total_capital=100_000.0,
        investable_ratio=1.0,
        cash_reserve_ratio=0.05,
        currency="CNY",
        buy_fee_pct=0.00025,
        sell_fee_pct=0.00025,
        benchmark_code="000300",
        default_single_position_pct=0.2,
        auto_trade_enabled=0,
    )
    db_session.add(portfolio)
    db_session.flush()
    return portfolio


def _ready_model(db_session, *, set_id: str, model_id: str) -> FactorModelRun:
    factor = Factor(
        code=f"factor_{set_id}",
        name=f"Factor {set_id}",
        category="fundamental",
        direction="higher_better",
        status="active",
        lifecycle_status="active",
        is_active=1,
        origin="test",
    )
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(
        factor_id=factor.id,
        version=1,
        formula_expr="1",
        validation_status="valid",
    )
    factor_set = FactorSet(
        id=set_id,
        name=set_id,
        status="frozen",
        content_hash=("a" * 32),
        frozen_at=datetime(2026, 8, 21),
        created_by="test",
    )
    db_session.add_all([version, factor_set])
    db_session.flush()
    db_session.add(FactorSetMember(
        factor_set_id=set_id,
        factor_id=factor.id,
        factor_version_id=version.id,
        factor_code=factor.code,
        factor_version=1,
        role="feature",
        display_order=0,
        missing_policy="exclude",
    ))
    model = FactorModelRun(
        id=model_id,
        model_type="ridge",
        asset_type="stock",
        target_code="target_5d_return",
        status="validated",
        sample_count=1000,
        symbol_count=100,
        trade_date_count=250,
        feature_versions_json=json.dumps({factor.code: 1, "__factor_set_id__": set_id}),
        hyperparameters_json=json.dumps({"factor_set_id": set_id}),
        metrics_json="{}",
    )
    model.weights.append(FactorWeightSnapshot(
        factor_code=factor.code,
        factor_version=1,
        coefficient=1.0,
        normalized_weight=1.0,
    ))
    db_session.add(model)
    db_session.flush()
    return model


def _activate(db_session, model_id: str) -> None:
    db_session.add(FactorRuntimeState(
        id=1,
        weight_mode="ridge",
        active_model_run_id=model_id,
        updated_by="test",
        version=1,
    ))
    db_session.flush()


def _enable_factor_pipeline(db_session, warehouse_path) -> None:
    config = db_session.get(FactorSystemConfig, 1)
    if config is None:
        config = FactorSystemConfig(
            id=1,
            feature_enabled=1,
            warehouse_path=str(warehouse_path),
            updated_by="stage3-test",
        )
        db_session.add(config)
    else:
        config.feature_enabled = 1
        config.warehouse_path = str(warehouse_path)
        config.updated_by = "stage3-test"
    db_session.commit()


def test_strategy_options_only_expose_active_ready_model_and_its_factor_set(db_session):
    """A strategy can only select the runtime active, readiness-qualified pair."""
    from app.services.factor_usage_service import get_factor_usage_options

    portfolio = _portfolio(db_session)
    active = _ready_model(
        db_session, set_id="fs-stage3-active", model_id="model-stage3-active"
    )
    _ready_model(
        db_session, set_id="fs-stage3-inactive", model_id="model-stage3-inactive"
    )
    _activate(db_session, active.id)

    options = get_factor_usage_options(db_session, portfolio.id)

    assert [item.factor_model_run_id for item in options.factor_models] == [active.id]
    assert options.factor_models[0].factor_set_id == "fs-stage3-active"
    assert [item.factor_set_id for item in options.factor_sets] == ["fs-stage3-active"]
    assert not options.blocking_reasons


def test_model_with_conflicting_factor_set_metadata_is_not_selectable_or_bindable(db_session):
    """A model must fail closed when its two immutable lineage fields disagree."""
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services.factor_usage_service import get_factor_usage_options, preflight_usage

    portfolio = _portfolio(db_session)
    active = _ready_model(
        db_session,
        set_id="fs-stage3-metadata-bound",
        model_id="model-stage3-metadata-bound",
    )
    _ready_model(
        db_session,
        set_id="fs-stage3-metadata-other",
        model_id="model-stage3-metadata-other",
    )
    active.feature_versions_json = json.dumps({
        "__factor_set_id__": "fs-stage3-metadata-other",
    })
    _activate(db_session, active.id)
    request = FactorUsageBindRequest(
        factor_model_run_id=active.id,
        factor_set_id="fs-stage3-metadata-bound",
        run_mode="production_pit",
        pit_mode="strict_pit_safe",
    )

    options = get_factor_usage_options(db_session, portfolio.id)
    preflight = preflight_usage(db_session, portfolio.id, request, "stage3-test")

    assert options.factor_models == []
    assert any(
        "MODEL_FACTOR_SET_METADATA_MISMATCH" in reason
        for reason in options.blocking_reasons
    )
    assert preflight.ok is False
    assert "MODEL_FACTOR_SET_METADATA_MISMATCH" in {
        warning.code for warning in preflight.warnings
    }


def test_strategy_binding_rejects_factor_set_member_with_foreign_version(db_session):
    """A frozen FactorSet member must reference a version of its own factor."""
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services.factor_usage_service import get_factor_usage_options, preflight_usage

    portfolio = _portfolio(db_session)
    active = _ready_model(
        db_session,
        set_id="fs-stage3-member-bound",
        model_id="model-stage3-member-bound",
    )
    other_factor = Factor(
        code="factor_stage3_member_other",
        name="Factor stage3 member other",
        category="fundamental",
        direction="higher_better",
        status="active",
        lifecycle_status="active",
        is_active=1,
        origin="test",
    )
    db_session.add(other_factor)
    db_session.flush()
    other_version = FactorVersion(
        factor_id=other_factor.id,
        version=1,
        formula_expr="1",
        validation_status="valid",
    )
    db_session.add(other_version)
    db_session.flush()
    member = db_session.execute(
        select(FactorSetMember).where(
            FactorSetMember.factor_set_id == "fs-stage3-member-bound"
        )
    ).scalar_one()
    member.factor_version_id = other_version.id
    _activate(db_session, active.id)
    request = FactorUsageBindRequest(
        factor_model_run_id=active.id,
        factor_set_id="fs-stage3-member-bound",
        run_mode="production_pit",
        pit_mode="strict_pit_safe",
    )

    options = get_factor_usage_options(db_session, portfolio.id)
    preflight = preflight_usage(db_session, portfolio.id, request, "stage3-test")

    assert options.factor_models == []
    assert any("FACTOR_SET_NOT_READY" in reason for reason in options.blocking_reasons)
    warning = next(
        item for item in preflight.warnings if item.code == "FACTOR_SET_NOT_READY"
    )
    assert warning.detail["details"]["factor_set_readiness"]["code"] == (
        "MEMBER_FACTOR_VERSION_OWNER_MISMATCH"
    )


def test_save_and_apply_freezes_explicit_key_members_in_snapshot(db_session):
    """Snapshot-level Score gates must retain the requested key-member set."""
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services.factor_usage_service import save_and_apply_usage

    portfolio = _portfolio(db_session)
    active = _ready_model(
        db_session,
        set_id="fs-stage3-key-members",
        model_id="model-stage3-key-members",
    )
    symbols = [
        Symbol(
            symbol=f"stage3-key-member-{suffix}",
            name=f"stage3-key-member-{suffix}",
            market="SZ",
            asset_type="stock",
            is_active=1,
        )
        for suffix in ("first", "second")
    ]
    db_session.add_all(symbols)
    db_session.flush()
    _activate(db_session, active.id)

    result = save_and_apply_usage(
        db_session,
        portfolio.id,
        FactorUsageBindRequest(
            factor_model_run_id=active.id,
            factor_set_id="fs-stage3-key-members",
            run_mode="production_pit",
            pit_mode="strict_pit_safe",
            key_members_json=[symbols[1].id, symbols[0].id],
        ),
        "stage3-test",
    )

    snapshot = db_session.get(StrategyExecutionSnapshot, result.strategy_snapshot_id)
    assert snapshot is not None
    assert json.loads(snapshot.key_members_json) == [symbols[1].id, symbols[0].id]


def test_key_member_change_has_a_distinct_snapshot_idempotency_identity(db_session):
    """Changing a snapshot-level Score gate cannot replay an older binding."""
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services.decision_clock import resolve
    from app.services.factor_usage_service import save_and_apply_usage

    portfolio = _portfolio(db_session)
    active = _ready_model(
        db_session,
        set_id="fs-stage3-key-member-identity",
        model_id="model-stage3-key-member-identity",
    )
    symbols = [
        Symbol(
            symbol=f"stage3-key-identity-{suffix}",
            name=f"stage3-key-identity-{suffix}",
            market="SZ",
            asset_type="stock",
            is_active=1,
        )
        for suffix in ("first", "second")
    ]
    db_session.add_all(symbols)
    db_session.flush()
    _activate(db_session, active.id)
    clock = resolve(date(2026, 8, 21), mode="t_day_close")
    fixed_now = datetime(2026, 8, 21, 7, 0)

    def save_for(key_member_id: int):
        return save_and_apply_usage(
            db_session,
            portfolio.id,
            FactorUsageBindRequest(
                factor_model_run_id=active.id,
                factor_set_id="fs-stage3-key-member-identity",
                run_mode="production_pit",
                pit_mode="strict_pit_safe",
                key_members_json=[key_member_id],
            ),
            "stage3-test",
        )

    with patch(
        "app.services.factor_usage_service._utcnow_naive", return_value=fixed_now
    ), patch(
        "app.services.factor_usage_service._resolve_clock_for_now", return_value=clock
    ):
        first = save_for(symbols[0].id)
        second = save_for(symbols[1].id)

    assert first.strategy_snapshot_id != second.strategy_snapshot_id
    assert first.idempotency_key != second.idempotency_key


def test_snapshot_binding_rejects_factor_set_mismatched_to_active_model(db_session):
    """A client cannot pair the active model with another frozen FactorSet."""
    from app.schemas.decision_engine import FactorUsageBindRequest
    from app.services.factor_usage_service import preflight_usage, save_and_apply_usage

    portfolio = _portfolio(db_session)
    active = _ready_model(
        db_session, set_id="fs-stage3-bound", model_id="model-stage3-bound"
    )
    _ready_model(
        db_session, set_id="fs-stage3-other", model_id="model-stage3-other"
    )
    _activate(db_session, active.id)
    request = FactorUsageBindRequest(
        factor_model_run_id=active.id,
        factor_set_id="fs-stage3-other",
        run_mode="production_pit",
        pit_mode="strict_pit_safe",
    )

    preflight = preflight_usage(db_session, portfolio.id, request, "stage3-test")

    assert preflight.ok is False
    assert "MODEL_FACTOR_SET_MISMATCH" in {warning.code for warning in preflight.warnings}
    with pytest.raises(HTTPException) as error:
        save_and_apply_usage(db_session, portfolio.id, request, "stage3-test")
    assert error.value.status_code == 400
    assert db_session.scalar(select(func.count()).select_from(PortfolioFactorUsage)) == 0
    assert db_session.scalar(select(func.count()).select_from(StrategyExecutionSnapshot)) == 0


def test_retiring_active_model_falls_back_and_leaves_audit_trail(db_session):
    """Retirement removes a model from live selection rather than reusing its Scores."""
    from app.models.factor_runtime import FactorModelAuditLog
    from app.services.factor_model_contract import assess_factor_model_readiness
    from app.services.factors.runtime import retire_factor_model

    model = _ready_model(
        db_session, set_id="fs-stage3-retire", model_id="model-stage3-retire"
    )
    _activate(db_session, model.id)

    runtime = retire_factor_model(
        db_session,
        model.id,
        actor="stage3-test",
        reason="data lineage superseded",
    )

    assert runtime.weight_mode == "manual"
    assert runtime.active_model_run_id is None
    assert model.status == "retired"
    readiness = assess_factor_model_readiness(db_session, model_run_id=model.id)
    assert readiness.ready is False
    assert readiness.code == "MODEL_RETIRED"
    audit = db_session.execute(
        select(FactorModelAuditLog).where(FactorModelAuditLog.model_run_id == model.id)
    ).scalars().all()
    assert any(item.action == "retire" and "lineage superseded" in (item.note or "") for item in audit)


def test_retire_model_api_exposes_the_fail_closed_runtime_transition(db_session):
    """The HTTP-facing operation must not require callers to mutate model status directly."""
    from app.api.routes.factor_models import (
        FactorModelRetireRequest,
        retire_model,
    )

    model = _ready_model(
        db_session, set_id="fs-stage3-retire-api", model_id="model-stage3-retire-api"
    )
    _activate(db_session, model.id)

    response = retire_model(
        model.id,
        FactorModelRetireRequest(actor="api-test", reason="superseded"),
        db_session,
    )

    assert response["weight_mode"] == "manual"
    assert response["active_model_run_id"] is None


def test_strict_score_query_uses_latest_visible_revision_not_future_revision(db_session):
    """A future revision must not hide the earlier PIT-safe Score for the same symbol."""
    from app.services.score_query_service import estimate_score_coverage, fetch_latest_pit_scores

    symbol = Symbol(
        symbol="stage3-score", name="stage3-score", market="SZ",
        asset_type="stock", is_active=1,
    )
    db_session.add(symbol)
    db_session.flush()
    cutoff = datetime(2026, 8, 21, 7, 0)
    base = dict(
        symbol_id=symbol.id,
        trade_date=date(2026, 8, 21),
        quality_score=70.0,
        quality_grade="A",
        timing_score=70.0,
        stage="growth",
        action="HOLD",
        priority_score=70.0,
        weight_mode="ridge",
        factor_model_run_id="model-stage3-score",
        factor_set_id="fs-stage3-score",
        factor_data_cutoff_at=cutoff,
        model_alpha_score=70.0,
    )
    db_session.add_all([
        Score(**base, calc_batch_id="stage3-safe", published_at=datetime(2026, 8, 21, 6, 59)),
        Score(**base, calc_batch_id="stage3-future", published_at=datetime(2026, 8, 21, 7, 1)),
    ])
    db_session.flush()

    rows = fetch_latest_pit_scores(
        db_session,
        factor_model_run_id="model-stage3-score",
        factor_set_id="fs-stage3-score",
        symbol_ids=[symbol.id],
        decision_date=date(2026, 8, 21),
        data_cutoff_at=cutoff,
        require_published_at=True,
    )
    expected, actual, max_age = estimate_score_coverage(
        db_session,
        factor_model_run_id="model-stage3-score",
        factor_set_id="fs-stage3-score",
        symbol_ids=[symbol.id],
        decision_date=date(2026, 8, 21),
        data_cutoff_at=cutoff,
        require_published_at=True,
    )

    assert [row.score_id for row in rows] == [
        db_session.execute(select(Score.id).where(Score.calc_batch_id == "stage3-safe")).scalar_one()
    ]
    assert (expected, actual, max_age) == (1, 1, 0)


def test_retired_runtime_model_cannot_expose_its_score_scope(db_session):
    """A stale runtime pointer must return no dynamic Scores, never a manual fallback."""
    from app.services.factors.score_scope import apply_active_score_scope

    model = _ready_model(
        db_session, set_id="fs-stage3-scope", model_id="model-stage3-scope"
    )
    model.status = "retired"
    _activate(db_session, model.id)
    symbol = Symbol(
        symbol="stage3-retired-scope", name="stage3-retired-scope", market="SZ",
        asset_type="stock", is_active=1,
    )
    db_session.add(symbol)
    db_session.flush()
    db_session.add(Score(
        symbol_id=symbol.id,
        trade_date=date(2026, 8, 21),
        quality_score=80.0,
        quality_grade="A",
        timing_score=80.0,
        stage="growth",
        action="HOLD",
        priority_score=80.0,
        weight_mode="ridge",
        factor_model_run_id=model.id,
        calc_batch_id="stage3-retired-scope",
    ))
    db_session.flush()

    rows = db_session.execute(
        apply_active_score_scope(select(Score), db_session)
    ).scalars().all()

    assert rows == []


def test_score_api_rejects_model_factor_set_mismatch_before_calculation(db_session):
    """The public Score endpoint cannot create an untraceable model/set pair."""
    from app.api.routes.scores import calculate_scores
    from app.schemas.score import ScoreCalculationRequest

    model = _ready_model(
        db_session, set_id="fs-stage3-score-api", model_id="model-stage3-score-api"
    )
    _ready_model(
        db_session, set_id="fs-stage3-score-api-other", model_id="model-stage3-score-api-other"
    )
    _activate(db_session, model.id)

    with pytest.raises(HTTPException) as error:
        calculate_scores(
            ScoreCalculationRequest(
                symbol_ids=[],
                trade_date=date(2026, 8, 21),
                weight_mode="ridge",
                factor_model_run_id=model.id,
                factor_set_id="fs-stage3-score-api-other",
            ),
            db_session,
        )

    assert error.value.status_code == 422
    assert "MODEL_FACTOR_SET_MISMATCH" in str(error.value.detail)


def test_model_train_api_rejects_non_frozen_factor_set_in_every_mode(db_session):
    """Offline training is not an exception to immutable FactorSet traceability."""
    from app.api.routes.factor_models import FactorModelTrainRequest, train_factor_model

    _ready_model(
        db_session, set_id="fs-stage3-draft-train", model_id="model-stage3-draft-seed"
    )
    db_session.get(FactorSet, "fs-stage3-draft-train").status = "draft"
    db_session.flush()

    with pytest.raises(HTTPException) as error:
        train_factor_model(
            FactorModelTrainRequest(
                factor_set_id="fs-stage3-draft-train",
                mode="offline_minimal",
            ),
            db_session,
        )

    assert error.value.status_code == 400
    assert "frozen" in str(error.value.detail)


@pytest.mark.parametrize(
    ("factor_set_id", "setup", "expected_code"),
    [
        (None, None, "FACTOR_SET_REQUIRED"),
        ("", None, "FACTOR_SET_REQUIRED"),
        ("fs-stage3-pipeline-missing", None, "FACTOR_SET_NOT_FOUND"),
        ("fs-stage3-pipeline-draft", "draft", "FACTOR_SET_NOT_FROZEN"),
        ("fs-stage3-pipeline-no-hash", "no_hash", "FACTOR_SET_NO_CONTENT_HASH"),
    ],
)
def test_async_training_task_rejects_unready_factor_set_before_queueing(
    db_session, tmp_path, factor_set_id, setup, expected_code,
):
    """Public async training cannot queue an untraceable FactorSet binding."""
    from app.api.routes.factor_pipeline import create_pipeline_task
    from app.schemas.async_task import FactorPipelineCreate

    _enable_factor_pipeline(db_session, tmp_path / "pipeline-gate.duckdb")
    if setup == "draft":
        db_session.add(FactorSet(
            id=factor_set_id,
            name=factor_set_id,
            status="draft",
            created_by="stage3-test",
        ))
        db_session.commit()
    elif setup == "no_hash":
        _ready_model(
            db_session,
            set_id=factor_set_id,
            model_id="model-stage3-pipeline-no-hash",
        )
        db_session.get(FactorSet, factor_set_id).content_hash = None
        db_session.commit()

    with patch("app.services.factors.pipeline_task._start_worker"):
        with pytest.raises(HTTPException) as error:
            create_pipeline_task(FactorPipelineCreate(
                train_model=True,
                factor_set_id=factor_set_id,
            ))

    assert error.value.status_code == 422
    assert error.value.detail["error_code"] == expected_code
    assert db_session.scalar(
        select(func.count()).select_from(AsyncTaskRecord)
        .where(AsyncTaskRecord.task_type == "factor_pipeline")
    ) == 0


def test_async_training_task_persists_ready_factor_set_binding(db_session, tmp_path):
    """A readiness-qualified FactorSet reaches the queued task unchanged."""
    from app.api.routes.factor_pipeline import create_pipeline_task
    from app.schemas.async_task import FactorPipelineCreate

    _enable_factor_pipeline(db_session, tmp_path / "pipeline-ready.duckdb")
    _ready_model(
        db_session,
        set_id="fs-stage3-pipeline-ready",
        model_id="model-stage3-pipeline-ready",
    )
    db_session.commit()

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_pipeline_task(FactorPipelineCreate(
            train_model=True,
            factor_set_id="fs-stage3-pipeline-ready",
        ))

    db_session.expire_all()
    task = db_session.get(AsyncTaskRecord, result["id"])
    assert task is not None
    assert json.loads(task.payload_json)["factor_set_id"] == "fs-stage3-pipeline-ready"


def test_async_data_prep_allows_no_factor_set(db_session, tmp_path):
    """The no-training data-preparation path remains backward compatible."""
    from app.api.routes.factor_pipeline import create_pipeline_task
    from app.schemas.async_task import FactorPipelineCreate

    _enable_factor_pipeline(db_session, tmp_path / "pipeline-data-prep.duckdb")

    with patch("app.services.factors.pipeline_task._start_worker"):
        result = create_pipeline_task(FactorPipelineCreate(
            train_model=False,
            materialize_scores=False,
        ))

    assert result["task_type"] == "factor_pipeline"
