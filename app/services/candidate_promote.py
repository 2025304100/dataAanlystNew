"""P1：挖掘候选独立存储与手动晋升服务。

核心职责：
1. 挖掘完成后，把 ScanResult 中的 executable 候选同步写入 discovery_candidates（is_promoted=0）
2. 用户手动点击"加入候选池"时，执行 promote_candidate 标记 is_promoted=1
3. 查询候选列表（支持按 scan_run_id / is_promoted 过滤）

设计要点：
- discovery_candidates 通过 universe_symbol_id 关联基础表 universe_symbols
- 通过 symbol code 反查 universe_symbols.id（挖掘流程中 Symbol.symbol 与 UniverseSymbol.symbol 一致）
- 评分快照（quality/timing/priority/dimension_scores_json）写入候选记录，避免后续 score 变动影响展示
- is_promoted=0 的候选不进入业务候选池（symbols 表 is_active=1 且在 latest-candidates 中）
  注：当前 latest-candidates 仍查 ScanResult，P1 阶段保留兼容，前端通过 is_promoted 字段区分展示
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.discovery_score_snapshot import DiscoveryScoreSnapshotItem
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services.factors.score_scope import get_active_score_scope

logger = logging.getLogger(__name__)


def _batch_latest_scores(db: Session, symbol_ids: list[int]) -> dict[int, Score]:
    """批量查 symbol_ids 的最新 Score 记录（按 trade_date 最新）。

    复用 discovery_results 的查询模式，避免 N+1。
    """
    if not symbol_ids:
        return {}
    from sqlalchemy import func
    scope = get_active_score_scope(db)

    latest_score_subq = (
        select(Score.symbol_id, func.max(Score.trade_date).label("max_date"))
        .where(Score.symbol_id.in_(symbol_ids))
        .where(Score.weight_mode == scope.weight_mode)
        .where(
            Score.factor_model_run_id == scope.model_run_id
            if scope.weight_mode == 'ridge'
            else True
        )
        .group_by(Score.symbol_id)
        .subquery()
    )
    score_rows = db.execute(
        select(Score).join(
            latest_score_subq,
            (Score.symbol_id == latest_score_subq.c.symbol_id)
            & (Score.trade_date == latest_score_subq.c.max_date),
        )
        .where(Score.weight_mode == scope.weight_mode)
        .where(
            Score.factor_model_run_id == scope.model_run_id
            if scope.weight_mode == 'ridge'
            else True
        )
    ).scalars().all()
    return {s.symbol_id: s for s in score_rows}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _find_universe_symbol_by_code(db: Session, symbol_code: str) -> UniverseSymbol | None:
    """通过 symbol code 反查 universe_symbols 表。

    挖掘流程中 Symbol.symbol（如 "000001"）与 UniverseSymbol.symbol 一致，
    因此可直接通过 code 匹配。
    """
    code = symbol_code.strip().upper()
    return db.execute(
        select(UniverseSymbol).where(UniverseSymbol.symbol == code)
    ).scalars().first()


def sync_scan_results_to_candidates(
    db: Session,
    *,
    scan_run_id: int,
    warning_days: int = 3,
    valid_days: int = 5,
) -> int:
    """挖掘完成后，把 ScanResult executable 候选同步写入 discovery_candidates。

    幂等：通过 (scan_run_id, universe_symbol_id) 唯一约束去重，已存在的跳过。
    只同步 result_type="executable" 的记录（与候选池语义一致）。

    返回：新增候选数量。
    """
    # 查询该 scan_run 的 executable 结果，关联 Symbol 拿 code
    rows = db.execute(
        select(ScanResult, Symbol)
        .join(Symbol, Symbol.id == ScanResult.symbol_id)
        .where(
            ScanResult.scan_run_id == scan_run_id,
            ScanResult.result_type == "executable",
        )
        .order_by(ScanResult.priority_score.desc(), ScanResult.created_at.desc())
    ).all()

    if not rows:
        logger.info("candidate_sync: scan_run=%d has no executable results, skip", scan_run_id)
        return 0

    # 批量查 universe_symbols（按 code 匹配，避免 N+1）
    codes = [s.symbol for _, s in rows if s.symbol]
    universe_map: dict[str, UniverseSymbol] = {}
    if codes:
        universe_rows = db.execute(
            select(UniverseSymbol).where(UniverseSymbol.symbol.in_(codes))
        ).scalars().all()
        universe_map = {u.symbol: u for u in universe_rows}

    # 查询已存在的候选（幂等检查）
    existing_universe_ids = set(
        r[0] for r in db.execute(
            select(DiscoveryCandidate.universe_symbol_id).where(
                DiscoveryCandidate.scan_run_id == scan_run_id
            )
        ).all()
    )

    # 批量查 Score 表，获取维度评分明细（含 event_score）和评分配置快照
    symbol_ids = [s.id for _, s in rows if s.id is not None]
    score_map = _batch_latest_scores(db, symbol_ids)

    created = 0
    skipped_no_universe = 0
    skipped_exists = 0
    for result, symbol in rows:
        universe = universe_map.get(symbol.symbol)
        if universe is None:
            # 基础表未同步该标的（可能 universe 同步未完成或失败），跳过
            skipped_no_universe += 1
            continue
        if universe.id in existing_universe_ids:
            skipped_exists += 1
            continue

        # 从 Score 表取维度评分明细和配置快照（保留消息面 event_score 等维度）
        score = score_map.get(symbol.id)
        dim_json = score.dimension_scores_json if score else None
        config_snapshot = score.scoring_config_snapshot_json if score else None

        candidate = DiscoveryCandidate(
            scan_run_id=scan_run_id,
            universe_symbol_id=universe.id,
            symbol=symbol.symbol,
            name=symbol.name,
            asset_type=symbol.asset_type,
            quality_score=result.quality_score,
            timing_score=result.timing_score,
            priority_score=result.priority_score,
            dimension_scores_json=dim_json,
            scoring_config_snapshot_json=config_snapshot,
            stage=result.stage,
            action=result.action,
            reason_tags=result.reason_tags,
            warning_days=warning_days,
            valid_days=valid_days,
            is_promoted=0,
        )
        db.add(candidate)
        db.flush()  # 确保 candidate.id 可用，供 emit_discovery_new 使用
        # WP-MSG.7：候选新发现事件接入通知系统（不阻断主流程）
        try:
            from app.services.notifications.event_emitter import emit_discovery_new
            emit_discovery_new(
                db,
                candidate_id=candidate.id,
                symbol_id=symbol.id,
                symbol=symbol.symbol,
                score=float(candidate.priority_score or 0.0),
                reason=None,
            )
        except Exception:
            logger.warning(
                "emit_discovery_new failed for symbol=%s scan_run=%d (non-blocking)",
                symbol.symbol, scan_run_id, exc_info=True,
            )
        created += 1
        existing_universe_ids.add(universe.id)

    db.flush()
    logger.info(
        "candidate_sync: scan_run=%d created=%d skipped_no_universe=%d skipped_exists=%d",
        scan_run_id, created, skipped_no_universe, skipped_exists,
    )
    return created


def promote_candidate(db: Session, candidate_id: int) -> dict[str, Any]:
    """手动晋升单个候选：标记 is_promoted=1。

    晋升语义：用户确认该挖掘结果值得进入业务候选池。
    晋升后不会自动操作 symbols 表（symbols 表在挖掘流程中已存在），
    而是通过 is_promoted=1 让前端区分"已确认/待确认"状态。

    返回：{"ok": bool, "candidate_id": int, "symbol": str, "already_promoted": bool}
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}

    if candidate.is_promoted == 1:
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "symbol": candidate.symbol,
            "already_promoted": True,
        }

    candidate.is_promoted = 1
    candidate.promoted_at = _now()
    db.commit()
    logger.info("candidate_promote: id=%d symbol=%s promoted", candidate_id, candidate.symbol)
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "symbol": candidate.symbol,
        "already_promoted": False,
    }


