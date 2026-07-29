"""UAT-PAGES.2：已排除池与扫描记录查询服务。

提供：
1. 已排除候选列表查询（join DiscoveryCandidate + OpportunityTransitionEvent）
   - 数据来源：opportunity_transition_events 的 exclude 事件
   - 返回候选基础信息 + 排除原因 + 排除时间 + 操作来源
2. 扫描记录列表查询（join ScanRun + DiscoveryTaskRecord + DiscoveryScoreSnapshot）
   - 返回快照、阶段耗时、参数、缓存命中、差异摘要、错误详情
3. 单条扫描记录详情查询

设计要点：
- 只读查询，不修改业务数据
- 字段对齐前端 ScanHistory/ExcludedPool 渲染需求
- 错误处理统一中文文案
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from app.models.discovery import DiscoveryTaskRecord
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot
from app.models.opportunity_transition_event import (
    EVENT_EXCLUDE,
    OpportunityTransitionEvent,
)
from app.models.scan import ScanRun

logger = logging.getLogger(__name__)


def _safe_json(raw: str | None) -> Any:
    """安全解析 JSON 字段，失败返回 None。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    """datetime -> ISO 字符串，None 保持 None。"""
    return value.isoformat() if value else None


# ----------------------------------------------------------------------------
# 已排除候选列表
# ----------------------------------------------------------------------------


