"""挖掘结果分层保留与清理服务（WP-P.7）。

分层保留策略（参照 spec）：
- 当前候选展示：默认 5 天，3 天预警，过期从活动候选隐藏
- 未晋升 ScanResult：7 天且每 scope 至少保留最近 3 次，到期物理删除
- 未晋升 DiscoveryCandidate：7 天到期物理删除
- 已晋升/已观察/已入组合快照：长期保留，迁入正式对象后按业务审计保留
- 评分快照 items：最近 20 个交易日/每 scope，只由后台清理
- ScanRun 摘要：180 天，删除大明细后保留参数、数量、耗时、错误摘要
- 日 K 与基础因子：按模型/回测所需窗口长期保留，禁止随候选过期删除
- Score 历史：至少 250 个交易日
- 候选晋升为观察项或组合成员时把入选评分和来源复制为正式快照
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.discovery_score_snapshot import (
    DiscoveryScoreSnapshot,
    DiscoveryScoreSnapshotItem,
)
from app.models.portfolio import Position
from app.models.scan import ScanResult, ScanRun
from app.models.watchlist import WatchlistItem

logger = logging.getLogger(__name__)


# 默认保留期（参照 spec）
DEFAULT_CANDIDATE_DISPLAY_DAYS = 5     # 当前候选展示 5 天
DEFAULT_CANDIDATE_WARNING_DAYS = 3    # 3 天预警
DEFAULT_SCAN_RESULT_RETENTION_DAYS = 7  # 未晋升 ScanResult 7 天
DEFAULT_MIN_SCAN_RUNS_PER_SCOPE = 3   # 每 scope 至少保留最近 3 次
DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS = 7  # 未晋升 DiscoveryCandidate 7 天
DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS = 20  # 评分快照 items 最近 20 个交易日
DEFAULT_SCAN_RUN_RETENTION_DAYS = 180  # ScanRun 摘要 180 天
DEFAULT_SCORE_HISTORY_TRADING_DAYS = 250  # Score 历史至少 250 个交易日


def _now_utc() -> datetime:
    """统一 UTC naive datetime（与现有模型默认值保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class CleanupReport:
    """清理操作报告。"""
    deleted_scan_results: int = 0
    deleted_scan_runs: int = 0
    deleted_discovery_candidates: int = 0
    deleted_snapshot_items: int = 0
    deleted_superseded_snapshots: int = 0
    hidden_active_candidates: int = 0  # 从活动候选隐藏但未删除
    kept_min_scan_runs_per_scope: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "deleted_scan_results": self.deleted_scan_results,
            "deleted_scan_runs": self.deleted_scan_runs,
            "deleted_discovery_candidates": self.deleted_discovery_candidates,
            "deleted_snapshot_items": self.deleted_snapshot_items,
            "deleted_superseded_snapshots": self.deleted_superseded_snapshots,
            "hidden_active_candidates": self.hidden_active_candidates,
            "kept_min_scan_runs_per_scope": dict(self.kept_min_scan_runs_per_scope),
            "errors": list(self.errors),
        }


# ============================================================================
# 辅助函数
# ============================================================================

