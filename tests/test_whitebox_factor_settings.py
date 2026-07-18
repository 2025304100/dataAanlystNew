from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes.factors import (
    FactorSystemConfigUpdate,
    get_factors_overview,
    initialize_factor_warehouse,
    patch_factor_config,
)
from app.models.factor_runtime import FactorSystemConfig
from app.models.async_task import AsyncTaskRecord
from app.db.session import get_session_local
from app.schemas.async_task import FactorPipelineCreate
from app.services.factors.pipeline_task import (
    _cancelled,
    _touch_task_heartbeat,
    get_pipeline_eta,
    resolve_calculation_start,
    resolve_pipeline_dates,
)


pytestmark = pytest.mark.whitebox


def test_factor_feature_can_be_enabled_and_warehouse_initialized(
    db_session,
    tmp_path,
):
    warehouse_path = tmp_path / 'factor_settings.duckdb'
    db_session.add(
        FactorSystemConfig(
            id=1,
            feature_enabled=0,
            warehouse_path=str(warehouse_path),
            updated_by='test',
        )
    )
    db_session.commit()

    before = get_factors_overview(db_session)
    assert before['feature_enabled'] is False
    assert before['warehouse_error'] == 'warehouse_not_initialized'
    assert before['health']['warehouse_available'] is False

    with pytest.raises(HTTPException) as exc:
        initialize_factor_warehouse(db_session)
    assert exc.value.status_code == 409

    enabled = patch_factor_config(
        FactorSystemConfigUpdate(feature_enabled=True, actor='test'),
        db_session,
    )
    initialized = initialize_factor_warehouse(db_session)

    assert enabled['feature_enabled'] is True
    assert initialized['feature_enabled'] is True
    assert initialized['health']['warehouse_available'] is True
    assert warehouse_path.exists()


def test_factor_pipeline_rejects_invalid_window_relationship():
    with pytest.raises(ValidationError, match='validation_days'):
        FactorPipelineCreate(window_days=60, validation_days=60)


def test_factor_pipeline_uses_bounded_default_ranges():
    latest = __import__('datetime').date(2026, 7, 13)
    today = __import__('datetime').date(2026, 7, 15)

    daily = resolve_pipeline_dates(
        FactorPipelineCreate(train_model=False),
        latest_bar_date=latest,
        today=today,
    )
    training = resolve_pipeline_dates(
        FactorPipelineCreate(train_model=True),
        latest_bar_date=latest,
        today=today,
    )

    assert daily == (
        __import__('datetime').date(2026, 5, 29),
        today,
    )
    assert training == (
        __import__('datetime').date(2025, 1, 9),
        today,
    )
    assert resolve_calculation_start(
        FactorPipelineCreate(train_model=False),
        mirror_start_date=daily[0],
        latest_bar_date=latest,
    ) == __import__('datetime').date(2026, 7, 3)


def test_factor_pipeline_cancel_check_uses_fresh_session(db_session):
    task = AsyncTaskRecord(
        id='factor-cancel-test',
        task_type='factor_pipeline',
        status='running',
        stage='mirror',
    )
    db_session.add(task)
    db_session.commit()
    assert _cancelled(task.id) is False

    SessionLocal = get_session_local()
    cancel_db = SessionLocal()
    try:
        persisted = cancel_db.get(AsyncTaskRecord, task.id)
        persisted.status = 'cancelled'
        persisted.stage = 'cancelled'
        cancel_db.commit()
    finally:
        cancel_db.close()

    assert _cancelled(task.id) is True


def test_factor_pipeline_heartbeat_only_touches_running_task(db_session):
    base = datetime(2026, 7, 17, 1, 0)
    task = AsyncTaskRecord(
        id='factor-heartbeat-test',
        task_type='factor_pipeline',
        status='running',
        stage='factors',
        updated_at=base,
    )
    db_session.add(task)
    db_session.commit()

    assert _touch_task_heartbeat(task.id) is True
    db_session.expire_all()
    refreshed = db_session.get(AsyncTaskRecord, task.id)
    assert refreshed.updated_at > base

    refreshed.status = 'failed'
    db_session.commit()
    failed_updated_at = refreshed.updated_at
    assert _touch_task_heartbeat(task.id) is False
    db_session.expire_all()
    assert (
        db_session.get(AsyncTaskRecord, task.id).updated_at
        == failed_updated_at
    )


def test_factor_pipeline_eta_is_grouped_by_run_mode(db_session):
    base = datetime(2026, 7, 17, 1, 0)
    for index, duration in enumerate((100, 200, 300), start=1):
        db_session.add(
            AsyncTaskRecord(
                id=f"eta-train-{index}",
                task_type="factor_pipeline",
                status="done",
                stage="done",
                payload_json=json.dumps(
                    {"train_model": True, "full_refresh": False}
                ),
                started_at=base,
                finished_at=base + timedelta(seconds=duration),
            )
        )
    db_session.add(
        AsyncTaskRecord(
            id="eta-daily-1",
            task_type="factor_pipeline",
            status="done",
            stage="done",
            payload_json=json.dumps(
                {"train_model": False, "full_refresh": False}
            ),
            started_at=base,
            finished_at=base + timedelta(seconds=600),
        )
    )
    db_session.commit()

    training = get_pipeline_eta(
        train_model=True,
        full_refresh=False,
    )
    daily = get_pipeline_eta(
        train_model=False,
        full_refresh=False,
    )
    full_training = get_pipeline_eta(
        train_model=True,
        full_refresh=True,
    )

    assert training["sample_count"] == 3
    assert training["recommended_seconds"] == 200
    assert daily["sample_count"] == 1
    assert daily["recommended_seconds"] == 900
    assert full_training["sample_count"] == 0
    assert full_training["recommended_seconds"] == 3600
