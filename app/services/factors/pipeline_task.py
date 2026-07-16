'''Cancellable local factor pipeline backed by the existing async task table.'''
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, timedelta

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import FactorPipelineCreate
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.factors.bar_mirror import mirror_daily_bars
from app.services.factors.config import (
    get_current_factor_system_config,
    get_factor_system_config,
)
from app.services.factors.data_sync import mirror_factor_inputs
from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factors.ridge_model import train_rolling_ridge
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.factors.scoring_bridge import materialize_factor_scores
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets


logger = logging.getLogger(__name__)
TASK_TYPE = 'factor_pipeline'


def resolve_pipeline_dates(
    payload: FactorPipelineCreate,
    *,
    latest_bar_date: date | None,
    today: date | None = None,
) -> tuple[date, date]:
    end_date = payload.end_date or today or date.today()
    if payload.start_date is not None:
        return payload.start_date, end_date
    anchor = latest_bar_date or end_date
    lookback_days = 550 if payload.train_model else 45
    return anchor - timedelta(days=lookback_days), end_date


def resolve_calculation_start(
    payload: FactorPipelineCreate,
    *,
    mirror_start_date: date,
    latest_bar_date: date | None,
) -> date:
    if payload.start_date is not None or payload.train_model:
        return mirror_start_date
    anchor = latest_bar_date or mirror_start_date
    return max(mirror_start_date, anchor - timedelta(days=10))


def _cancelled(task_id: str) -> bool:
    """Read cancellation in a fresh transaction.

    The pipeline's main SQLAlchemy session can remain inside a long-running
    mirror transaction. Reusing its identity map would hide a cancellation
    committed by the API session until the whole mirror phase completes.
    """
    SessionLocal = get_session_local()
    cancel_db = SessionLocal()
    try:
        task = cancel_db.get(AsyncTaskRecord, task_id)
        return task is not None and task.status == 'cancelled'
    finally:
        cancel_db.close()


def _latest_factor_date(
    warehouse: FactorWarehouse,
    calc_batch_id: str,
) -> date | None:
    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            '''
            SELECT MAX(trade_date)
            FROM factor_values
            WHERE calc_batch_id = ?
            ''',
            [calc_batch_id],
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def create_factor_pipeline_task(payload: FactorPipelineCreate) -> dict:
    factor_config = get_current_factor_system_config()
    if not factor_config.feature_enabled:
        raise ValueError(
            'Factor pipeline is disabled; enable it in Settings > Factor Models'
        )
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {'queued', 'running'}:
        return existing[0].model_dump()
    task = create_async_task(TASK_TYPE, payload.model_dump())
    _start_worker(task.id, _run_factor_pipeline)
    return task.model_dump()


