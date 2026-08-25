'''Cancellable local factor pipeline backed by the existing async task table.'''
from __future__ import annotations

import json
import logging
from dataclasses import asdict
import threading
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import FactorPipelineCreate
from app.schemas.errors import (
    TechnicalDetails,
    build_user_error,
)
from app.services.async_tasks import (
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.factors.bar_mirror import mirror_daily_bars
from app.services.factors.batch_audit import batch_context
from app.services.factors.config import (
    get_current_factor_system_config,
    get_factor_system_config,
)
from app.services.factors.data_sync import mirror_factor_inputs
from app.services.factors.factor_engine import calculate_stock_factors
from app.services.factor_set_service import factor_set_readiness
from app.services.factors.ridge_model import train_rolling_ridge
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.factors.scoring_bridge import materialize_factor_scores
from app.services.factors.store import FactorWarehouse
from app.models.score import Score
from app.models.decision_engine import DecisionEvidence
from app.models.journal_entry import JournalEntry
from app.models.trade_setup import TradeSetup
from app.services.factors.target_engine import calculate_targets


logger = logging.getLogger(__name__)
TASK_TYPE = 'factor_pipeline'

# 样本不足时按运行模式回退；训练、全量刷新不能再沿用 3 分钟。
DEFAULT_ETA_SECONDS = {
    (False, False): 900,
    (True, False): 1200,
    (False, True): 1800,
    (True, True): 3600,
}
TASK_HEARTBEAT_SECONDS = 15.0


def _prune_unreferenced_scores(db, *, keep_batches: int = 30) -> dict[str, object]:
    """Bound SQLite/MySQL score growth while preserving all live references."""
    if keep_batches < 1:
        raise ValueError("keep_batches must be at least 1")
    rows = db.execute(
        select(Score.calc_batch_id, func.max(Score.created_at))
        .where(Score.calc_batch_id.is_not(None))
        .group_by(Score.calc_batch_id)
        .order_by(func.max(Score.created_at).desc(), Score.calc_batch_id.desc())
    ).all()
    retained = {str(row[0]) for row in rows[:keep_batches]}
    if not retained:
        return {"score_rows_deleted": 0, "protected_factor_batch_ids": []}
    referenced: set[int] = set()
    for model in (DecisionEvidence, JournalEntry, TradeSetup):
        try:
            referenced.update(
                int(value[0])
                for value in db.execute(
                    select(model.score_id).where(model.score_id.is_not(None))
                ).all()
            )
        except Exception:
            # Legacy databases may not yet have every optional table/column.
            continue
    candidates = db.execute(
        select(Score.id).where(
            Score.calc_batch_id.not_in(retained),
            ~Score.id.in_(referenced) if referenced else True,
        )
    ).scalars().all()
    if candidates:
        db.execute(delete(Score).where(Score.id.in_(candidates)))
    remaining_details = db.execute(select(Score.factor_scores_json)).all()
    protected_factor_batches: set[str] = set()
    for (raw_detail,) in remaining_details:
        if not raw_detail:
            continue
        try:
            payload = json.loads(raw_detail)
        except (TypeError, ValueError):
            continue
        dynamic = payload.get("_dynamic_model") or payload
        batch_id = dynamic.get("factor_calc_batch_id") if isinstance(dynamic, dict) else None
        if batch_id:
            protected_factor_batches.add(str(batch_id))
    return {
        "score_rows_deleted": len(candidates),
        "protected_factor_batch_ids": sorted(protected_factor_batches),
    }


class FactorPipelineBindingError(ValueError):
    """A public training request lacks a release-qualified FactorSet."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        readiness: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.readiness = readiness or {}

    def to_dict(self) -> dict:
        return {
            'error_code': self.code,
            'message': str(self),
            'factor_set_readiness': self.readiness,
        }


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


def _touch_task_heartbeat(task_id: str) -> bool:
    """Refresh updated_at for a running task using a short DB session."""
    SessionLocal = get_session_local()
    heartbeat_db = SessionLocal()
    try:
        task = heartbeat_db.get(AsyncTaskRecord, task_id)
        if task is None or task.status != 'running':
            return False
        task.updated_at = _now()
        heartbeat_db.commit()
        return True
    finally:
        heartbeat_db.close()


def _start_task_heartbeat(
    task_id: str,
) -> tuple[threading.Event, threading.Thread]:
    """Keep long DuckDB calculations visible without holding a SQL session."""
    stop_event = threading.Event()

    def _beat() -> None:
        while not stop_event.wait(TASK_HEARTBEAT_SECONDS):
            try:
                if not _touch_task_heartbeat(task_id):
                    return
            except Exception:
                logger.exception(
                    'Failed to heartbeat factor pipeline task %s',
                    task_id,
                )

    thread = threading.Thread(
        target=_beat,
        name=f'factor-heartbeat-{task_id[:8]}',
        daemon=True,
    )
    thread.start()
    # 契约：必须返回 (stop_event, thread)，否则调用方
    # `heartbeat_stop, heartbeat_thread = _start_task_heartbeat(...)`
    # 解包 None 会抛 `cannot unpack non-iterable NoneType object`
    return stop_event, thread

# mirror 阶段子阶段的 percent 映射
# metadata: 5% → 8%（通常很快）
# universe_bars: 8% → 18%
# business_bars: 18% → 25%
_MIRROR_PHASE_PERCENT = {
    'metadata': (5.0, 8.0),
    'universe_bars': (8.0, 18.0),
    'business_bars': (18.0, 25.0),
}


def _make_mirror_progress_callback(task_id: str):
    """构造 mirror 阶段的进度回调。

    使用独立 session 调用 _set_task，避免影响主 mirror 事务。
    callback 签名：(phase, processed, total, message)
    """
    def _callback(phase: str, processed: int, total: int, message: str) -> None:
        start_pct, end_pct = _MIRROR_PHASE_PERCENT.get(phase, (5.0, 25.0))
        ratio = min(1.0, processed / total) if total > 0 else 0.0
        percent = round(start_pct + (end_pct - start_pct) * ratio, 1)
        # 独立 session：主 session 在 mirror 事务中，commit 会提前结束事务
        SessionLocal = get_session_local()
        progress_db = SessionLocal()
        try:
            _set_task(
                progress_db,
                task_id,
                total=total,
                processed=processed,
                percent=percent,
                message=message,
            )
        except Exception:
            logger.exception(
                'Failed to update mirror progress for task %s', task_id
            )
        finally:
            progress_db.close()
    return _callback


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


def get_pipeline_eta(
    *,
    train_model: bool = True,
    full_refresh: bool = False,
) -> dict:
    """基于最近已完成的 factor_pipeline 任务统计预估总耗时。

    返回字段：
      avg_seconds / median_seconds: 历史 done 任务的平均/中位数总耗时
      sample_count: 样本数
      fallback_seconds: 样本不足时使用的经验默认值
      recommended_seconds: 实际推荐的预估时长（样本 >=3 用中位数，否则用 fallback）
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        rows = db.execute(
            select(
                AsyncTaskRecord.started_at,
                AsyncTaskRecord.finished_at,
                AsyncTaskRecord.payload_json,
            )
            .where(
                AsyncTaskRecord.task_type == TASK_TYPE,
                AsyncTaskRecord.status == 'done',
                AsyncTaskRecord.started_at.isnot(None),
                AsyncTaskRecord.finished_at.isnot(None),
            )
            .order_by(AsyncTaskRecord.finished_at.desc())
            .limit(50)
        ).all()
    finally:
        db.close()

    durations: list[float] = []
    for started, finished, payload_json in rows:
        if not started or not finished:
            continue
        try:
            payload = json.loads(payload_json or '{}')
        except (TypeError, json.JSONDecodeError):
            continue
        if bool(payload.get('train_model', True)) != train_model:
            continue
        if bool(payload.get('full_refresh', False)) != full_refresh:
            continue
        delta = (finished - started).total_seconds()
        if delta > 0:
            durations.append(delta)
        if len(durations) >= 10:
            break

    sample_count = len(durations)
    avg_seconds = sum(durations) / sample_count if durations else 0.0
    med_seconds = float(median(durations)) if durations else 0.0
    fallback = float(DEFAULT_ETA_SECONDS[(train_model, full_refresh)])
    # 样本 >=3 用中位数（抗异常值），否则用对应运行模式的经验默认值
    recommended = med_seconds if sample_count >= 3 else fallback
    return {
        'avg_seconds': round(avg_seconds, 1),
        'median_seconds': round(med_seconds, 1),
        'sample_count': sample_count,
        'fallback_seconds': fallback,
        'recommended_seconds': round(recommended, 1),
        'train_model': train_model,
        'full_refresh': full_refresh,
    }


def _require_ready_training_factor_set(payload: FactorPipelineCreate) -> None:
    """Fail before queueing when a public training job lacks immutable lineage."""
    if not payload.train_model:
        return

    factor_set_id = (payload.factor_set_id or '').strip()
    if not factor_set_id:
        raise FactorPipelineBindingError(
            'FACTOR_SET_REQUIRED',
            'train_model=true requires a frozen, readiness-qualified factor_set_id',
        )

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        readiness = factor_set_readiness(
            db,
            factor_set_id,
            require_frozen=True,
        )
    finally:
        db.close()

    if not readiness['ready']:
        raise FactorPipelineBindingError(
            f"FACTOR_SET_{readiness['code']}",
            readiness['message'],
            readiness=readiness,
        )

    # Persist the canonical non-whitespace ID used for the readiness check.
    payload.factor_set_id = factor_set_id


def create_factor_pipeline_task(payload: FactorPipelineCreate) -> dict:
    factor_config = get_current_factor_system_config()
    if not factor_config.feature_enabled:
        raise ValueError(
            'Factor pipeline is disabled; enable it in Settings > Factor Models'
        )
    _require_ready_training_factor_set(payload)
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {'queued', 'running'}:
        return existing[0].model_dump()
    task = create_async_task(TASK_TYPE, payload.model_dump())
    _start_worker(task.id, _run_factor_pipeline)
    return task.model_dump()


def _classify_pipeline_error(exc: Exception, stage: str):
    """将异常映射到统一错误协议 error_code，返回 (error_code, override_user_message)。

    避免向用户裸露 `NoneType`/原始异常文本，统一返回中文可操作文案。
    """
    msg = str(exc) or type(exc).__name__
    lower = msg.lower()
    # 参数/配置校验类（不可重试）
    if isinstance(exc, (ValueError,)):
        if 'disabled' in lower or 'enable' in lower:
            return (
                'FACTOR_PIPELINE_DISABLED',
                '因子模型未启用，请在「设置 → 因子模型」中开启后再运行流水线',
            )
        if 'validation_days' in lower or 'window_days' in lower:
            return (
                'VALIDATION_ERROR',
                '因子流水线启动失败：训练/验证窗口参数不合法，请调整后重试',
            )
        return (
            'VALIDATION_ERROR',
            '因子流水线启动失败：参数校验未通过，请检查配置后重试',
        )
    # DuckDB 锁/连接类（可重试）
    if (
        'duckdb' in lower
        or 'lock' in lower
        or 'concurrent' in lower
        or 'different configuration' in lower
        or ('database' in lower and ('lock' in lower or 'busy' in lower))
    ):
        return (
            'DB_LOCK_TIMEOUT',
            '因子仓库被占用或繁忙，请稍后重试；若持续失败可重启后端释放残留连接',
        )
    # 通用兜底（可重试）
    return (
        'UNKNOWN_ERROR',
        f'因子流水线在「{stage}」阶段失败，请稍后重试；若持续失败请查看技术详情',
    )


def _run_factor_pipeline(task_id: str) -> None:
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    SessionLocal = get_session_local()
    db = SessionLocal()
    current_stage = 'initializing'
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None:
            return
        if task.status == 'cancelled':
            return
        # 入口立即更新状态：让前端看到 worker 已启动，避免"提交后没动静"的错觉
        _set_task(
            db,
            task_id,
            status='running',
            stage='initializing',
            percent=2,
            message='Initializing warehouse and resolving date range',
            started_at=_now(),
        )
        heartbeat_stop, heartbeat_thread = _start_task_heartbeat(
            task_id
        )
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
        current_stage = 'mirror'
        # Release the configuration-read transaction before long local
        # warehouse writes. All bar reads below use short sessions, so MySQL
        # connections are returned to the pool between DuckDB batches.
        db.rollback()
        bars = mirror_daily_bars(
            db,
            warehouse=warehouse,
            start_date=effective_start_date,
            end_date=effective_end_date,
            full_refresh=payload.full_refresh,
            should_cancel=should_cancel,
            progress_callback=_make_mirror_progress_callback(task_id),
        )
        if should_cancel():
            return
        # daily bars mirror 完成，进入 factor inputs mirror 阶段
        # 更新 message 和 percent，避免用户看到进度停滞
        _set_task(
            db,
            task_id,
            stage='mirror',
            percent=27,
            message='Mirroring factor inputs (valuations, financials, flows)',
        )
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
        current_stage = 'factors'
        # WPD-06: wrap factor calculation in a batch audit context. The
        # calc_batch_id is pre-generated so ingestion_batches and
        # factor_values share the same identifier for atomicity checks.
        # batch_context swallows begin/finalize errors so it never blocks
        # the main pipeline.
        factor_calc_batch_id = f'factors-{uuid4().hex}'
        with batch_context(
            warehouse,
            batch_id=factor_calc_batch_id,
            source_key='factor.calculation',
            scope={
                'start_date': calculation_start_date.isoformat(),
                'end_date': effective_end_date.isoformat(),
            },
        ):
            factors = calculate_stock_factors(
                warehouse,
                start_date=calculation_start_date,
                end_date=effective_end_date,
                calc_batch_id=factor_calc_batch_id,
            )
        results['factors'] = asdict(factors)
        if should_cancel():
            return

        try:
            db.close()
        except Exception:
            logger.warning(
                'Failed to close stale database session for factor pipeline %s',
                task_id,
                exc_info=True,
            )
        db = SessionLocal()
        _set_task(
            db,
            task_id,
            stage='targets',
            percent=55,
            message='Generating T+1 to T+5 labels',
        )
        current_stage = 'targets'
        target_calc_batch_id = f'targets-{uuid4().hex}'
        with batch_context(
            warehouse,
            batch_id=target_calc_batch_id,
            source_key='target.generation',
            scope={
                'start_date': calculation_start_date.isoformat(),
                'end_date': effective_end_date.isoformat(),
            },
        ):
            targets = calculate_targets(
                warehouse,
                start_date=calculation_start_date,
                end_date=effective_end_date,
                calc_batch_id=target_calc_batch_id,
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
            current_stage = 'train'
            model_result = train_rolling_ridge(
                db,
                warehouse,
                factor_calc_batch_id=factors.calc_batch_id,
                target_calc_batch_id=targets.calc_batch_id,
                factor_set_id=getattr(payload, 'factor_set_id', None),
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
            current_stage = 'score'
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
        # Retain a bounded calculation history so repeated score refreshes do
        # not grow the analytical warehouse without limit. Audit metadata and
        # SQLite Score rows remain intact for decision/evidence traceability.
        try:
            score_retention = _prune_unreferenced_scores(db)
            db.commit()
            results['batch_retention'] = warehouse.prune_calculation_batches(
                protected_batch_ids=score_retention.get('protected_factor_batch_ids', []),
            )
            results['batch_retention']['score_rows_deleted'] = score_retention['score_rows_deleted']
        except Exception as cleanup_exc:
            logger.warning(
                'Factor batch retention cleanup skipped for %s: %s',
                task_id,
                cleanup_exc,
                exc_info=True,
            )
            results['batch_retention'] = {
                'status': 'SKIPPED',
                'reason': str(cleanup_exc),
            }
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
        try:
            db.rollback()
        except Exception:
            logger.warning(
                'Failed to roll back database session for factor pipeline %s',
                task_id,
                exc_info=True,
            )
        logger.exception('Factor pipeline task %s failed', task_id)
        failure_db = SessionLocal()
        try:
            # 终态保护：若任务已被取消，不覆盖为 failed（硬约束：
            # cancelled 终态不得被 worker 线程覆盖）
            existing = failure_db.get(AsyncTaskRecord, task_id)
            if existing is not None and existing.status == 'cancelled':
                return
            # 统一错误协议：避免裸露 NoneType/原始异常文本，返回中文可操作文案
            error_code, override_message = _classify_pipeline_error(
                exc, current_stage
            )
            user_error = build_user_error(
                error_code,
                override_user_message=override_message,
                technical_details=TechnicalDetails(
                    exception_type=type(exc).__name__,
                    error_message=str(exc),
                ),
            )
            _set_task(
                failure_db,
                task_id,
                status='failed',
                stage='failed',
                message=user_error.user_message,
                errors_json=json.dumps(
                    [user_error.model_dump(mode='json')],
                    ensure_ascii=False,
                    default=str,
                ),
                finished_at=_now(),
            )
        except Exception:
            logger.exception('Failed to mark factor pipeline task failed')
        finally:
            failure_db.close()
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        db.close()


# ---------------------------------------------------------------------------
# WPD-03: stale task recovery and lock diagnostics
# ---------------------------------------------------------------------------


def recover_stale_pipeline_tasks(db_session) -> list[dict]:
    """Recover zombie ``factor_pipeline`` tasks on service startup.

    Scans ``status='running'`` tasks whose heartbeat has expired and
    marks them ``failed`` so they no longer block single-flight
    scheduling.  Also releases the warehouse lock left behind by the
    dead process (best-effort).

    Should be called from the FastAPI startup hook (see ``app.main``).
    """
    from app.services.factors.warehouse_locks import recover_stale_tasks

    return recover_stale_tasks(
        db_session,
        task_types=('factor_pipeline',),
    )


def diagnose_pipeline_lock_state() -> dict:
    """Diagnose the current pipeline lock and task state.

    Returns a JSON-serialisable dict with:

    * ``active_running_tasks`` — running factor_pipeline tasks with
      heartbeat age.
    * ``warehouse_lock`` — :class:`WarehouseLockInfo` serialised.
    * ``stale_tasks`` — stale factor_pipeline task diagnostics.
    * ``recovery_recommendation`` — ``'ok'`` / ``'recover_stale'`` /
      ``'force_release_lock'``.
    """
    from sqlalchemy import select

    from app.services.factors.warehouse_locks import (
        diagnose_stale_tasks,
        diagnose_warehouse_lock,
    )

    # Resolve warehouse path from config.
    try:
        config = get_current_factor_system_config()
        warehouse_path = Path(config.warehouse_path)
    except Exception:
        return {
            'active_running_tasks': [],
            'warehouse_lock': None,
            'stale_tasks': [],
            'recovery_recommendation': 'ok',
            'error': 'config_unavailable',
        }

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        now = _now()
        stmt = select(AsyncTaskRecord).where(
            AsyncTaskRecord.task_type == TASK_TYPE,
            AsyncTaskRecord.status == 'running',
        )
        running_tasks = db.execute(stmt).scalars().all()
        active_running_tasks: list[dict] = []
        for t in running_tasks:
            last_alive = (
                t.heartbeat_at or t.updated_at or t.started_at or t.created_at
            )
            age = (
                (now - last_alive).total_seconds()
                if last_alive is not None
                else None
            )
            active_running_tasks.append(
                {
                    'task_id': t.id,
                    'status': t.status,
                    'heartbeat_at': (
                        t.heartbeat_at.isoformat()
                        if t.heartbeat_at
                        else None
                    ),
                    'age_seconds': age,
                }
            )

        # Diagnose stale tasks, then filter to factor_pipeline.
        all_stale = diagnose_stale_tasks(db)
        stale_ids = {s.task_id for s in all_stale if s.is_stale}
        stale_pipeline: list[dict] = []
        if stale_ids:
            type_stmt = select(
                AsyncTaskRecord.id, AsyncTaskRecord.task_type
            ).where(AsyncTaskRecord.id.in_(stale_ids))
            type_map = dict(db.execute(type_stmt).all())
            for diag in all_stale:
                if (
                    diag.is_stale
                    and type_map.get(diag.task_id) == TASK_TYPE
                ):
                    stale_pipeline.append(
                        {
                            'task_id': diag.task_id,
                            'is_stale': diag.is_stale,
                            'stale_reason': diag.stale_reason,
                            'heartbeat_at': (
                                diag.heartbeat_at.isoformat()
                                if diag.heartbeat_at
                                else None
                            ),
                            'stage_started_at': (
                                diag.stage_started_at.isoformat()
                                if diag.stage_started_at
                                else None
                            ),
                            'stage_budget_seconds': diag.stage_budget_seconds,
                        }
                    )

        # Warehouse lock diagnosis.
        lock_info = diagnose_warehouse_lock(warehouse_path)

        # Recommendation.
        if stale_pipeline:
            recommendation = 'recover_stale'
        elif lock_info.is_locked:
            recommendation = 'force_release_lock'
        else:
            recommendation = 'ok'

        return {
            'active_running_tasks': active_running_tasks,
            'warehouse_lock': {
                'path': lock_info.path,
                'is_locked': lock_info.is_locked,
                'lock_owner_pid': lock_info.lock_owner_pid,
                'lock_owner_process': lock_info.lock_owner_process,
                'lock_age_seconds': lock_info.lock_age_seconds,
                'diagnostic_method': lock_info.diagnostic_method,
                'notes': list(lock_info.notes),
            },
            'stale_tasks': stale_pipeline,
            'recovery_recommendation': recommendation,
        }
    finally:
        db.close()


__all__ = [
    'TASK_TYPE',
    'create_factor_pipeline_task',
    'diagnose_pipeline_lock_state',
    'get_pipeline_eta',
    'recover_stale_pipeline_tasks',
    'resolve_calculation_start',
    'resolve_pipeline_dates',
]
