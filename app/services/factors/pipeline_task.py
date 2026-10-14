'''Cancellable local factor pipeline backed by the existing async task table.'''
from __future__ import annotations

import json
import logging
from dataclasses import asdict
import threading
from datetime import date, datetime, timedelta, timezone
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
    FactorSevenError,
    factor_structured_error,
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
from app.services.factors.runtime import _snapshot
from app.services.factors.scoring_bridge import materialize_factor_scores, materialize_factor_score_window
from app.services.factors.store import FactorWarehouse, SHARED_TARGET_BATCH_ID
from app.models.score import Score
from app.models.factor_model import FactorModelRun
from app.models.factor_runtime import FactorRuntimeState
from app.models.factor_evaluation import FactorSet
from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.models.decision_engine import DecisionEvidence
from app.models.journal_entry import JournalEntry
from app.models.trade_setup import TradeSetup
from app.services.factors.target_engine import calculate_targets


logger = logging.getLogger(__name__)
TASK_TYPE = 'factor_pipeline'
_TASK_DISPATCH_LOCK = threading.Lock()


def _expected_runtime_identity(payload: FactorPipelineCreate):
    hp = payload.hyperparameters_json or {}
    if not ({'expected_model_run_id', 'expected_weight_mode'} & hp.keys()):
        return None
    model_id = hp.get('expected_model_run_id')
    mode = hp.get('expected_weight_mode')
    if not isinstance(model_id, str) or not model_id.strip() or mode not in {'ridge', 'shadow'}:
        raise factor_structured_error(
            'MODEL_RUNTIME_EXPECTATION_INVALID', title_zh='评分任务缺少模型身份',
            detail_zh='请同时指定 expected_model_run_id 和 expected_weight_mode（ridge 或 shadow），然后重新提交评分。',
            impact='未创建或发布任何模型评分。', fix_link='/settings/factor-center', retryable=False,
        )
    return model_id.strip(), mode