def list_excluded_candidates(
    db: Session,
    *,
    symbol: str | None = None,
    name: str | None = None,
    exclude_date_from: str | None = None,
    exclude_date_to: str | None = None,
    reason_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查询已排除候选列表。

    数据来源：opportunity_transition_events 中 event_type='exclude' 的记录，
    JOIN discovery_candidates（source_id=candidate.id）获取候选基础信息。

    筛选：
    - symbol: 候选 symbol 子串（不区分大小写）
    - name: 候选 name 子串
    - exclude_date_from / exclude_date_to: 排除时间区间（ISO 日期，UTC）
    - reason_type: reason_json.reason 子串（不区分大小写）

    返回字段（每行）：
    - event_id, excluded_at, actor_type, exclude_reason（reason_json 解析后的 dict/str）
    - candidate_id, symbol, name, asset_type, scan_run_id
    - quality_score, timing_score, priority_score, stage, action
    - created_at（候选创建时间）
    """
    # 基础查询：exclude 事件 LEFT JOIN discovery_candidates（source_type='candidate'）
    stmt = (
        select(OpportunityTransitionEvent, DiscoveryCandidate)
        .outerjoin(
            DiscoveryCandidate,
            and_(
                DiscoveryCandidate.id == OpportunityTransitionEvent.source_id,
                OpportunityTransitionEvent.source_type == "candidate",
            ),
        )
        .where(OpportunityTransitionEvent.event_type == EVENT_EXCLUDE)
    )

    # symbol 子串筛选
    if symbol:
        stmt = stmt.where(
            or_(
                DiscoveryCandidate.symbol.ilike(f"%{symbol}%"),
                DiscoveryCandidate.name.ilike(f"%{symbol}%"),
            )
        )
    # name 子串筛选（独立参数，与 symbol 互补）
    if name:
        stmt = stmt.where(DiscoveryCandidate.name.ilike(f"%{name}%"))

    # 排除时间区间筛选
    if exclude_date_from:
        try:
            from_dt = datetime.fromisoformat(exclude_date_from)
            stmt = stmt.where(OpportunityTransitionEvent.created_at >= from_dt)
        except ValueError:
            logger.warning("list_excluded: invalid exclude_date_from=%s", exclude_date_from)
    if exclude_date_to:
        try:
            to_dt = datetime.fromisoformat(exclude_date_to)
            stmt = stmt.where(OpportunityTransitionEvent.created_at <= to_dt)
        except ValueError:
            logger.warning("list_excluded: invalid exclude_date_to=%s", exclude_date_to)

    # reason_type 子串筛选（在 Python 层做，避免 SQLite JSON 兼容性问题）
    # 注意：先取较大 limit 再过滤，保证 reason_type 命中率
    effective_limit = limit if not reason_type else max(limit * 5, 200)
    stmt = stmt.order_by(
        desc(OpportunityTransitionEvent.created_at),
        desc(OpportunityTransitionEvent.id),
    ).limit(effective_limit).offset(offset)

    rows = db.execute(stmt).all()
    result: list[dict[str, Any]] = []
    for event, candidate in rows:
        if candidate is None:
            # 来源记录已被清理（candidate 删除），仅展示事件
            cand_dict: dict[str, Any] = {
                "candidate_id": None,
                "symbol": None,
                "name": None,
                "asset_type": None,
                "scan_run_id": None,
                "quality_score": None,
                "timing_score": None,
                "priority_score": None,
                "stage": None,
                "action": None,
                "created_at": None,
            }
        else:
            cand_dict = {
                "candidate_id": candidate.id,
                "symbol": candidate.symbol,
                "name": candidate.name,
                "asset_type": candidate.asset_type,
                "scan_run_id": candidate.scan_run_id,
                "quality_score": candidate.quality_score,
                "timing_score": candidate.timing_score,
                "priority_score": candidate.priority_score,
                "stage": candidate.stage,
                "action": candidate.action,
                "created_at": _iso(candidate.created_at),
            }

        reason_raw = _safe_json(event.reason_json)
        # reason_type 子串筛选
        if reason_type:
            reason_str = ""
            if isinstance(reason_raw, dict):
                reason_str = " ".join(str(v) for v in reason_raw.values())
            elif reason_raw is not None:
                reason_str = str(reason_raw)
            if reason_type.lower() not in reason_str.lower():
                continue

        result.append({
            **cand_dict,
            "event_id": event.id,
            "excluded_at": _iso(event.created_at),
            "actor_type": event.actor_type,
            "exclude_reason": reason_raw,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "idempotency_key": event.idempotency_key,
        })
        if len(result) >= limit:
            break

    return result


# ----------------------------------------------------------------------------
# 扫描记录列表与详情
# ----------------------------------------------------------------------------


def _scan_run_to_dict(
    run: ScanRun,
    task: DiscoveryTaskRecord | None,
    snapshot: DiscoveryScoreSnapshot | None,
    *,
    include_detail: bool = False,
) -> dict[str, Any]:
    """把 ScanRun + 关联记录转换为前端字典。

    include_detail=True 时附带 task_record 与 snapshot 详情供详情查看使用。
    """
    payload: dict[str, Any] = {
        # ScanRun 基础字段
        "id": run.id,
        "run_name": run.run_name,
        "scope_snapshot": run.scope_snapshot,
        "filters_snapshot": run.filters_snapshot,
        "portfolio_id": run.portfolio_id,
        "portfolio_rule_id": run.portfolio_rule_id,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
        # ScanRun 缓存与统计字段
        "snapshot_id": run.snapshot_id,
        "cache_key": run.cache_key,
        "cache_hit": run.cache_hit,
        "total_in_snapshot": run.total_in_snapshot,
        "coarse_match_count": run.coarse_match_count,
        "advanced_match_count": run.advanced_match_count,
        "result_rows_written": run.result_rows_written,
        "degraded_reason": run.degraded_reason,
        # DiscoveryTaskRecord 字段（若关联存在）
        "task_id": task.id if task else None,
        "scope": task.scope if task else None,
        "min_score": task.min_score if task else None,
        "stage_durations": _safe_json(task.stage_durations_json) if task else None,
        "dirty_symbol_count": task.dirty_symbol_count if task else None,
        "reused_score_count": task.reused_score_count if task else None,
        "rescored_count": task.rescored_count if task else None,
        "snapshot_hit": task.snapshot_hit if task else None,
        "task_degraded_reason": task.degraded_reason if task else None,
        "task_payload": _safe_json(task.payload_json) if task and include_detail else None,
        "task_errors": _safe_json(task.errors_json) if task and include_detail else None,
        # DiscoveryScoreSnapshot 字段（若关联存在）
        "snapshot_scope": snapshot.scope if snapshot else None,
        "snapshot_trade_date": _iso(snapshot.trade_date) if snapshot else None,
        "snapshot_status": snapshot.status if snapshot else None,
        "snapshot_generated_at": _iso(snapshot.generated_at) if snapshot else None,
        "snapshot_symbol_count": snapshot.symbol_count if snapshot else None,
        "snapshot_coverage_pct": snapshot.coverage_pct if snapshot else None,
        "snapshot_dirty_symbol_count": snapshot.dirty_symbol_count if snapshot else None,
        "snapshot_build_duration_seconds": snapshot.build_duration_seconds if snapshot else None,
        "snapshot_error_summary": _safe_json(snapshot.error_summary_json) if snapshot else None,
        "scoring_config_id": snapshot.scoring_config_id if snapshot else None,
        "scoring_config_version": snapshot.scoring_config_version if snapshot else None,
        "weight_mode": snapshot.weight_mode if snapshot else None,
        "factor_model_run_id": snapshot.factor_model_run_id if snapshot else None,
        "snapshot_data_cutoff_at": _iso(snapshot.data_cutoff_at) if snapshot else None,
    }
    if include_detail:
        payload["task_record"] = {
            "id": task.id if task else None,
            "status": task.status if task else None,
            "stage": task.stage if task else None,
            "percent": task.percent if task else None,
            "message": task.message if task else None,
            "total": task.total if task else None,
            "processed": task.processed if task else None,
            "ok_count": task.ok_count if task else None,
            "failed_count": task.failed_count if task else None,
            "scored_count": task.scored_count if task else None,
            "empty_count": task.empty_count if task else None,
            "executable_count": task.executable_count if task else None,
            "stage_durations_json": _safe_json(task.stage_durations_json) if task else None,
            "errors_json": _safe_json(task.errors_json) if task else None,
            "payload_json": _safe_json(task.payload_json) if task else None,
            "created_at": _iso(task.created_at) if task else None,
            "started_at": _iso(task.started_at) if task else None,
            "finished_at": _iso(task.finished_at) if task else None,
        } if task else None
        payload["snapshot_record"] = {
            "id": snapshot.id if snapshot else None,
            "scope": snapshot.scope if snapshot else None,
            "trade_date": _iso(snapshot.trade_date) if snapshot else None,
            "status": snapshot.status if snapshot else None,
            "generated_at": _iso(snapshot.generated_at) if snapshot else None,
            "build_duration_seconds": snapshot.build_duration_seconds if snapshot else None,
            "symbol_count": snapshot.symbol_count if snapshot else None,
            "coverage_pct": snapshot.coverage_pct if snapshot else None,
            "dirty_symbol_count": snapshot.dirty_symbol_count if snapshot else None,
            "error_summary_json": _safe_json(snapshot.error_summary_json) if snapshot else None,
            "scoring_config_id": snapshot.scoring_config_id if snapshot else None,
            "scoring_config_version": snapshot.scoring_config_version if snapshot else None,
            "weight_mode": snapshot.weight_mode if snapshot else None,
            "factor_model_run_id": snapshot.factor_model_run_id if snapshot else None,
            "data_cutoff_at": _iso(snapshot.data_cutoff_at) if snapshot else None,
            "created_at": _iso(snapshot.created_at) if snapshot else None,
        } if snapshot else None
    return payload


def list_scan_runs(
    db: Session,
    *,
    scope: str | None = None,
    trade_date: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查询扫描记录列表（ScanRun LEFT JOIN DiscoveryTaskRecord + DiscoveryScoreSnapshot）。

    筛选：
    - scope: 通过 DiscoveryTaskRecord.scope 过滤（cn-stock/cn-etf/us-stock/us-etf）
    - trade_date: 通过 DiscoveryScoreSnapshot.trade_date 过滤（ISO 日期）
    - status: 通过 ScanRun.status 过滤（done/running/failed/cancelled）
    """
    stmt = select(ScanRun, DiscoveryTaskRecord, DiscoveryScoreSnapshot).outerjoin(
        DiscoveryTaskRecord,
        DiscoveryTaskRecord.scan_run_id == ScanRun.id,
    ).outerjoin(
        DiscoveryScoreSnapshot,
        DiscoveryScoreSnapshot.id == ScanRun.snapshot_id,
    )

    if scope:
        stmt = stmt.where(DiscoveryTaskRecord.scope == scope)
    if status:
        stmt = stmt.where(ScanRun.status == status)
    if trade_date:
        try:
            td = datetime.fromisoformat(trade_date)
            stmt = stmt.where(DiscoveryScoreSnapshot.trade_date >= td)
            # 区间上界（同一天）：trade_date < td + 1 day
            from datetime import timedelta
            stmt = stmt.where(
                DiscoveryScoreSnapshot.trade_date < td + timedelta(days=1)
            )
        except ValueError:
            logger.warning("list_scan_runs: invalid trade_date=%s", trade_date)

    stmt = stmt.order_by(desc(ScanRun.created_at), desc(ScanRun.id)).limit(limit).offset(offset)
    rows = db.execute(stmt).all()
    return [_scan_run_to_dict(run, task, snapshot) for run, task, snapshot in rows]


def get_scan_run_detail(db: Session, scan_run_id: int) -> dict[str, Any] | None:
    """查询单条扫描记录详情（含 task_record 与 snapshot_record 完整信息）。"""
    row = db.execute(
        select(ScanRun, DiscoveryTaskRecord, DiscoveryScoreSnapshot)
        .outerjoin(
            DiscoveryTaskRecord,
            DiscoveryTaskRecord.scan_run_id == ScanRun.id,
        )
        .outerjoin(
            DiscoveryScoreSnapshot,
            DiscoveryScoreSnapshot.id == ScanRun.snapshot_id,
        )
        .where(ScanRun.id == scan_run_id)
    ).first()
    if row is None:
        return None
    run, task, snapshot = row
    return _scan_run_to_dict(run, task, snapshot, include_detail=True)


__all__ = [
    "list_excluded_candidates",
    "list_scan_runs",
    "get_scan_run_detail",
]