def _safe_datetime(value) -> datetime | None:
    """安全地将值转换为 datetime 类型，处理 MySQL 返回的字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _extract_scope_from_snapshot(scope_snapshot: str | None) -> str | None:
    """从 ScanRun.scope_snapshot JSON 中解析 scope 字段。

    scope_snapshot 形如 {"snapshot_id": 1, "scope": "cn_stock", ...}。
    无法解析时返回 None（调用方按 "unknown" 兜底分组）。
    """
    if not scope_snapshot:
        return None
    try:
        data = json.loads(scope_snapshot)
        if isinstance(data, dict):
            scope = data.get("scope")
            if isinstance(scope, str) and scope:
                return scope
    except (ValueError, TypeError):
        return None
    return None


def _get_promoted_symbol_ids(db: Session) -> set[int]:
    """获取已晋升的 symbol_id 集合（来自 DiscoveryCandidate.is_promoted=1）。

    通过 candidate.symbol 反查 Symbol.id（与 candidate_promote.list_candidates 一致）。
    """
    from app.models.symbol import Symbol

    rows = db.execute(
        select(Symbol.id)
        .join(DiscoveryCandidate, DiscoveryCandidate.symbol == Symbol.symbol)
        .where(DiscoveryCandidate.is_promoted == 1)
    ).all()
    return {r[0] for r in rows}


def _get_observed_symbol_ids(db: Session) -> set[int]:
    """获取已观察的 symbol_id 集合（WatchlistItem）。"""
    rows = db.execute(select(WatchlistItem.symbol_id)).all()
    return {r[0] for r in rows}


def _get_position_symbol_ids(db: Session) -> set[int]:
    """获取已入组合的 symbol_id 集合（Position）。"""
    rows = db.execute(select(Position.symbol_id)).all()
    return {r[0] for r in rows}


# ============================================================================
# 1. 隐藏过期活动候选
# ============================================================================

def _hide_expired_active_candidates(
    db: Session,
    *,
    now: datetime,
    display_days: int = DEFAULT_CANDIDATE_DISPLAY_DAYS,
) -> int:
    """隐藏过期的活动候选（不删除，仅更新 is_active=0）。

    超过 display_days 的活动候选从展示隐藏，但不物理删除。
    后续可被 _cleanup_expired_scan_results 删除。

    操作对象：ScanResult 中 is_active=1 且未冻结且年龄超过 display_days 的记录。
    已晋升/已观察/已入组合的 ScanResult 不隐藏（保留展示）。
    """
    cutoff = now - timedelta(days=display_days)

    # 收集需要保留活动状态的 symbol_id（已晋升/已观察/已入组合）
    protected_symbol_ids = (
        _get_promoted_symbol_ids(db)
        | _get_observed_symbol_ids(db)
        | _get_position_symbol_ids(db)
    )

    # 查询需要隐藏的 ScanResult：未冻结、created_at 早于 cutoff、is_active != 0
    # is_active 字段是 WP-P.7 新增列，可能为 NULL（旧记录），视为 1
    stmt = (
        select(ScanResult.id, ScanResult.symbol_id)
        .where(ScanResult.is_frozen == 0)
        .where(ScanResult.created_at < cutoff)
        .where(ScanResult.is_active != 0)
    )
    try:
        rows = db.execute(stmt).all()
    except Exception:
        # 旧库可能没有 is_active 列；调用方应先执行 schema 补丁
        logger.warning("_hide_expired_active_candidates: is_active column missing, skip")
        return 0

    hidden_ids: list[int] = []
    for row in rows:
        symbol_id = row[1]
        if symbol_id in protected_symbol_ids:
            continue
        hidden_ids.append(row[0])

    if not hidden_ids:
        return 0

    # 单事务批量更新 is_active=0
    try:
        result = db.execute(
            ScanResult.__table__.update()
            .where(ScanResult.id.in_(hidden_ids))
            .values(is_active=0)
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        db.rollback()
        raise


# ============================================================================
# 2. 删除过期未晋升 ScanResult
# ============================================================================

def _cleanup_expired_scan_results(
    db: Session,
    *,
    now: datetime,
    retention_days: int = DEFAULT_SCAN_RESULT_RETENTION_DAYS,
    min_per_scope: int = DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
) -> tuple[int, dict[str, int]]:
    """删除过期未晋升 ScanResult。

    规则：
    - 超过 retention_days 的 ScanResult 删除
    - 但每 scope 至少保留最近 min_per_scope 次 ScanRun 的结果
    - 已晋升/已观察/已入组合的 ScanResult 不删除（参照 spec "已晋升/已观察/已入组合快照长期保留"）

    Returns:
        (deleted_count, kept_per_scope)
    """
    cutoff = now - timedelta(days=retention_days)

    # 收集受保护 symbol_id（已晋升/已观察/已入组合）
    protected_symbol_ids = (
        _get_promoted_symbol_ids(db)
        | _get_observed_symbol_ids(db)
        | _get_position_symbol_ids(db)
    )

    # 查询所有 ScanRun，按 scope 分组并按 created_at desc 排序
    all_runs = db.execute(
        select(ScanRun).order_by(ScanRun.created_at.desc())
    ).scalars().all()

    # 按 scope 分组
    runs_by_scope: dict[str, list[ScanRun]] = {}
    for run in all_runs:
        scope = _extract_scope_from_snapshot(run.scope_snapshot) or "unknown"
        runs_by_scope.setdefault(scope, []).append(run)

    # 计算每 scope 需要保留的 ScanRun id（最近 min_per_scope 次）
    kept_run_ids_per_scope: dict[str, set[int]] = {}
    kept_per_scope: dict[str, int] = {}
    for scope, runs in runs_by_scope.items():
        # runs 已按 created_at desc 排序
        kept_runs = runs[:min_per_scope]
        kept_run_ids_per_scope[scope] = {r.id for r in kept_runs}
        kept_per_scope[scope] = len(kept_runs)

    # 所有需要保留的 ScanRun id 集合（并集）
    all_kept_run_ids: set[int] = set()
    for ids in kept_run_ids_per_scope.values():
        all_kept_run_ids.update(ids)

    # 查询过期 ScanResult 候选：created_at < cutoff 且未冻结
    expired_results = db.execute(
        select(ScanResult)
        .where(ScanResult.is_frozen == 0)
        .where(ScanResult.created_at < cutoff)
    ).scalars().all()

    # 过滤：受保护 symbol 或属于保留 ScanRun 的不删除
    to_delete_ids: list[int] = []
    for sr in expired_results:
        if sr.symbol_id in protected_symbol_ids:
            continue
        if sr.scan_run_id in all_kept_run_ids:
            continue
        to_delete_ids.append(sr.id)

    if not to_delete_ids:
        return 0, kept_per_scope

    # 单事务批量删除
    try:
        result = db.execute(
            delete(ScanResult).where(ScanResult.id.in_(to_delete_ids))
        )
        db.commit()
        return result.rowcount or 0, kept_per_scope
    except Exception:
        db.rollback()
        raise


# ============================================================================
# 3. 删除过期未晋升 DiscoveryCandidate
# ============================================================================

def _cleanup_expired_discovery_candidates(
    db: Session,
    *,
    now: datetime,
    retention_days: int = DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS,
) -> int:
    """删除过期未晋升 DiscoveryCandidate。

    规则：
    - 超过 retention_days 的未晋升 DiscoveryCandidate 物理删除
    - 已晋升为观察项或组合成员的不删除
    """
    cutoff = now - timedelta(days=retention_days)

    # 查询过期且未晋升的候选
    expired_candidates = db.execute(
        select(DiscoveryCandidate.id)
        .where(DiscoveryCandidate.is_promoted == 0)
        .where(DiscoveryCandidate.created_at < cutoff)
    ).all()

    to_delete_ids = [r[0] for r in expired_candidates]
    if not to_delete_ids:
        return 0

    try:
        result = db.execute(
            delete(DiscoveryCandidate).where(DiscoveryCandidate.id.in_(to_delete_ids))
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        db.rollback()
        raise


# ============================================================================
# 4. 删除过期 ScanRun 摘要
# ============================================================================

def _cleanup_old_scan_runs(
    db: Session,
    *,
    now: datetime,
    retention_days: int = DEFAULT_SCAN_RUN_RETENTION_DAYS,
) -> int:
    """删除过期 ScanRun 摘要。

    规则：
    - 超过 retention_days 的 ScanRun 物理删除
    - 但 ScanResult 应已先清理（外键 ON DELETE CASCADE）

    spec："ScanRun 摘要：180 天，删除大明细后保留参数、数量、耗时、错误摘要"
    本函数物理删除整条 ScanRun；如需保留摘要 metadata 应在调用前另存。
    """
    cutoff = now - timedelta(days=retention_days)

    expired_runs = db.execute(
        select(ScanRun.id).where(ScanRun.created_at < cutoff)
    ).all()

    to_delete_ids = [r[0] for r in expired_runs]
    if not to_delete_ids:
        return 0

    try:
        result = db.execute(
            delete(ScanRun).where(ScanRun.id.in_(to_delete_ids))
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        db.rollback()
        raise


# ============================================================================
# 5. 删除过期评分快照 items
# ============================================================================

def _cleanup_old_snapshot_items(
    db: Session,
    *,
    now: datetime,
    retention_trading_days: int = DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
) -> tuple[int, int]:
    """删除过期评分快照 items。

    规则：
    - 保留每 scope 最近 N 个交易日的快照 items
    - superseded 状态快照的 items 全部删除（保留主表 metadata）

    Returns:
        (deleted_items_count, deleted_superseded_snapshots_count)
        - deleted_superseded_snapshots_count: 被清理 items 的 superseded 快照数量
    """
    # 1. 收集每 scope 最近 N 个交易日的 trade_date
    ready_snapshots = db.execute(
        select(DiscoveryScoreSnapshot)
        .where(DiscoveryScoreSnapshot.status == "ready")
        .order_by(DiscoveryScoreSnapshot.trade_date.desc())
    ).scalars().all()

    # 按 scope 分组，取每 scope 最近 N 个 distinct trade_date
    latest_dates_per_scope: dict[str, set[datetime]] = {}
    distinct_dates_per_scope: dict[str, list[datetime]] = {}
    for snap in ready_snapshots:
        scope = snap.scope
        dates = distinct_dates_per_scope.setdefault(scope, [])
        if snap.trade_date not in dates:
            dates.append(snap.trade_date)
        # 不截断，先收集所有 distinct dates

    for scope, dates in distinct_dates_per_scope.items():
        # dates 按 ready_snapshots 的 trade_date desc 顺序去重后已保持 desc
        latest_dates_per_scope[scope] = set(dates[:retention_trading_days])

    # 2. 找出 items 需要删除的 snapshot id
    #    - ready 但 trade_date 不在最近 N 个交易日
    #    - superseded 状态的全部
    all_snapshots = db.execute(
        select(DiscoveryScoreSnapshot.id, DiscoveryScoreSnapshot.scope,
               DiscoveryScoreSnapshot.trade_date, DiscoveryScoreSnapshot.status)
    ).all()

    snapshots_to_clean_items: list[int] = []  # ready 但过期的 snapshot
    superseded_snapshots: list[int] = []      # superseded 状态的 snapshot
    for snap_id, scope, trade_date, status in all_snapshots:
        if status == "superseded":
            superseded_snapshots.append(snap_id)
        elif status == "ready":
            latest_dates = latest_dates_per_scope.get(scope, set())
            if trade_date not in latest_dates:
                snapshots_to_clean_items.append(snap_id)

    deleted_items_count = 0

    # 3. 删除 ready 但过期的 snapshot items
    if snapshots_to_clean_items:
        try:
            result = db.execute(
                delete(DiscoveryScoreSnapshotItem).where(
                    DiscoveryScoreSnapshotItem.snapshot_id.in_(snapshots_to_clean_items)
                )
            )
            deleted_items_count += result.rowcount or 0
        except Exception:
            db.rollback()
            raise

    # 4. 删除 superseded snapshot 的 items
    if superseded_snapshots:
        try:
            result = db.execute(
                delete(DiscoveryScoreSnapshotItem).where(
                    DiscoveryScoreSnapshotItem.snapshot_id.in_(superseded_snapshots)
                )
            )
            deleted_items_count += result.rowcount or 0
        except Exception:
            db.rollback()
            raise

    db.commit()
    return deleted_items_count, len(superseded_snapshots)


# ============================================================================
# 6. 删除 superseded 状态快照主表
# ============================================================================

def _cleanup_superseded_snapshots(
    db: Session,
) -> int:
    """删除 superseded 状态快照主表（items 应已先清理）。

    规则：
    - superseded 状态且无任何引用的快照主表删除
    - 保留 ready / building / failed 状态快照
    - 引用检查：ScanRun.snapshot_id 不引用此快照
    """
    # 查询所有 superseded 快照
    superseded_snaps = db.execute(
        select(DiscoveryScoreSnapshot.id).where(
            DiscoveryScoreSnapshot.status == "superseded"
        )
    ).all()
    superseded_ids = [r[0] for r in superseded_snaps]
    if not superseded_ids:
        return 0

    # 查询被 ScanRun 引用的 snapshot id
    referenced_ids = set(
        r[0] for r in db.execute(
            select(ScanRun.snapshot_id)
            .where(ScanRun.snapshot_id.in_(superseded_ids))
            .where(ScanRun.snapshot_id.isnot(None))
        ).all()
    )

    # 排除被引用的
    to_delete_ids = [sid for sid in superseded_ids if sid not in referenced_ids]
    if not to_delete_ids:
        return 0

    try:
        result = db.execute(
            delete(DiscoveryScoreSnapshot).where(
                DiscoveryScoreSnapshot.id.in_(to_delete_ids)
            )
        )
        db.commit()
        return result.rowcount or 0
    except Exception:
        db.rollback()
        raise


# ============================================================================
# 聚合入口
# ============================================================================

def cleanup_expired_discovery_results_layered(
    db: Session,
    *,
    now: datetime | None = None,
    scan_result_retention_days: int = DEFAULT_SCAN_RESULT_RETENTION_DAYS,
    min_scan_runs_per_scope: int = DEFAULT_MIN_SCAN_RUNS_PER_SCOPE,
    discovery_candidate_retention_days: int = DEFAULT_DISCOVERY_CANDIDATE_RETENTION_DAYS,
    snapshot_items_retention_trading_days: int = DEFAULT_SNAPSHOT_ITEMS_RETENTION_TRADING_DAYS,
    scan_run_retention_days: int = DEFAULT_SCAN_RUN_RETENTION_DAYS,
) -> CleanupReport:
    """分层清理挖掘结果（WP-P.7）。

    顺序：
    1. 隐藏过期活动候选（不删除，从展示隐藏）
    2. 删除过期未晋升 ScanResult（每 scope 至少保留最近 N 次）
    3. 删除过期未晋升 DiscoveryCandidate
    4. 删除过期 ScanRun 摘要（180 天前，但保留参数/数量/耗时）
    5. 删除评分快照 items（最近 20 个交易日外）
    6. 删除 superseded 状态快照（被替换后无引用）

    全程 best-effort：单步失败不阻塞后续步骤，记入 errors。
    关键：日 K 与基础因子 / Score 历史不在本服务删除（参照 spec 约束）。
    """
    if now is None:
        now = _now_utc()

    report = CleanupReport()

    # 步骤 1：隐藏过期活动候选
    try:
        report.hidden_active_candidates = _hide_expired_active_candidates(
            db, now=now,
        )
    except Exception as exc:
        db.rollback()
        logger.exception("_hide_expired_active_candidates failed")
        report.errors.append({
            "step": "hide_expired_active_candidates",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })

    # 步骤 2：删除过期 ScanResult
    try:
        deleted, kept_per_scope = _cleanup_expired_scan_results(
            db, now=now,
            retention_days=scan_result_retention_days,
            min_per_scope=min_scan_runs_per_scope,
        )
        report.deleted_scan_results = deleted
        report.kept_min_scan_runs_per_scope = kept_per_scope
    except Exception as exc:
        db.rollback()
        logger.exception("_cleanup_expired_scan_results failed")
        report.errors.append({
            "step": "cleanup_expired_scan_results",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })

    # 步骤 3：删除过期 DiscoveryCandidate
    try:
        report.deleted_discovery_candidates = _cleanup_expired_discovery_candidates(
            db, now=now,
            retention_days=discovery_candidate_retention_days,
        )
    except Exception as exc:
        db.rollback()
        logger.exception("_cleanup_expired_discovery_candidates failed")
        report.errors.append({
            "step": "cleanup_expired_discovery_candidates",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })

    # 步骤 4：删除过期 ScanRun
    try:
        report.deleted_scan_runs = _cleanup_old_scan_runs(
            db, now=now,
            retention_days=scan_run_retention_days,
        )
    except Exception as exc:
        db.rollback()
        logger.exception("_cleanup_old_scan_runs failed")
        report.errors.append({
            "step": "cleanup_old_scan_runs",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })

    # 步骤 5：删除过期快照 items
    try:
        deleted_items, _ = _cleanup_old_snapshot_items(
            db, now=now,
            retention_trading_days=snapshot_items_retention_trading_days,
        )
        report.deleted_snapshot_items = deleted_items
    except Exception as exc:
        db.rollback()
        logger.exception("_cleanup_old_snapshot_items failed")
        report.errors.append({
            "step": "cleanup_old_snapshot_items",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })

    # 步骤 6：删除 superseded 快照主表
    try:
        report.deleted_superseded_snapshots = _cleanup_superseded_snapshots(db)
    except Exception as exc:
        db.rollback()
        logger.exception("_cleanup_superseded_snapshots failed")
        report.errors.append({
            "step": "cleanup_superseded_snapshots",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        })

    return report