def _validate_requested_runtime(db, payload: FactorPipelineCreate):
    # A locking/current read bypasses both the ORM identity cache and MySQL's
    # repeatable-read snapshot. During score publication this row lock is held
    # until commit, so activation cannot race the publication transaction.
    state = db.execute(
        select(FactorRuntimeState).where(FactorRuntimeState.id == 1)
        .with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    runtime = _snapshot(state)
    expected = _expected_runtime_identity(payload)
    if expected is not None and expected != (runtime.active_model_run_id, runtime.weight_mode):
        raise factor_structured_error(
            'MODEL_RUNTIME_CHANGED', title_zh='活动模型已变更，本次评分已中止',
            detail_zh='评分任务指定的模型或运行模式与当前活动模型不一致。请查看当前模型，再重新生成评分。',
            impact='本次任务未发布评分；已有合法历史评分保留。',
            fix_link='/settings/factor-center', retryable=True,
            extras={'expected_model_run_id': expected[0], 'expected_weight_mode': expected[1],
                    'actual_model_run_id': runtime.active_model_run_id, 'actual_weight_mode': runtime.weight_mode},
        )
    return runtime


def _pin_scoring_runtime(payload: FactorPipelineCreate, runtime) -> None:
    # Scheduled/legacy scoring callers may omit an expectation. Bind their
    # request to the model observed at dispatch instead of following later
    # activations. Training never inherits the old active model as its output.
    if (payload.materialize_scores and not payload.train_model
            and _expected_runtime_identity(payload) is None
            and runtime.active_model_run_id and runtime.weight_mode in {'ridge', 'shadow'}):
        payload.hyperparameters_json = {
            **(payload.hyperparameters_json or {}),
            'expected_model_run_id': runtime.active_model_run_id,
            'expected_weight_mode': runtime.weight_mode,
        }


def _pipeline_request_intent(payload: FactorPipelineCreate) -> dict:
    # Actor/source describe who requested the task, not what it calculates.
    # Every calculation/training/publication parameter remains in the intent.
    intent = payload.model_dump(mode='json', exclude={'actor', 'source_hint'})
    for key in ('hyperparameters_json', 'factor_scope_json'):
        intent[key] = intent.get(key) or {}
    return intent


def _complete_calculation_dates(db, warehouse, *, start_date: date, end_date: date,
                                threshold: float = 0.90, as_of: datetime | None = None) -> dict:
    """Compare the frozen mirror with both its history and the live local source.

    Never assume MAX(trade_date) is a complete cross section. A source update
    during mirroring is detectable because the mirror must also cover the
    source's current symbol count. This check is read-only and fails closed.
    """
    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute("""
            SELECT trade_date, COUNT(DISTINCT symbol)
            FROM raw_daily_bars WHERE adjust = 'qfq'
              AND trade_date >= ? AND trade_date <= ?
            GROUP BY trade_date ORDER BY trade_date
        """, [start_date - timedelta(days=45), end_date]).fetchall()
    source_rows = db.execute(
        select(UniverseDailyBar.trade_date,
               func.count(UniverseDailyBar.universe_symbol_id.distinct()))
        .join(UniverseSymbol, UniverseSymbol.id == UniverseDailyBar.universe_symbol_id)
        .where(UniverseDailyBar.trade_date >= start_date - timedelta(days=45),
               UniverseDailyBar.trade_date <= end_date,
               UniverseSymbol.asset_type == 'stock', UniverseSymbol.region == 'cn')
        .group_by(UniverseDailyBar.trade_date)
    ).all()
    source_counts = dict(source_rows)
    universe_count = int(db.scalar(select(func.count(UniverseSymbol.id)).where(
        UniverseSymbol.asset_type == 'stock', UniverseSymbol.region == 'cn',
    )) or 0)
    counts = dict(rows)
    accepted, skipped = [], []
    exchange_now = (as_of or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    for day, observed in rows:
        if day < start_date:
            continue
        prior = [count for prior_day, count in rows if prior_day < day][-20:]
        # A bootstrap still needs an independently observed universe/source
        # count; the largest mirror cross section prevents a tiny last day
        # from redefining its own baseline.
        baseline = int(median(prior)) if prior else max(counts.values(), default=0)
        expected = max(baseline, int(source_counts.get(day, 0)))
        if len(prior) < 3 and day >= exchange_now.date() - timedelta(days=45):
            expected = max(expected, universe_count)
        ratio = observed / expected if expected else 0.0
        closed = day < exchange_now.date() or (
            day == exchange_now.date() and (exchange_now.hour, exchange_now.minute) >= (15, 15)
        )
        if expected and ratio >= threshold and closed:
            accepted.append(day)
        else:
            skipped.append({'trade_date': day, 'observed_symbols': observed,
                            'expected_symbols': expected, 'coverage': ratio,
                            'reason': ('INCOMPLETE_DAILY_BAR_CROSS_SECTION' if closed
                                       else 'DAILY_BAR_NOT_CLOSED')})
    return {'accepted_dates': accepted, 'skipped_dates': skipped,
            'selected_end_date': accepted[-1] if accepted else None,
            'minimum_bar_coverage': threshold}


def _qualified_factor_dates(warehouse, *, batch_id: str,
                            expected_versions: dict[str, int],
                            minimum_coverage: float = 0.70) -> dict:
    with warehouse.connection(read_only=True) as conn:
        totals = dict(conn.execute("""
            SELECT trade_date, COUNT(DISTINCT symbol) FROM raw_daily_bars
            WHERE adjust = 'qfq' GROUP BY trade_date
        """).fetchall())
        rows = conn.execute("""
            SELECT trade_date, factor_code, factor_version,
                   COUNT(DISTINCT CASE WHEN eligible AND normalized_value IS NOT NULL
                                       AND isfinite(normalized_value) THEN symbol END)
            FROM factor_values WHERE calc_batch_id = ?
            GROUP BY trade_date, factor_code, factor_version ORDER BY trade_date
        """, [batch_id]).fetchall()
    per_day = {}
    for day, code, version, count in rows:
        if expected_versions.get(code) == version:
            per_day.setdefault(day, {})[code] = count / max(totals.get(day, 0), 1)
    accepted, skipped = [], []
    for day in sorted({row[0] for row in rows}):
        coverage = {code: per_day.get(day, {}).get(code, 0.0) for code in expected_versions}
        if coverage and min(coverage.values()) >= minimum_coverage:
            accepted.append(day)
        else:
            skipped.append({'trade_date': day, 'coverage_by_factor': coverage,
                            'reason': 'INSUFFICIENT_FACTOR_COVERAGE'})
    return {'accepted_dates': accepted, 'skipped_dates': skipped,
            'minimum_factor_coverage': minimum_coverage}

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


def retention_protected_batch_ids(db: Session, score_retention: dict) -> list[str]:
    """保留清理的保护集：Score 引用批次 ∪ 模型 identity 引用批次。

    单独成函数是因为"漏了 target 侧"这个 bug 就长在调用点的组装上，
    不抽出来就只能测到 helper、测不到真实保护集。
    """
    merged = {
        str(item) for item in score_retention.get("protected_factor_batch_ids", [])
        if item
    }
    merged |= _model_referenced_batch_ids(db)
    return sorted(merged)


def _model_referenced_batch_ids(db: Session) -> set[str]:
    """已训练模型在 MySQL 里持久引用的 duckdb 计算批次。

    `ridge_model` 把 identity（含 `factor_calc_batch_id` 与
    `target_calc_batch_id`）整包写进 `factor_model_runs.hyperparameters_json`，
    所以这些批次承担着"复现该模型当年所用标签"的职责。按数量淘汰若把它们删掉，
    重跑同一模型的评估会静默换成另一套标签——而 target 侧此前完全没进保护清单。
    """
    from app.models.factor_model import FactorModelRun

    batch_ids: set[str] = set()
    try:
        rows = db.execute(select(FactorModelRun.hyperparameters_json)).all()
    except Exception:
        # 旧库可能还没有该表/列；保护清单缺一项不能阻断保留清理本身
        logger.exception("读取模型引用批次失败，本轮按无引用处理")
        return batch_ids
    for (raw,) in rows:
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("factor_calc_batch_id", "target_calc_batch_id"):
            value = payload.get(key)
            if value:
                batch_ids.add(str(value))
    return batch_ids


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

# spec §需求 5 阶段映射：internal stage → 展示/错误协议 stage
#  1. 同步数据（mirror）        = data_sync
#  2. 因子预处理/计算（factors） = factor_preprocessing
#  3. 目标生成（targets）        = target_generation
#  4. 模型训练（train）          = training
#  5. 评分生成/消费（score）     = scoring
_PIPELINE_STAGE_MAP: dict[str, str] = {
    "initializing": "initializing",
    "mirror": "data_sync",
    "factors": "factor_preprocessing",
    "targets": "target_generation",
    "train": "training",
    "score": "scoring",
    "done": "done",
    "failed": "failed",
}


def _canonical_stage(raw: str) -> str:
    """把内部 stage 原始值映射到 §需求 5 阶段规范名；未知值透传小写。"""
    if not raw:
        return "unknown"
    return _PIPELINE_STAGE_MAP.get(raw, str(raw).lower())


# mirror 阶段子阶段的 percent 映射
# metadata: 5% → 8%（通常很快）
# universe_bars: 8% → 18%
# business_bars: 18% → 25%
# factor_inputs: 27% → 35%（VIZ-0930-26：这段原先没有回调，实测百分比能冻住 40 分钟）
_MIRROR_PHASE_PERCENT = {
    'metadata': (5.0, 8.0),
    'universe_bars': (8.0, 18.0),
    'business_bars': (18.0, 25.0),
    'factor_inputs': (27.0, 35.0),
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
    """Fail before queueing when a public training job lacks immutable lineage.

    Task 9 精确 5 错误码（BFG 7 要素结构化错误 FactorSevenError）：
      ① PIPELINE_NO_FACTOR_SET         — train=true 但 factor_set_id 空/未传
      ② PIPELINE_FACTOR_SET_NOT_FOUND  — factor_set_id 传了但 DB 无此行
      ③ PIPELINE_FACTOR_SET_NOT_FROZEN — 查到但 frozen_at 为空 / status != frozen
      ④ PIPELINE_FACTOR_SET_EMPTY      — 冻结但成员数 == 0
      ⑤ PIPELINE_FACTOR_SET_NO_FEATURE_FACTORS — 冻结有成员但 role=feature 数 == 0
      兜底 PIPELINE_FACTOR_SET_NOT_READY — 其他 readiness 未通过（member version not
        trainable / content hash 空 / lineage 不一致等）

    严格取消「如果没传，取第一个冻结集合」的 fallback：任何空值都抛 7 要素 400。
    """
    if not payload.train_model:
        return

    raw_id = getattr(payload, "factor_set_id", None)
    # 精确判定空值（None/''/'0'/空白串），不做任何 fallback 查"第一个冻结集合"
    is_empty = (
        raw_id is None
        or (isinstance(raw_id, str) and (raw_id.strip() in ("", "0") or len(raw_id.strip()) == 0))
        or (not isinstance(raw_id, str) and not raw_id)
    )
    if is_empty:
        raise factor_structured_error(
            "PIPELINE_NO_FACTOR_SET",
            title_zh="流水线未指定因子集合",
            detail_zh="训练模式需要显式选择一个已冻结因子集合。请前往 设置 → 选择集合 或 因子中心 冻结集合。",
            fix_link="/settings/factors",
            retryable=False,
        )

    factor_set_id = str(raw_id).strip()
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

    if readiness["ready"]:
        # 校验通过，持久化 canonical non-whitespace ID
        payload.factor_set_id = factor_set_id
        return

    raw_code = readiness.get("code", "UNKNOWN")
    details = readiness.get("details") or {}
    # Task 9 (2) 精确 5 错误码映射
    if raw_code == "NOT_FOUND":
        raise factor_structured_error(
            "PIPELINE_FACTOR_SET_NOT_FOUND",
            title_zh="因子集合不存在",
            detail_zh=f"流水线请求的 factor_set_id={factor_set_id!r} 在数据库中不存在。请前往因子中心查看可用的已冻结集合。",
            fix_link="/settings/factor-center",
            retryable=False,
            extras={"factor_set_id": factor_set_id, "readiness_details": details},
        )
    if raw_code == "NOT_FROZEN":
        raise factor_structured_error(
            "PIPELINE_FACTOR_SET_NOT_FROZEN",
            title_zh="因子集合未冻结",
            detail_zh=f"FactorSet(id={factor_set_id}) status={details.get('status', 'N/A')!r}，frozen_at 为空。训练模式必须使用已冻结集合。",
            fix_link="/settings/factor-center?action=freeze",
            retryable=True,
            extras={"factor_set_id": factor_set_id, "readiness_details": details},
        )
    if raw_code == "EMPTY_SET":
        raise factor_structured_error(
            "PIPELINE_FACTOR_SET_EMPTY",
            title_zh="因子集合为空",
            detail_zh=f"FactorSet(id={factor_set_id}) 已冻结但成员数为 0（空集合）。请先添加至少 1 个 feature 角色因子后重新冻结。",
            fix_link="/settings/factor-center?open=members",
            retryable=True,
            extras={"factor_set_id": factor_set_id, "readiness_details": details},
        )
    if raw_code == "NO_FEATURE_SET":
        raise factor_structured_error(
            "PIPELINE_FACTOR_SET_NO_FEATURE_FACTORS",
            title_zh="因子集合缺少 feature 角色因子",
            detail_zh=f"FactorSet(id={factor_set_id}) 已冻结但 role=feature 的成员数 = 0。请把至少 1 个成员角色改为 feature 后重试。",
            fix_link="/settings/factor-center?open=members",
            retryable=True,
            extras={"factor_set_id": factor_set_id, "readiness_details": details},
        )
    # 兜底：版本不可训练 / content hash 空 / 血缘不一致 等
    raise factor_structured_error(
        "PIPELINE_FACTOR_SET_NOT_READY",
        title_zh="因子集合 readiness 综合未通过",
        detail_zh=(
            f"FactorSet(id={factor_set_id}) 未通过 readiness 综合门禁（code={raw_code}）："
            f"{readiness.get('message', '未知原因')}。请按提示修复后重试。"
        ),
        fix_link="/settings/pipeline",
        retryable=True,
        extras={
            "factor_set_id": factor_set_id,
            "readiness_code": raw_code,
            "readiness_details": details,
        },
    )


def create_factor_pipeline_task(payload: FactorPipelineCreate) -> dict:
    # Serialize the existing single-worker contract within this process; two
    # simultaneous activations must not both pass the single-flight check.
    with _TASK_DISPATCH_LOCK:
        return _create_factor_pipeline_task(payload)


def _create_factor_pipeline_task(payload: FactorPipelineCreate) -> dict:
    factor_config = get_current_factor_system_config()
    if not factor_config.feature_enabled:
        raise ValueError(
            'Factor pipeline is disabled; enable it in Settings > Factor Models'
        )
    # Task 9 (1) 取消 fallback + (2) 精确 5 错误码 — 抛 FactorSevenError（7 要素）
    _require_ready_training_factor_set(payload)

    SessionLocal = get_session_local()
    runtime_db = SessionLocal()
    try:
        runtime = _validate_requested_runtime(runtime_db, payload)
        _pin_scoring_runtime(payload, runtime)
    finally:
        runtime_db.close()
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {'queued', 'running'}:
        try:
            old_payload = FactorPipelineCreate.model_validate_json(existing[0].payload_json or '{}')
            same_intent = _pipeline_request_intent(old_payload) == _pipeline_request_intent(payload)
        except (TypeError, ValueError):
            same_intent = False
        if same_intent:
            return existing[0].model_dump()
        raise factor_structured_error(
            'FACTOR_PIPELINE_BUSY', title_zh='已有其他因子任务正在运行',
            detail_zh='正在运行的任务与本次模型、集合或计算请求不同。本次请求未提交，请等现有任务完成后重试。',
            impact='模型激活状态保留；本次评分或训练任务未创建。',
            fix_link='/settings/factor-center', retryable=True,
            extras={'running_task_id': existing[0].id},
        )

    # ── Task 9 (3) ③ factor_audit_logs.attributes_json.factor_set_id 写入 ──
    # 仅在 train=true 且 factor_set_id 有值时写启动审计
    if getattr(payload, "train_model", False) and getattr(payload, "factor_set_id", None):
        actor = getattr(payload, "actor", "settings:pipeline")
        fsid = str(payload.factor_set_id)
        SessionLocal = get_session_local()
        audit_db = SessionLocal()
        try:
            from app.models.factor_runtime import FactorAuditLog
            payload_dict = payload.model_dump()
            attributes = {
                "factor_set_id": fsid,
                "action": "start_pipeline_with_factorset",
                "train_model": True,
                "hyperparameters": {
                    "factor_set_id": fsid,
                    "window_days": payload_dict.get("window_days"),
                    "validation_days": payload_dict.get("validation_days"),
                },
                "factor_scope": {"factor_set_id": fsid},
            }
            audit_db.add(FactorAuditLog(
                action="start_pipeline_with_factorset",
                factor_set_id=fsid,
                model_run_id=None,
                actor=actor,
                before_json=json.dumps({"pipeline": "not_started"}, ensure_ascii=False),
                after_json=json.dumps({"pipeline": "queued", "factor_set_id": fsid}, ensure_ascii=False),
                attributes_json=json.dumps(attributes, ensure_ascii=False),
            ))
            audit_db.commit()
        except Exception as _ae:
            # 审计写入失败不阻塞流水线启动（best-effort，避免因为审计表 schema 问题导致主流程卡死）
            logger.warning(
                "start_pipeline factor_audit_logs best-effort write failed: %s",
                _ae,
            )
            try:
                audit_db.rollback()
            except Exception:
                pass
        finally:
            audit_db.close()

    # force_new：本入口是用户显式“启动流水线”动作，并发重复提交已由上面的
    # 单飞（queued/running 回读）拦住；如果不传 force_new，AC-10 幂等回读会
    # 把“上一轮已 done”的历史任务直接返回，前端看到一条旧任务而 worker 根本不
    # 启动（第二次点击被静默吞掉）。同族的 mirror_task / mining validation /
    # factor_evaluation 均已用 force_new=True 表达同一语义。
    task = create_async_task(TASK_TYPE, payload.model_dump(), force_new=True)
    _start_worker(task.id, _run_factor_pipeline)
    return task.model_dump()


def _classify_pipeline_error(exc: Exception, stage: str):
    """将异常映射到统一错误协议 error_code，返回 (error_code, override_user_message)。

    避免向用户裸露 `NoneType`/原始异常文本，统一返回中文可操作文案。
    """
    msg = str(exc) or type(exc).__name__
    lower = msg.lower()
    if isinstance(exc, FactorSevenError):
        return exc.error_code, exc.detail_zh
    # 参数/配置校验类（不可重试）
    if isinstance(exc, (ValueError,)):
        if 'no_complete_trade_date' in lower:
            return ('DAILY_BAR_INCOMPLETE', '行情尚未收盘或镜像未覆盖完整交易日，请等待本地行情同步完成后重试')
        if 'factor_coverage' in lower:
            return ('FACTOR_COVERAGE_INSUFFICIENT', '冻结集合的有效因子覆盖不足，请补齐该版本所需数据后重试；本次未发布评分')
        if 'no_factor_scores_published' in lower:
            return ('FACTOR_SCORE_INPUT_MISSING', '本窗口没有可发布的模型评分，请检查冻结版本的输入覆盖和业务标的映射')
        if 'frozen_factor' in lower or 'factor_value_version_mismatch' in lower:
            return ('FACTOR_VERSION_INPUT_INVALID', '冻结因子版本的公式、输入或计算结果不匹配，请修复版本定义后重新计算')
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
        starting_runtime = _validate_requested_runtime(db, payload)
        _pin_scoring_runtime(payload, starting_runtime)
        if payload.train_model and (payload.factor_scope_json or {}).get('asset_type', 'stock') != 'stock':
            raise ValueError('unsupported_training_asset_type:stock_only')
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
            # 7 类输入逐段上报，percent 在 27→35 之间插值
            progress_callback=_make_mirror_progress_callback(task_id),
        )
        results['bar_mirror'] = bars.to_dict()
        results['input_mirror'] = {
            **asdict(inputs),
            'rows_written': inputs.rows_written,
        }
        if should_cancel():
            return

        current_stage = 'factors'
        # End any repeatable-read snapshot opened while mirroring factor
        # inputs; completeness must observe source rows committed meanwhile.
        db.rollback()
        completeness = _complete_calculation_dates(
            db, warehouse, start_date=calculation_start_date,
            end_date=effective_end_date,
        )
        results['data_completeness'] = completeness
        if not completeness['accepted_dates']:
            raise ValueError('no_complete_trade_date_after_mirror')
        effective_end_date = completeness['selected_end_date']
        results['effective_range']['end_date'] = effective_end_date
        runtime_before_calculation = _validate_requested_runtime(db, payload)
        _pin_scoring_runtime(payload, runtime_before_calculation)
        calculation_set_ids = [payload.factor_set_id] if payload.factor_set_id else []
        if not payload.train_model and runtime_before_calculation.active_model_run_id:
            active_model = db.get(FactorModelRun, runtime_before_calculation.active_model_run_id)
            active_set = json.loads(active_model.hyperparameters_json or '{}').get('factor_set_id') if active_model else None
            if active_set:
                calculation_set_ids.append(active_set)

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
        ) as factor_batch_audit:
            factors = calculate_stock_factors(
                warehouse,
                start_date=calculation_start_date,
                end_date=effective_end_date,
                calc_batch_id=factor_calc_batch_id,
                db=db,
                factor_set_ids=tuple(dict.fromkeys(calculation_set_ids)),
                trade_dates=tuple(completeness['accepted_dates']),
            )
            # 审计行数只是记账，不许把计算阶段带崩：字段缺失时留 0
            factor_batch_audit.rows_written = getattr(factors, 'rows_written', 0)
            factor_batch_audit.rows_received = getattr(factors, 'rows_written', 0)
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
        # 常驻共享批次：本次只刷新 [calculation_start_date, effective_end_date]
        # 这一段（含尾部 horizon 天），历史行的覆盖情况以覆盖表为准，不看这里的
        # scope——scope 记的是"这次刷到哪"，不是"这个批次覆盖哪"。
        target_calc_batch_id = SHARED_TARGET_BATCH_ID
        with batch_context(
            warehouse,
            batch_id=target_calc_batch_id,
            source_key='target.generation',
            scope={
                'start_date': calculation_start_date.isoformat(),
                'end_date': effective_end_date.isoformat(),
                'shared_batch': True,
            },
        ) as target_batch_audit:
            targets = calculate_targets(
                warehouse,
                start_date=calculation_start_date,
                end_date=effective_end_date,
                calc_batch_id=target_calc_batch_id,
            )
            target_batch_audit.rows_written = getattr(targets, 'rows_written', 0)
            target_batch_audit.rows_received = getattr(targets, 'rows_written', 0)
        results['targets'] = asdict(targets)
        if should_cancel():
            return

        model_result = None
        if payload.train_model:
            current_stage = 'train'
            training_dates = None
            if payload.factor_set_id:
                training_set = db.get(FactorSet, payload.factor_set_id)
                expected = {m.factor_code: m.factor_version for m in training_set.members if m.role == 'feature'}
                training_coverage = _qualified_factor_dates(
                    warehouse, batch_id=factors.calc_batch_id, expected_versions=expected,
                )
                results['training_coverage'] = training_coverage
                if not training_coverage['accepted_dates']:
                    raise ValueError('insufficient_frozen_factor_coverage_for_training')
                training_dates = tuple(training_coverage['accepted_dates'])
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
                allowed_trade_dates=training_dates,
                hyperparameters={**(payload.hyperparameters_json or {}),
                                 'actor': payload.actor, 'source_hint': payload.source_hint},
            )
            db.commit()
            results['model'] = asdict(model_result)
        if should_cancel():
            return

        runtime = _validate_requested_runtime(db, payload)
        _pin_scoring_runtime(payload, runtime)
        if (
            payload.materialize_scores
            and not payload.train_model
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
            # _set_task commits progress and releases earlier locks. Re-read
            # the expectation now, retaining this current-read row lock until
            # the scores and their lineage have committed together.
            runtime = _validate_requested_runtime(db, payload)
            scoring_model = db.get(FactorModelRun, runtime.active_model_run_id)
            from app.services.factor_model_contract import assess_factor_model_readiness
            readiness = assess_factor_model_readiness(db, model_run_id=runtime.active_model_run_id,
                                                      require_runtime_active=True)
            if not readiness.ready:
                raise ValueError(f'{readiness.code}:{readiness.message}')
            expected = {member.factor_code: int(member.factor_version) for member in scoring_model.weights}
            scoring_coverage = _qualified_factor_dates(
                warehouse, batch_id=factors.calc_batch_id,
                expected_versions={code: int(version) for code, version in expected.items()},
            )
            results['scoring_coverage'] = scoring_coverage
            results['scoring_bridge'] = materialize_factor_score_window(
                db, warehouse, trade_dates=scoring_coverage['accepted_dates'],
                factor_calc_batch_id=factors.calc_batch_id,
                model_run_id=runtime.active_model_run_id, mode=runtime.weight_mode,
                pipeline_run_id=task_id,
                publication_guard=lambda: _validate_requested_runtime(db, payload),
            )
            _validate_requested_runtime(db, payload)
            db.commit()
        elif payload.materialize_scores:
            results['scoring_bridge'] = {
                'status': 'SKIPPED',
                'reason': ('TRAINED_MODEL_REQUIRES_EXPLICIT_ACTIVATION' if payload.train_model
                           else 'NO_ACTIVE_FACTOR_MODEL'),
                'mode': runtime.weight_mode, 'materialized_count': 0,
                'model_run_id': model_result.model_run_id if model_result else None,
                'next_action': 'Review and explicitly activate a validated real-data model before requesting scores',
            }
        # Retain a bounded calculation history so repeated score refreshes do
        # not grow the analytical warehouse without limit. Audit metadata and
        # SQLite Score rows remain intact for decision/evidence traceability.
        try:
            score_retention = _prune_unreferenced_scores(db)
            db.commit()
            protected = retention_protected_batch_ids(db, score_retention)
            results['batch_retention'] = warehouse.prune_calculation_batches(
                protected_batch_ids=protected,
            )
            results['batch_retention']['protected_batch_ids'] = protected
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
        scores_skipped = results.get('scoring_bridge', {}).get('status') == 'SKIPPED'
        scores_partial = results.get('scoring_bridge', {}).get('status') == 'PARTIAL'
        training_rejected = model_result is not None and model_result.status != 'validated'
        completion_status = ('TRAINING_REJECTED' if training_rejected else
                             'AWAITING_MODEL_ACTIVATION' if scores_skipped or model_result is not None else
                             'PARTIAL_SCORE_PUBLICATION' if scores_partial else 'COMPLETED')
        results['completion_status'] = completion_status
        completion_message = {
            'TRAINING_REJECTED': 'Factor calculation completed; training candidate rejected by data/model gates',
            'AWAITING_MODEL_ACTIVATION': 'Factor calculation completed; scores skipped until explicit model activation',
            'PARTIAL_SCORE_PUBLICATION': 'Factor scores partially published; missing inputs or symbol mappings are listed in results',
            'COMPLETED': 'Factor pipeline completed',
        }[completion_status]

        _set_task(
            db,
            task_id,
            status='done',
            stage='done',
            percent=100,
            message=completion_message,
            result_json=json.dumps(
                results, ensure_ascii=False, default=str
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        # AC-14 失败隔离：只回滚当前 MySQL 事务（训练阶段写入的 FactorModelRun
        # 会被 rollback → MAX(model_run.id) 前后不变）。DuckDB warehouse 写入的
        # stage1=同步/stage2=因子/stage3=目标数据不会被 MySQL rollback 影响，
        # 即 stage4 训练失败 → stage1..stage3 数据不删（不回滚成功阶段）。
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
            # Task 9 (4) 分阶段 7 要素错误：把 canonical stage 写入 extras.stage
            canonical = _canonical_stage(current_stage)
            error_code, override_message = _classify_pipeline_error(
                exc, current_stage
            )
            # 若是 FactorSevenError（如 readiness 校验异常），优先使用其 7 字段内容
            if isinstance(exc, FactorSevenError):
                fse_dict = exc.to_dict()
                if not fse_dict.get("correlation_id"):
                    fse_dict["correlation_id"] = uuid4().hex
                fse_dict["stage"] = canonical
                # 与 UserError 结构兼容：把 7 要素 extras 填 stage
                extras_for_err = dict(exc.extras or {})
                extras_for_err.setdefault("stage", canonical)
                user_error = build_user_error(
                    exc.error_code,
                    override_user_message=(
                        override_message or exc.detail_zh or exc.title_zh
                    ),
                    override_impact=exc.impact,
                    extras=extras_for_err,
                    correlation_id=exc.correlation_id,
                    technical_details=TechnicalDetails(
                        exception_type=type(exc).__name__,
                        error_message=str(exc),
                    ),
                )
            else:
                user_error = build_user_error(
                    error_code,
                    override_user_message=override_message,
                    extras={"stage": canonical},
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
                result_json=json.dumps(locals().get('results', {}), ensure_ascii=False, default=str),
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
