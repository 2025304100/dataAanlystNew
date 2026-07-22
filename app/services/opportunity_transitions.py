"""机会状态流转统一领域服务（WP3.2）。

所有候选→观察→组合→订单的流转必须经过本服务。
单事务写入业务对象 + 审计事件，幂等键防双击/重试。
失败回滚，不出现半状态。

参照 spec line 178-196：
- 所有候选→观察→组合→订单的流转必须经过该服务
- 单事务写入业务对象和关联状态
- 幂等键防止双击/重试产生重复关系
- 失败回滚，不允许出现"候选已晋升但观察项没写成功"的半状态

复用 WP2.2 observations 服务的内部 helper（_get_candidate_symbol_id /
_build_candidate_score_snapshot / _build_candidate_reason），不修改其稳定接口。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.opportunity_transition_event import (
    ACTOR_MIGRATION,
    ACTOR_SYSTEM,
    ACTOR_USER,
    EVENT_CANDIDATE_TO_OBSERVATION,
    EVENT_CANDIDATE_TO_PORTFOLIO,
    EVENT_EXCLUDE,
    EVENT_EXPIRE,
    EVENT_MEMBER_ARCHIVE_TO_OBSERVATION,
    EVENT_OBSERVATION_TO_PORTFOLIO,
    EVENT_RESTORE,
    OpportunityTransitionEvent,
    TYPE_CANDIDATE,
    TYPE_OBSERVATION,
    TYPE_PORTFOLIO_MEMBER,
)
from app.models.watchlist import Watchlist, WatchlistItem
from app.services.observations import (
    _build_candidate_reason,
    _build_candidate_score_snapshot,
    _get_candidate_symbol_id,
)

# 复用 observations 服务的状态常量，避免硬编码
from app.services.observations import STATUS_ARCHIVED, STATUS_WATCHING


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _now_utc() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_idempotency_key(
    event_type: str,
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int | None,
    actor_type: str,
) -> str:
    """构造幂等键。

    规则：event_type:source_type:source_id:target_type:target_id:actor_type
    对于同一来源的同一事件类型，幂等键相同，防双击。
    """
    target_part = str(target_id) if target_id is not None else "none"
    return (
        f"{event_type}:{source_type}:{source_id}:"
        f"{target_type}:{target_part}:{actor_type}"
    )


def _find_event_by_idempotency_key(
    db: Session,
    idempotency_key: str,
) -> OpportunityTransitionEvent | None:
    """按幂等键查找已有事件。"""
    return db.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.idempotency_key == idempotency_key
        )
    ).scalars().first()


def _create_event(
    db: Session,
    *,
    symbol_id: int,
    event_type: str,
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int,
    from_status: str | None = None,
    to_status: str = "",
    reason: dict | None = None,
    idempotency_key: str,
    actor_type: str = ACTOR_USER,
) -> OpportunityTransitionEvent:
    """创建审计事件（不 commit，由调用方控制事务）。"""
    event = OpportunityTransitionEvent(
        symbol_id=symbol_id,
        event_type=event_type,
        source_type=source_type,
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        from_status=from_status,
        to_status=to_status,
        reason_json=json.dumps(reason, ensure_ascii=False) if reason else None,
        idempotency_key=idempotency_key,
        actor_type=actor_type,
        created_at=_now_utc(),
    )
    db.add(event)
    return event


def _upsert_observation_for_candidate(
    db: Session,
    *,
    candidate: DiscoveryCandidate,
    watchlist_id: int,
    symbol_id: int,
    reason: dict | None,
    score_snapshot: dict | None,
    note: str | None,
    priority: int,
    tags: list[str] | None,
    target_portfolio_id: int | None,
) -> WatchlistItem:
    """创建或恢复候选对应的观察项（不 commit，由调用方控制事务）。

    复用 observations.idempotent_add_observation 的幂等逻辑，
    但不调用其内部 commit，保证单事务原子性。

    幂等规则：
    - 同 watchlist_id + symbol_id 已存在且非 archived：返回已有记录
    - 同 watchlist_id + symbol_id 已存在且 archived：恢复为 watching
    - 不存在：新建
    """
    existing = db.execute(
        select(WatchlistItem).where(
            and_(
                WatchlistItem.watchlist_id == watchlist_id,
                WatchlistItem.symbol_id == symbol_id,
            )
        )
    ).scalars().first()

    if existing is not None:
        # 幂等：返回已有记录（非 409）
        if existing.status == STATUS_ARCHIVED:
            # 恢复归档记录
            existing.status = STATUS_WATCHING
            existing.archived_at = None
            existing.updated_at = _now_utc()
            # 更新来源信息（如果提供了新的）
            if existing.origin_type == "manual":
                existing.origin_type = "candidate"
            if existing.origin_id is None:
                existing.origin_id = candidate.id
            if reason is not None:
                existing.reason_json = json.dumps(reason, ensure_ascii=False)
            if score_snapshot is not None:
                existing.score_snapshot_json = json.dumps(
                    score_snapshot, ensure_ascii=False
                )
        return existing

    # 新建记录
    item = WatchlistItem(
        watchlist_id=watchlist_id,
        symbol_id=symbol_id,
        origin_type="candidate",
        origin_id=candidate.id,
        reason_json=json.dumps(reason, ensure_ascii=False) if reason else None,
        score_snapshot_json=(
            json.dumps(score_snapshot, ensure_ascii=False) if score_snapshot else None
        ),
        status=STATUS_WATCHING,
        priority=priority,
        tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
        target_portfolio_id=target_portfolio_id,
        note=note,
        added_at=_now_utc(),
    )
    db.add(item)
    return item


# ----------------------------------------------------------------------------
# Task 2: 候选 → 观察
# ----------------------------------------------------------------------------


def transition_candidate_to_observation(
    db: Session,
    *,
    candidate_id: int,
    watchlist_id: int,
    note: str | None = None,
    priority: int = 0,
    tags: list[str] | None = None,
    target_portfolio_id: int | None = None,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> tuple[WatchlistItem, OpportunityTransitionEvent | None]:
    """候选加入观察池（单事务，幂等）。

    spec Scenario "双击幂等"：
    - 同一候选连续点击两次"加入观察"
    - 只产生 1 个观察项和 1 个成功事件
    - 不返回错误

    spec Scenario "流转原子性"：
    - 任一步骤异常时所有写入回滚
    - 不出现"候选已晋升但观察项没写成功"的半状态

    实现策略：
    1. 读取候选
    2. 构造幂等键（如未提供）
    3. 幂等检查（按 idempotency_key 查找已有事件）
       - 已存在：返回已有观察项 + None（无新事件）
       - 不存在：继续
    4. 单事务内：
       a. 写入观察项 + 来源 + 评分快照（不调用 observations.idempotent_add_observation
          以避免其内部 commit 破坏原子性，改用 _upsert_observation_for_candidate）
       b. 更新 discovery_candidates.is_promoted = True（WP3.3 双轨兼容）
       c. 创建 OpportunityTransitionEvent 审计事件
    5. 任一步骤异常：事务回滚
    """
    # 1. 读取候选
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    # 2. 构造幂等键
    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_CANDIDATE_TO_OBSERVATION,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_id,
            actor_type=actor_type,
        )

    # 3. 幂等检查
    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        # 幂等：返回已有观察项，不创建新事件
        existing_item = db.execute(
            select(WatchlistItem).where(
                and_(
                    WatchlistItem.watchlist_id == watchlist_id,
                    WatchlistItem.origin_type == "candidate",
                    WatchlistItem.origin_id == candidate_id,
                )
            )
        ).scalars().first()
        if existing_item is not None:
            return existing_item, None
        # 边界：事件存在但观察项不存在（不应发生，但防御性处理）
        # 继续创建观察项（但不创建新事件，因为事件已存在）
        # 此处通过 fall-through 到主流程，但主流程会尝试创建事件 → 唯一约束冲突
        # 为避免冲突，直接返回 None 事件
        symbol_id_fallback = _get_candidate_symbol_id(db, candidate)
        item_fallback = _upsert_observation_for_candidate(
            db,
            candidate=candidate,
            watchlist_id=watchlist_id,
            symbol_id=symbol_id_fallback,
            reason=_build_candidate_reason(candidate),
            score_snapshot=_build_candidate_score_snapshot(candidate),
            note=note,
            priority=priority,
            tags=tags,
            target_portfolio_id=target_portfolio_id,
        )
        try:
            db.commit()
            db.refresh(item_fallback)
        except Exception:
            db.rollback()
            raise
        return item_fallback, None

    # 4. 构造评分快照与原因（复用 observations 服务的逻辑）
    symbol_id = _get_candidate_symbol_id(db, candidate)
    score_snapshot = _build_candidate_score_snapshot(candidate)
    reason = _build_candidate_reason(candidate)
    if note:
        reason = {**(reason or {}), "note": note}

    # 5. 单事务：写入观察项 + 更新候选 is_promoted + 创建审计事件
    try:
        # a. 写入观察项（不 commit，由本服务统一控制事务）
        item = _upsert_observation_for_candidate(
            db,
            candidate=candidate,
            watchlist_id=watchlist_id,
            symbol_id=symbol_id,
            reason=reason,
            score_snapshot=score_snapshot,
            note=note,
            priority=priority,
            tags=tags,
            target_portfolio_id=target_portfolio_id,
        )

        # b. 更新候选 is_promoted（WP3.3 双轨兼容）
        candidate.is_promoted = 1
        if hasattr(candidate, "promoted_at"):
            candidate.promoted_at = _now_utc()

        # c. 创建审计事件
        event = _create_event(
            db,
            symbol_id=symbol_id,
            event_type=EVENT_CANDIDATE_TO_OBSERVATION,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_id,
            from_status="candidate",
            to_status="observation",
            reason=reason,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        # d. 单事务 commit
        db.commit()
        db.refresh(item)
        return item, event

    except Exception:
        # 失败回滚：所有写入回滚（包括观察项、候选 is_promoted、审计事件）
        db.rollback()
        raise


# ----------------------------------------------------------------------------
# Task 3: 观察 → 组合
# ----------------------------------------------------------------------------


def transition_observation_to_portfolio(
    db: Session,
    *,
    watchlist_item_id: int,
    portfolio_id: int,
    execution_mode: str = "manual",
    note: str | None = None,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> tuple[WatchlistItem, OpportunityTransitionEvent | None]:
    """观察项加入组合（单事务，幂等）。

    WP4 会实现 portfolio_members 表，本服务提前预留接口。
    第一阶段：只创建审计事件 + 更新观察项 target_portfolio_id。
    WP4 完成后：创建 PortfolioMember 记录。
    """
    # 1. 读取观察项
    item = db.get(WatchlistItem, watchlist_item_id)
    if item is None:
        raise ValueError(f"WatchlistItem {watchlist_item_id} not found")

    # 2. 构造幂等键
    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_OBSERVATION_TO_PORTFOLIO,
            source_type=TYPE_OBSERVATION,
            source_id=watchlist_item_id,
            target_type=TYPE_PORTFOLIO_MEMBER,
            target_id=portfolio_id,
            actor_type=actor_type,
        )

    # 3. 幂等检查
    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        # 幂等：返回已有状态
        return item, None

    # 4. 单事务
    try:
        # a. 更新观察项 target_portfolio_id
        item.target_portfolio_id = portfolio_id
        item.updated_at = _now_utc()

        # b. WP4 预留：创建 PortfolioMember（第一阶段不实现，WP4 完成）
        # member = create_portfolio_member(...)

        # c. 创建审计事件
        reason: dict[str, Any] = {"execution_mode": execution_mode}
        if note:
            reason["note"] = note

        event = _create_event(
            db,
            symbol_id=item.symbol_id,
            event_type=EVENT_OBSERVATION_TO_PORTFOLIO,
            source_type=TYPE_OBSERVATION,
            source_id=watchlist_item_id,
            target_type=TYPE_PORTFOLIO_MEMBER,
            target_id=portfolio_id,
            from_status=item.status,
            to_status="portfolio_member",
            reason=reason,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        db.refresh(item)
        return item, event

    except Exception:
        db.rollback()
        raise


# ----------------------------------------------------------------------------
# Task 4: 候选 → 组合（可选同时加入观察池）
# ----------------------------------------------------------------------------


def transition_candidate_to_portfolio(
    db: Session,
    *,
    candidate_id: int,
    portfolio_id: int,
    watchlist_id: int | None = None,
    execution_mode: str = "manual",
    note: str | None = None,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> tuple[WatchlistItem | None, OpportunityTransitionEvent | None]:
    """候选直接加入组合（单事务，幂等）。

    可选：同时加入观察池（如果 watchlist_id 提供）。
    第一阶段：只创建审计事件 + 更新候选 is_promoted。
    WP4 完成后：创建 PortfolioMember 记录。
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_CANDIDATE_TO_PORTFOLIO,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_PORTFOLIO_MEMBER,
            target_id=portfolio_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        # 幂等：返回已有状态
        return None, None

    try:
        symbol_id = _get_candidate_symbol_id(db, candidate)
        reason: dict[str, Any] = {"execution_mode": execution_mode}
        if note:
            reason["note"] = note

        observation_item: WatchlistItem | None = None

        # 可选：同时加入观察池（使用独立的幂等键，与候选→组合事件解耦）
        if watchlist_id is not None:
            obs_key = _build_idempotency_key(
                event_type=EVENT_CANDIDATE_TO_OBSERVATION,
                source_type=TYPE_CANDIDATE,
                source_id=candidate_id,
                target_type=TYPE_OBSERVATION,
                target_id=watchlist_id,
                actor_type=actor_type,
            )
            existing_obs_event = _find_event_by_idempotency_key(db, obs_key)
            if existing_obs_event is None:
                # 创建观察项 + 候选→观察审计事件（同一事务）
                score_snapshot = _build_candidate_score_snapshot(candidate)
                obs_reason = _build_candidate_reason(candidate)
                if note:
                    obs_reason = {**(obs_reason or {}), "note": note}

                observation_item = _upsert_observation_for_candidate(
                    db,
                    candidate=candidate,
                    watchlist_id=watchlist_id,
                    symbol_id=symbol_id,
                    reason=obs_reason,
                    score_snapshot=score_snapshot,
                    note=note,
                    priority=0,
                    tags=None,
                    target_portfolio_id=portfolio_id,
                )
                _create_event(
                    db,
                    symbol_id=symbol_id,
                    event_type=EVENT_CANDIDATE_TO_OBSERVATION,
                    source_type=TYPE_CANDIDATE,
                    source_id=candidate_id,
                    target_type=TYPE_OBSERVATION,
                    target_id=watchlist_id,
                    from_status="candidate",
                    to_status="observation",
                    reason=obs_reason,
                    idempotency_key=obs_key,
                    actor_type=actor_type,
                )

        # 更新候选 is_promoted
        candidate.is_promoted = 1
        if hasattr(candidate, "promoted_at"):
            candidate.promoted_at = _now_utc()

        # 创建候选→组合审计事件
        event = _create_event(
            db,
            symbol_id=symbol_id,
            event_type=EVENT_CANDIDATE_TO_PORTFOLIO,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_PORTFOLIO_MEMBER,
            target_id=portfolio_id,
            from_status="candidate",
            to_status="portfolio_member",
            reason=reason,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        if observation_item is not None:
            db.refresh(observation_item)
        return observation_item, event

    except Exception:
        db.rollback()
        raise


# ----------------------------------------------------------------------------
# Task 5: 排除 / 恢复 / 过期
# ----------------------------------------------------------------------------


def exclude_candidate(
    db: Session,
    *,
    candidate_id: int,
    reason: str | None = None,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> OpportunityTransitionEvent | None:
    """排除候选（单事务，幂等）。

    DiscoveryCandidate 当前无 status 字段（WP3.3 会扩展），
    第一阶段仅记录审计事件，不修改候选业务字段。
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_EXCLUDE,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_CANDIDATE,
            target_id=candidate_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        return None

    try:
        symbol_id = _get_candidate_symbol_id(db, candidate)

        # WP3.3 双轨兼容：若候选将来扩展 status 字段，则更新
        old_status = None
        if hasattr(candidate, "status"):
            old_status = getattr(candidate, "status", None)
            candidate.status = "excluded"  # type: ignore[attr-defined]

        event = _create_event(
            db,
            symbol_id=symbol_id,
            event_type=EVENT_EXCLUDE,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_CANDIDATE,
            target_id=candidate_id,
            from_status=old_status,
            to_status="excluded",
            reason={"reason": reason} if reason else None,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        db.refresh(event)
        return event

    except Exception:
        db.rollback()
        raise


def restore_candidate(
    db: Session,
    *,
    candidate_id: int,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> OpportunityTransitionEvent | None:
    """恢复排除的候选（单事务，幂等）。

    DiscoveryCandidate 当前无 status 字段（WP3.3 会扩展），
    第一阶段仅记录审计事件。
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_RESTORE,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_CANDIDATE,
            target_id=candidate_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        return None

    try:
        symbol_id = _get_candidate_symbol_id(db, candidate)

        old_status = None
        if hasattr(candidate, "status"):
            old_status = getattr(candidate, "status", None)
            candidate.status = "active"  # type: ignore[attr-defined]

        event = _create_event(
            db,
            symbol_id=symbol_id,
            event_type=EVENT_RESTORE,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_CANDIDATE,
            target_id=candidate_id,
            from_status=old_status,
            to_status="active",
            reason={"action": "restore_candidate"},
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        db.refresh(event)
        return event

    except Exception:
        db.rollback()
        raise


def expire_candidate(
    db: Session,
    *,
    candidate_id: int,
    actor_type: str = ACTOR_SYSTEM,
    idempotency_key: str | None = None,
) -> OpportunityTransitionEvent | None:
    """过期候选（系统触发，单事务，幂等）。

    DiscoveryCandidate 当前无 status 字段（WP3.3 会扩展），
    第一阶段仅记录审计事件。
    """
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_EXPIRE,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_CANDIDATE,
            target_id=candidate_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        return None

    try:
        symbol_id = _get_candidate_symbol_id(db, candidate)

        old_status = None
        if hasattr(candidate, "status"):
            old_status = getattr(candidate, "status", None)
            candidate.status = "expired"  # type: ignore[attr-defined]

        event = _create_event(
            db,
            symbol_id=symbol_id,
            event_type=EVENT_EXPIRE,
            source_type=TYPE_CANDIDATE,
            source_id=candidate_id,
            target_type=TYPE_CANDIDATE,
            target_id=candidate_id,
            from_status=old_status,
            to_status="expired",
            reason={"action": "expire_candidate"},
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        db.refresh(event)
        return event

    except Exception:
        db.rollback()
        raise


def exclude_observation(
    db: Session,
    *,
    watchlist_item_id: int,
    reason: str | None = None,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> OpportunityTransitionEvent | None:
    """排除观察项（归档，单事务，幂等）。

    将观察项 status 置为 'archived'，并记录审计事件。
    复用 observations.archive_observation 的状态变更逻辑，但单事务控制。
    """
    item = db.get(WatchlistItem, watchlist_item_id)
    if item is None:
        raise ValueError(f"WatchlistItem {watchlist_item_id} not found")

    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_EXCLUDE,
            source_type=TYPE_OBSERVATION,
            source_id=watchlist_item_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_item_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        return None

    try:
        old_status = item.status
        # 归档观察项（单事务控制，不调用 observations.archive_observation 的内部 commit）
        item.status = STATUS_ARCHIVED
        item.archived_at = _now_utc()
        item.updated_at = _now_utc()

        event = _create_event(
            db,
            symbol_id=item.symbol_id,
            event_type=EVENT_EXCLUDE,
            source_type=TYPE_OBSERVATION,
            source_id=watchlist_item_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_item_id,
            from_status=old_status,
            to_status=STATUS_ARCHIVED,
            reason={"reason": reason} if reason else None,
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        db.refresh(event)
        return event

    except Exception:
        db.rollback()
        raise


def restore_observation(
    db: Session,
    *,
    watchlist_item_id: int,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> OpportunityTransitionEvent | None:
    """恢复归档的观察项（单事务，幂等）。

    将观察项 status 恢复为 'watching'，并记录审计事件。
    """
    item = db.get(WatchlistItem, watchlist_item_id)
    if item is None:
        raise ValueError(f"WatchlistItem {watchlist_item_id} not found")

    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_RESTORE,
            source_type=TYPE_OBSERVATION,
            source_id=watchlist_item_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_item_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        return None

    try:
        old_status = item.status
        item.status = STATUS_WATCHING
        item.archived_at = None
        item.updated_at = _now_utc()

        event = _create_event(
            db,
            symbol_id=item.symbol_id,
            event_type=EVENT_RESTORE,
            source_type=TYPE_OBSERVATION,
            source_id=watchlist_item_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_item_id,
            from_status=old_status,
            to_status=STATUS_WATCHING,
            reason={"action": "restore_observation"},
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        db.refresh(event)
        return event

    except Exception:
        db.rollback()
        raise


# ----------------------------------------------------------------------------
# Task 6: 组合成员归档后回到观察状态
# ----------------------------------------------------------------------------


def member_archive_to_observation(
    db: Session,
    *,
    portfolio_member_id: int,
    watchlist_id: int,
    symbol_id: int | None = None,
    actor_type: str = ACTOR_USER,
    idempotency_key: str | None = None,
) -> tuple[WatchlistItem | None, OpportunityTransitionEvent | None]:
    """组合成员归档后回到观察状态（单事务，幂等）。

    WP4 会实现 PortfolioMember 表，本服务提前预留接口。
    第一阶段：调用方需提供 symbol_id 以创建/恢复观察项；
              若未提供 symbol_id，则只创建审计事件（symbol_id=0 占位）。
    WP4 完成后：从 PortfolioMember 反查 symbol_id，参数将被忽略。
    """
    if idempotency_key is None:
        idempotency_key = _build_idempotency_key(
            event_type=EVENT_MEMBER_ARCHIVE_TO_OBSERVATION,
            source_type=TYPE_PORTFOLIO_MEMBER,
            source_id=portfolio_member_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_id,
            actor_type=actor_type,
        )

    existing_event = _find_event_by_idempotency_key(db, idempotency_key)
    if existing_event is not None:
        return None, None

    try:
        observation_item: WatchlistItem | None = None

        # 若调用方提供 symbol_id，则创建/恢复观察项
        # WP4 完成后：从 PortfolioMember 反查 symbol_id
        effective_symbol_id = symbol_id if symbol_id is not None else 0

        if symbol_id is not None:
            # 查找已有观察项（同 watchlist + symbol）
            existing_item = db.execute(
                select(WatchlistItem).where(
                    and_(
                        WatchlistItem.watchlist_id == watchlist_id,
                        WatchlistItem.symbol_id == symbol_id,
                    )
                )
            ).scalars().first()

            if existing_item is not None:
                # 恢复归档记录
                if existing_item.status == STATUS_ARCHIVED:
                    existing_item.status = STATUS_WATCHING
                    existing_item.archived_at = None
                    existing_item.updated_at = _now_utc()
                observation_item = existing_item
            else:
                # 新建观察项，来源标记为 portfolio_member
                observation_item = WatchlistItem(
                    watchlist_id=watchlist_id,
                    symbol_id=symbol_id,
                    origin_type="portfolio_member",
                    origin_id=portfolio_member_id,
                    reason_json=json.dumps(
                        {"source": "portfolio_member", "portfolio_member_id": portfolio_member_id},
                        ensure_ascii=False,
                    ),
                    status=STATUS_WATCHING,
                    priority=0,
                    added_at=_now_utc(),
                )
                db.add(observation_item)

        # 创建审计事件
        event = _create_event(
            db,
            symbol_id=effective_symbol_id,
            event_type=EVENT_MEMBER_ARCHIVE_TO_OBSERVATION,
            source_type=TYPE_PORTFOLIO_MEMBER,
            source_id=portfolio_member_id,
            target_type=TYPE_OBSERVATION,
            target_id=watchlist_id,
            from_status="portfolio_member",
            to_status="observation",
            reason={"note": "member archived, back to observation"},
            idempotency_key=idempotency_key,
            actor_type=actor_type,
        )

        db.commit()
        if observation_item is not None:
            db.refresh(observation_item)
        db.refresh(event)
        return observation_item, event

    except Exception:
        db.rollback()
        raise


# ----------------------------------------------------------------------------
# Task 7: 查询审计链
# ----------------------------------------------------------------------------


def get_transition_history(
    db: Session,
    *,
    symbol_id: int | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    event_type: str | None = None,
    actor_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[OpportunityTransitionEvent]:
    """查询流转审计链。

    支持按 symbol_id / source / target / event_type / actor_type 筛选。
    按 created_at 降序返回（最新在前）。

    spec 验收："每个观察项和组合成员都能查看来源链"。
    """
    query = select(OpportunityTransitionEvent)

    conditions = []
    if symbol_id is not None:
        conditions.append(OpportunityTransitionEvent.symbol_id == symbol_id)
    if source_type is not None and source_id is not None:
        conditions.append(
            and_(
                OpportunityTransitionEvent.source_type == source_type,
                OpportunityTransitionEvent.source_id == source_id,
            )
        )
    if target_type is not None and target_id is not None:
        conditions.append(
            and_(
                OpportunityTransitionEvent.target_type == target_type,
                OpportunityTransitionEvent.target_id == target_id,
            )
        )
    if event_type is not None:
        conditions.append(OpportunityTransitionEvent.event_type == event_type)
    if actor_type is not None:
        conditions.append(OpportunityTransitionEvent.actor_type == actor_type)

    if conditions:
        query = query.where(and_(*conditions))

    query = (
        query.order_by(OpportunityTransitionEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    return list(db.execute(query).scalars().all())


__all__ = [
    "transition_candidate_to_observation",
    "transition_observation_to_portfolio",
    "transition_candidate_to_portfolio",
    "exclude_candidate",
    "restore_candidate",
    "expire_candidate",
    "exclude_observation",
    "restore_observation",
    "member_archive_to_observation",
    "get_transition_history",
]