def promote_candidates_batch(db: Session, candidate_ids: list[int]) -> dict[str, Any]:
    """批量晋升候选。

    返回：{"ok": bool, "promoted_count": int, "already_promoted_count": int, "not_found_ids": list}
    """
    promoted = 0
    already = 0
    not_found: list[int] = []
    now = _now()

    for cid in candidate_ids:
        candidate = db.get(DiscoveryCandidate, cid)
        if candidate is None:
            not_found.append(cid)
            continue
        if candidate.is_promoted == 1:
            already += 1
            continue
        candidate.is_promoted = 1
        candidate.promoted_at = now
        promoted += 1

    if promoted > 0:
        db.commit()
    logger.info(
        "candidate_promote_batch: promoted=%d already=%d not_found=%d",
        promoted, already, len(not_found),
    )
    return {
        "ok": True,
        "promoted_count": promoted,
        "already_promoted_count": already,
        "not_found_ids": not_found,
    }


def unpromote_candidate(db: Session, candidate_id: int) -> dict[str, Any]:
    """撤销晋升（从候选池移除）。

    返回：{"ok": bool, "candidate_id": int, "symbol": str}
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}

    candidate.is_promoted = 0
    candidate.promoted_at = None
    db.commit()
    logger.info("candidate_unpromote: id=%d symbol=%s", candidate_id, candidate.symbol)
    return {"ok": True, "candidate_id": candidate_id, "symbol": candidate.symbol}


def list_candidates(
    db: Session,
    *,
    scan_run_id: int | None = None,
    is_promoted: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查询候选列表，支持按 scan_run_id 和 is_promoted 过滤。

    返回字段与 get_latest_discovery_candidates 对齐，便于前端复用渲染逻辑。
    维度评分明细（trend/momentum/event_score 等）从 Score 表批量预加载。
    """
    stmt = select(DiscoveryCandidate)
    if scan_run_id is not None:
        stmt = stmt.where(DiscoveryCandidate.scan_run_id == scan_run_id)
    if is_promoted is not None:
        stmt = stmt.where(DiscoveryCandidate.is_promoted == is_promoted)
    stmt = stmt.order_by(
        DiscoveryCandidate.is_promoted.desc(),  # 已晋升的排前面
        # MySQL 不支持 NULLS LAST，用 COALESCE 兜底：null 视为 0 分排在最后
        DiscoveryCandidate.priority_score.desc(),
        DiscoveryCandidate.created_at.desc(),
    ).limit(limit).offset(offset)

    rows = db.execute(stmt).scalars().all()
    if not rows:
        return []

    # 通过 symbol code 反查 Symbol.id，再批量查 Score 表获取分项评分
    codes = [c.symbol for c in rows if c.symbol]
    sym_id_map: dict[str, int] = {}
    if codes:
        sym_rows = db.execute(
            select(Symbol.id, Symbol.symbol).where(Symbol.symbol.in_(codes))
        ).all()
        sym_id_map = {s.symbol: s.id for s in sym_rows}

    symbol_ids = list(set(sym_id_map.values()))
    score_map = _batch_latest_scores(db, symbol_ids)

    return [_candidate_to_dict(c, score_map, sym_id_map) for c in rows]