def _run_factor_pipeline(task_id: str) -> None:
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        if task.status == 'cancelled':
            return
        payload = FactorPipelineCreate.model_validate(
            json.loads(task.payload_json or '{}')
        )
        if payload.validation_days >= payload.window_days:
            raise ValueError('validation_days must be less than window_days')
        factor_config = get_factor_system_config(db)
        if not factor_config.feature_enabled:
            raise ValueError('Factor pipeline was disabled before execution')
        should_cancel = lambda: _cancelled(task_id)
        if should_cancel():
            return
        warehouse = FactorWarehouse(factor_config.warehouse_path)
        warehouse_health = warehouse.health()
        latest_bar_date = (
            date.fromisoformat(warehouse_health.latest_trade_date)
            if warehouse_health.latest_trade_date
            else None
        )
        effective_start_date, effective_end_date = (
            resolve_pipeline_dates(
                payload, latest_bar_date=latest_bar_date
            )
        )
        calculation_start_date = resolve_calculation_start(
            payload,
            mirror_start_date=effective_start_date,
            latest_bar_date=latest_bar_date,
        )
        results: dict = {}
        results['effective_range'] = {
            'mirror_start_date': effective_start_date,
            'calculation_start_date': calculation_start_date,
            'end_date': effective_end_date,
        }

        _set_task(
            db,
            task_id,
            status='running',
            stage='mirror',
            percent=5,
            message='Mirroring local market and factor inputs',
            started_at=_now(),
        )
        bars = mirror_daily_bars(
            db,
            warehouse=warehouse,
            start_date=effective_start_date,
            end_date=effective_end_date,
            full_refresh=payload.full_refresh,
            should_cancel=should_cancel,
        )
        if should_cancel():
            return
        inputs = mirror_factor_inputs(
            db,
            warehouse=warehouse,
            start_date=effective_start_date,
            end_date=effective_end_date,
            full_refresh=payload.full_refresh,
            should_cancel=should_cancel,
        )
        results['bar_mirror'] = bars.to_dict()
        results['input_mirror'] = {
            **asdict(inputs),
            'rows_written': inputs.rows_written,
        }
        if should_cancel():
            return

        _set_task(
            db,
            task_id,
            stage='factors',
            percent=35,
            message='Calculating cross-sectional factors',
        )
        factors = calculate_stock_factors(
            warehouse,
            start_date=calculation_start_date,
            end_date=effective_end_date,
        )
        results['factors'] = asdict(factors)
        if should_cancel():
            return

        _set_task(
            db,
            task_id,
            stage='targets',
            percent=55,
            message='Generating T+1 to T+5 labels',
        )
        targets = calculate_targets(
            warehouse,
            start_date=calculation_start_date,
            end_date=effective_end_date,
        )
        results['targets'] = asdict(targets)
        if should_cancel():
            return

        model_result = None
        if payload.train_model:
            _set_task(
                db,
                task_id,
                stage='train',
                percent=70,
                message='Training and validating Ridge candidate',
            )
            model_result = train_rolling_ridge(
                db,
                warehouse,
                factor_calc_batch_id=factors.calc_batch_id,
                target_calc_batch_id=targets.calc_batch_id,
                data_cutoff_date=(
                    payload.data_cutoff_date
                    or effective_end_date
                    or date.today()
                ),
                window_days=payload.window_days,
                validation_days=payload.validation_days,
            )
            db.commit()
            results['model'] = asdict(model_result)
        if should_cancel():
            return

        runtime = get_factor_runtime_snapshot(db)
        if (
            payload.materialize_scores
            and runtime.weight_mode in {'shadow', 'ridge'}
            and runtime.active_model_run_id
        ):
            _set_task(
                db,
                task_id,
                stage='score',
                percent=88,
                message='Materializing active factor score batch',
            )
            latest_date = _latest_factor_date(
                warehouse, factors.calc_batch_id
            )
            if latest_date is not None:
                bridge = materialize_factor_scores(
                    db,
                    warehouse,
                    trade_date=latest_date,
                    factor_calc_batch_id=factors.calc_batch_id,
                    model_run_id=runtime.active_model_run_id,
                    mode=runtime.weight_mode,
                    pipeline_run_id=task_id,
                )
                db.commit()
                results['scoring_bridge'] = asdict(bridge)
        results['runtime'] = runtime.to_dict()
        results['trained_model_auto_activated'] = False

        _set_task(
            db,
            task_id,
            status='done',
            stage='done',
            percent=100,
            message='Factor pipeline completed',
            result_json=json.dumps(
                results, ensure_ascii=False, default=str
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception('Factor pipeline task %s failed', task_id)
        try:
            _set_task(
                db,
                task_id,
                status='failed',
                stage='failed',
                message=f'Factor pipeline failed: {exc}',
                errors_json=json.dumps(
                    [{'stage': 'pipeline', 'error': str(exc)}],
                    ensure_ascii=False,
                ),
                finished_at=_now(),
            )
        except Exception:
            logger.exception('Failed to mark factor pipeline task failed')
    finally:
        db.close()


__all__ = [
    'TASK_TYPE',
    'create_factor_pipeline_task',
    'resolve_calculation_start',
    'resolve_pipeline_dates',
]