def get_latest_scan_run_candidates(
    db: Session,
    *,
    min_score: float = 0.0,
    limit: int = 50,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """获取最新一次挖掘任务的候选列表（从 discovery_candidates 表读）。

    与 discovery_results.get_latest_discovery_candidates 语义一致，
    但数据源从 ScanResult 改为 DiscoveryCandidate，带 is_promoted 字段。

    若最新任务无候选记录（P1 改造前的历史任务），回退查 ScanResult 兼容。
    """
    from app.models.discovery import DiscoveryTaskRecord

    # 1. 找最新 done 状态的挖掘任务
    task_stmt = select(DiscoveryTaskRecord).where(DiscoveryTaskRecord.status == "done")
    if scope:
        task_stmt = task_stmt.where(DiscoveryTaskRecord.scope == scope)
    task_stmt = task_stmt.order_by(
        DiscoveryTaskRecord.finished_at.desc(), DiscoveryTaskRecord.id.desc()
    )
    latest_task = db.execute(task_stmt).scalars().first()
    if latest_task is None or latest_task.scan_run_id is None:
        return []

    scan_run_id = latest_task.scan_run_id

    # 2. 优先查 discovery_candidates（P1 新表）
    candidates = list_candidates(db, scan_run_id=scan_run_id, limit=limit * 2)
    if candidates:
        # min_score 过滤
        filtered = [c for c in candidates if float(c.get("priority_score") or 0) >= min_score]
        return filtered[:limit]

    # 3. 回退：P1 改造前的历史任务，查 ScanResult（兼容旧数据）
    logger.info(
        "get_latest_scan_run_candidates: no discovery_candidates for scan_run=%d, "
        "fallback to ScanResult (legacy task)",
        scan_run_id,
    )
    return _fallback_legacy_candidates(db, scan_run_id=scan_run_id, min_score=min_score, limit=limit)


def _fallback_legacy_candidates(
    db: Session, *, scan_run_id: int, min_score: float, limit: int
) -> list[dict[str, Any]]:
    """回退逻辑：P1 改造前的历史任务查 ScanResult，is_promoted 固定为 None（未知）。

    维度评分明细（trend/momentum/event_score 等）从 Score 表批量预加载，
    与 list_candidates 保持一致，确保历史候选也能展示消息面维度。
    """
    from app.services.regions import region_from_market

    rows = db.execute(
        select(ScanResult, Symbol)
        .join(Symbol, Symbol.id == ScanResult.symbol_id)
        .where(
            ScanResult.scan_run_id == scan_run_id,
            ScanResult.result_type == "quality",
        )
        .order_by(ScanResult.priority_score.desc(), ScanResult.created_at.desc())
    ).all()

    now = _now()
    # 先过滤出有效候选，再批量查 Score 表
    valid_pairs: list[tuple[ScanResult, Symbol]] = []
    for scan_result, symbol in rows:
        if not scan_result.is_frozen:
            age_days = max(0, (now - scan_result.created_at).days) if scan_result.created_at else 0
            if age_days >= scan_result.valid_days:
                continue
        if float(scan_result.priority_score or 0) < min_score:
            continue
        valid_pairs.append((scan_result, symbol))
        if len(valid_pairs) >= limit:
            break

    if not valid_pairs:
        return []

    # 批量查 Score 表，获取维度评分明细（含 event_score 消息面）
    symbol_ids = [s.id for _, s in valid_pairs if s.id is not None]
    score_map = _batch_latest_scores(db, symbol_ids)

    result: list[dict[str, Any]] = []
    for scan_result, symbol in valid_pairs:
        score = score_map.get(symbol.id)
        result.append({
            "id": None,
            "candidate_id": None,
            "scan_run_id": scan_run_id,
            "universe_symbol_id": None,
            "symbol_id": symbol.id,
            "symbol": symbol.symbol,
            "name": symbol.name,
            "market": symbol.market,
            "region": region_from_market(symbol.market),
            "asset_type": symbol.asset_type,
            "quality_score": scan_result.quality_score,
            "timing_score": scan_result.timing_score,
            "priority_score": scan_result.priority_score,
            # 维度评分分项（从 Score 表预加载，保留消息面 event_score 等）
            "trend_score": score.trend_score if score else None,
            "momentum_score": score.momentum_score if score else None,
            "volatility_score": score.volatility_score if score else None,
            "liquidity_score": score.liquidity_score if score else None,
            "breadth_score": score.breadth_score if score else None,
            "event_score": score.event_score if score else None,
            "data_credibility": score.data_credibility if score else None,
            "scoring_preset_key": score.scoring_preset_key if score else None,
            "scoring_preset_name": score.scoring_preset_name if score else None,
            "scoring_config_version": score.scoring_config_version if score else None,
            "dimension_scores_json": score.dimension_scores_json if score else None,
            "scoring_config_snapshot_json": score.scoring_config_snapshot_json if score else None,
            "stage": scan_result.stage,
            "action": scan_result.action,
            "warning_days": scan_result.warning_days,
            "valid_days": scan_result.valid_days,
            "is_promoted": None,  # 历史数据无晋升状态
            "is_legacy": True,
            "created_at": scan_result.created_at.isoformat() if scan_result.created_at else None,
        })
    return result


def _candidate_to_dict(
    c: DiscoveryCandidate,
    score_map: dict[int, Score] | None = None,
    sym_id_map: dict[str, int] | None = None,
) -> dict[str, Any]:
    """把 DiscoveryCandidate ORM 对象转为前端可用的 dict。

    market/region 从关联的 UniverseSymbol 读取（调用方应预加载 relationship）；
    若未预加载则按 asset_type 推断 cn 区域兜底。

    维度评分明细（trend/momentum/event_score 等）从 score_map 预加载，
    dimension_scores_json/scoring_config_snapshot_json 从候选记录自身读取（sync 时快照）。
    """
    market = None
    region = None
    try:
        # 尝试访问 relationship（若 session 已关闭或未加载会抛 DetachedInstanceError）
        universe_ref = c.universe_symbol_ref
        if universe_ref is not None:
            market = universe_ref.market
            region = universe_ref.region
    except Exception:
        pass
    # 兜底：asset_type 推断
    if market is None and c.asset_type in ("stock", "etf"):
        market = "cn"
    if region is None and c.asset_type in ("stock", "etf"):
        region = "cn"

    # 从 Score 表预加载数据中取分项评分（含 event_score 消息面维度）
    symbol_id = sym_id_map.get(c.symbol) if sym_id_map else None
    score = score_map.get(symbol_id) if (score_map and symbol_id) else None

    return {
        "id": c.id,
        "candidate_id": c.id,
        "scan_run_id": c.scan_run_id,
        "universe_symbol_id": c.universe_symbol_id,
        "symbol_id": symbol_id,
        "symbol": c.symbol,
        "name": c.name,
        "market": market,
        "region": region,
        "asset_type": c.asset_type,
        "quality_score": c.quality_score,
        "timing_score": c.timing_score,
        "priority_score": c.priority_score,
        # 维度评分分项（从 Score 表预加载，保留消息面 event_score 等）
        "trend_score": score.trend_score if score else None,
        "momentum_score": score.momentum_score if score else None,
        "volatility_score": score.volatility_score if score else None,
        "liquidity_score": score.liquidity_score if score else None,
        "breadth_score": score.breadth_score if score else None,
        "event_score": score.event_score if score else None,
        "data_credibility": score.data_credibility if score else None,
        # 评分配置快照（sync 时从 Score 表快照，用于前端"为什么入选"展示）
        "scoring_preset_key": score.scoring_preset_key if score else None,
        "scoring_preset_name": score.scoring_preset_name if score else None,
        "scoring_config_version": score.scoring_config_version if score else None,
        "dimension_scores_json": c.dimension_scores_json,
        "scoring_config_snapshot_json": c.scoring_config_snapshot_json,
        "stage": c.stage,
        "action": c.action,
        "warning_days": c.warning_days,
        "valid_days": c.valid_days,
        "is_promoted": c.is_promoted,
        "promoted_at": c.promoted_at.isoformat() if c.promoted_at else None,
        "is_legacy": False,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _build_score_snapshot_json(
    candidate: DiscoveryCandidate,
    snapshot_item: DiscoveryScoreSnapshotItem | None,
) -> str:
    """构造 WatchlistItem.score_snapshot_json 的内容（JSON 字符串）。

    参照 spec："候选晋升为观察项或组合成员时把入选评分和来源复制为正式快照"。
    不引用快照 item ID（防止快照清理后引用断裂），仅保存评分副本。

    数据来源优先级：
    1. snapshot_item（WP-P.2 评分快照明细，最权威）
    2. candidate 自身的 quality/timing/priority/dimension_scores_json（sync 时快照）
    """
    payload: dict[str, Any] = {
        "source": "discovery_candidate",
        "candidate_id": candidate.id,
        "scan_run_id": candidate.scan_run_id,
        "symbol": candidate.symbol,
        "stage": candidate.stage,
        "action": candidate.action,
        "warning_days": candidate.warning_days,
        "valid_days": candidate.valid_days,
        "promoted_at": _now().isoformat(),
    }

    if snapshot_item is not None:
        payload.update({
            "quality_score": snapshot_item.quality_score,
            "timing_score": snapshot_item.timing_score,
            "priority_score": snapshot_item.priority_score,
            "dimension_scores_json": snapshot_item.dimension_scores_json,
            "data_credibility": snapshot_item.data_credibility,
            "health_summary_json": snapshot_item.health_summary_json,
            "snapshot_item_id": snapshot_item.id,  # 仅作参考，清理后仍可用其他字段
        })
    else:
        payload.update({
            "quality_score": candidate.quality_score,
            "timing_score": candidate.timing_score,
            "priority_score": candidate.priority_score,
            "dimension_scores_json": candidate.dimension_scores_json,
        })

    return json.dumps(payload, ensure_ascii=False)


def promote_candidate_to_observation(
    db: Session,
    *,
    candidate_id: int,
    watchlist_id: int,
    snapshot_item_id: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """候选晋升为观察项时，把入选评分和来源复制为正式快照（WP-P.7）。

    参照 spec："候选晋升为观察项或组合成员时把入选评分和来源复制为正式快照"。

    实现：
    1. 读取 DiscoveryCandidate
    2. （可选）读取 DiscoveryScoreSnapshotItem，构造 score_snapshot_json
    3. 通过 candidate.symbol 反查 Symbol.id
    4. 创建 WatchlistItem，score_snapshot_json 字段存入快照评分副本
    5. 标记 candidate.is_promoted=1
    6. 单事务写入（参照 project_memory 硬约束：delete+insert 用单事务）

    幂等：若 (watchlist_id, symbol_id) 已存在 WatchlistItem，仅追加/覆盖 score_snapshot_json，
    不创建重复项；candidate.is_promoted 也置为 1。

    Returns:
        {
            "ok": bool,
            "candidate_id": int,
            "watchlist_item_id": int,
            "symbol": str,
            "already_in_watchlist": bool,
        }
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        return {
            "ok": False,
            "error": "candidate not found",
            "candidate_id": candidate_id,
            "watchlist_item_id": None,
            "symbol": None,
            "already_in_watchlist": False,
        }

    # 校验 watchlist 存在
    watchlist = db.get(Watchlist, watchlist_id)
    if watchlist is None:
        return {
            "ok": False,
            "error": "watchlist not found",
            "candidate_id": candidate_id,
            "watchlist_item_id": None,
            "symbol": candidate.symbol,
            "already_in_watchlist": False,
        }

    # 通过 candidate.symbol 反查 Symbol.id
    symbol = db.execute(
        select(Symbol).where(Symbol.symbol == candidate.symbol)
    ).scalars().first()
    if symbol is None:
        return {
            "ok": False,
            "error": "symbol not found in symbols table",
            "candidate_id": candidate_id,
            "watchlist_item_id": None,
            "symbol": candidate.symbol,
            "already_in_watchlist": False,
        }

    # 可选：读取 DiscoveryScoreSnapshotItem
    snapshot_item: DiscoveryScoreSnapshotItem | None = None
    if snapshot_item_id is not None:
        snapshot_item = db.get(DiscoveryScoreSnapshotItem, snapshot_item_id)

    score_snapshot_json = _build_score_snapshot_json(candidate, snapshot_item)

    # 幂等：检查 (watchlist_id, symbol_id) 是否已存在
    existing_item = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == symbol.id,
        )
    ).scalars().first()

    already_in_watchlist = existing_item is not None

    try:
        if existing_item is not None:
            # 已存在：覆盖 score_snapshot_json（追加评分快照）
            existing_item.score_snapshot_json = score_snapshot_json
            if note is not None:
                existing_item.note = note
            watchlist_item = existing_item
        else:
            # 新建 WatchlistItem
            watchlist_item = WatchlistItem(
                watchlist_id=watchlist_id,
                symbol_id=symbol.id,
                note=note,
                score_snapshot_json=score_snapshot_json,
            )
            db.add(watchlist_item)

        # 同事务标记 candidate.is_promoted=1
        if candidate.is_promoted != 1:
            candidate.is_promoted = 1
            candidate.promoted_at = _now()

        # 单事务提交（参照 project_memory：delete+insert 用单事务）
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(watchlist_item)
    logger.info(
        "promote_candidate_to_observation: candidate_id=%d symbol=%s watchlist_id=%d "
        "already_in_watchlist=%s snapshot_item_id=%s",
        candidate_id, candidate.symbol, watchlist_id,
        already_in_watchlist, snapshot_item_id,
    )
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "watchlist_item_id": watchlist_item.id,
        "symbol": candidate.symbol,
        "already_in_watchlist": already_in_watchlist,
    }
