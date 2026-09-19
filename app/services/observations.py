"""正式观察池服务（WP2.2）。

提供富读模型、幂等加入、更新、归档/恢复接口。
所有候选→观察的流转必须经过本服务，单事务写来源与评分快照。

参照 spec line 162-165：
- 候选加入观察时同一事务写来源（origin_id 指向 candidate）与评分快照（score_snapshot_json）
- 同名单同标的重复请求返回已有记录（非 409）
- 历史无来源项标记 legacy/manual_unknown，禁止伪造来源（spec line 176）

富读模式参照 app/services/symbol_relationships.py 的 best-effort 模式：
任一子查询失败时降级（degraded=True）但不抛异常中断整个响应。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.discovery_score_snapshot import DiscoveryScoreSnapshotItem  # noqa: F401 - 评分快照来源（保留 import 供后续扩展直接读取快照明细）
from app.models.portfolio import Portfolio, Position
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem


# origin_type 取值枚举
ORIGIN_MANUAL = "manual"
ORIGIN_CANDIDATE = "candidate"
ORIGIN_SCAN_RESULT = "scan_result"
ORIGIN_ALERT = "alert"
ORIGIN_LEGACY_MANUAL_UNKNOWN = "legacy_manual_unknown"

# status 取值枚举
STATUS_WATCHING = "watching"
STATUS_READY = "ready"
STATUS_INVALID = "invalid"
STATUS_ARCHIVED = "archived"


def _now() -> datetime:
    """当前 UTC 时间（去除时区后缀，与现有模型 default 风格一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class ObservationRich:
    """观察项富读模型（WP2.2）。

    返回标的、最新行情、最新评分、数据健康、来源、组合关系等富信息。
    能回答"从哪里来、为什么加入、加入时多少分、现在什么状态、准备进哪个组合"。
    """
    # 基础
    watchlist_item_id: int
    watchlist_id: int
    watchlist_name: str | None = None
    symbol_id: int = 0
    symbol: str | None = None
    added_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None

    # 来源（WP2 关键：从哪里来、为什么加入）
    origin_type: str = ORIGIN_MANUAL
    origin_id: int | None = None
    reason: dict | None = None  # 从 reason_json 解析

    # 加入时评分快照（WP2 关键：加入时多少分）
    score_snapshot: dict | None = None  # 从 score_snapshot_json 解析

    # 当前状态（WP2 关键：现在什么状态）
    status: str = STATUS_WATCHING
    priority: int = 0
    tags: list[str] = field(default_factory=list)
    note: str | None = None

    # 目标组合（WP2 关键：准备进哪个组合）
    target_portfolio_id: int | None = None
    target_portfolio_name: str | None = None

    # 富读：最新行情
    latest_price: float | None = None
    latest_price_date: str | None = None
    price_change_pct: float | None = None  # 相对加入时

    # 富读：最新评分
    latest_total_score: float | None = None
    latest_quality_score: float | None = None
    latest_timing_score: float | None = None
    latest_score_date: str | None = None

    # 富读：数据健康
    data_credibility: str | None = None
    bar_count: int | None = None

    # 富读：组合关系
    has_position: bool = False
    position_portfolio_name: str | None = None

    # 降级标记
    degraded: bool = False
    degraded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "watchlist_item_id": self.watchlist_item_id,
            "watchlist_id": self.watchlist_id,
            "watchlist_name": self.watchlist_name,
            "symbol_id": self.symbol_id,
            "symbol": self.symbol,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "origin_type": self.origin_type,
            "origin_id": self.origin_id,
            "reason": self.reason,
            "score_snapshot": self.score_snapshot,
            "status": self.status,
            "priority": self.priority,
            "tags": self.tags,
            "note": self.note,
            "target_portfolio_id": self.target_portfolio_id,
            "target_portfolio_name": self.target_portfolio_name,
            "latest_price": self.latest_price,
            "latest_price_date": self.latest_price_date,
            "price_change_pct": self.price_change_pct,
            "latest_total_score": self.latest_total_score,
            "latest_quality_score": self.latest_quality_score,
            "latest_timing_score": self.latest_timing_score,
            "latest_score_date": self.latest_score_date,
            "data_credibility": self.data_credibility,
            "bar_count": self.bar_count,
            "has_position": self.has_position,
            "position_portfolio_name": self.position_portfolio_name,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }


# ----------------------------------------------------------------------------
# Task 2: 幂等加入接口
# ----------------------------------------------------------------------------


def idempotent_add_observation(
    db: Session,
    *,
    watchlist_id: int,
    symbol_id: int,
    origin_type: str = ORIGIN_MANUAL,
    origin_id: int | None = None,
    reason: dict | None = None,
    score_snapshot: dict | None = None,
    priority: int = 0,
    tags: list[str] | None = None,
    target_portfolio_id: int | None = None,
    note: str | None = None,
) -> WatchlistItem:
    """幂等加入观察池（WP2.2 spec Scenario）。

    幂等规则：同 watchlist_id + symbol_id 的记录已存在时：
    - 如果 status != 'archived'：返回已有记录（非 409）
    - 如果 status == 'archived'：恢复为 'watching'（取消归档）

    候选加入观察时（origin_type='candidate'）：
    - 同一事务写来源（origin_id 指向 candidate）与评分快照（score_snapshot_json）
    - 参照 spec line 164："同一事务写入观察项和来源/评分快照"
    """
    # 查找已有记录
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
            existing.updated_at = _now()
            # 更新来源信息（如果提供了新的）
            if origin_type != ORIGIN_MANUAL:
                existing.origin_type = origin_type
            if origin_id is not None:
                existing.origin_id = origin_id
            if reason is not None:
                existing.reason_json = json.dumps(reason, ensure_ascii=False)
            if score_snapshot is not None:
                existing.score_snapshot_json = json.dumps(
                    score_snapshot, ensure_ascii=False
                )
            db.commit()
        return existing

    # 新建记录
    item = WatchlistItem(
        watchlist_id=watchlist_id,
        symbol_id=symbol_id,
        origin_type=origin_type,
        origin_id=origin_id,
        reason_json=json.dumps(reason, ensure_ascii=False) if reason else None,
        score_snapshot_json=(
            json.dumps(score_snapshot, ensure_ascii=False) if score_snapshot else None
        ),
        status=STATUS_WATCHING,
        priority=priority,
        tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
        target_portfolio_id=target_portfolio_id,
        note=note,
        added_at=_now(),
    )
    db.add(item)
    db.flush()  # 获取自增 ID，但不提交事务
    item_id = item.id
    db.commit()
    # 重新查询以确保返回的 instance 处于正常 session 状态
    # （直接 db.refresh 在某些 session 状态下可能抛 InvalidRequestError）
    refreshed = db.get(WatchlistItem, item_id)
    return refreshed if refreshed is not None else item


# ----------------------------------------------------------------------------
# Task 3: 候选加入观察（单事务）
# ----------------------------------------------------------------------------


def _get_candidate_symbol_id(db: Session, candidate: DiscoveryCandidate) -> int:
    """从候选获取 symbol_id。

    DiscoveryCandidate 通过 symbol 字符串关联 symbols 表
    （universe_symbol_id 指向 universe_symbols，不直接持有 symbols.id），
    需通过 candidate.symbol 反查 Symbol.id。
    """
    # 优先使用 symbol_id 字段（若未来 DiscoveryCandidate 直接持有）
    symbol_id = getattr(candidate, "symbol_id", None)
    if symbol_id is not None:
        return symbol_id
    # 通过 symbol 字符串反查 symbols 表
    symbol_str = getattr(candidate, "symbol", None)
    if symbol_str:
        symbol = db.execute(
            select(Symbol).where(Symbol.symbol == symbol_str)
        ).scalars().first()
        if symbol:
            return symbol.id
    raise ValueError(f"Cannot resolve symbol_id for candidate {candidate.id}")


def _build_candidate_score_snapshot(candidate: DiscoveryCandidate) -> dict[str, Any]:
    """构造候选评分快照 dict（参照 candidate_promote._build_score_snapshot_json）。

    不引用快照 item ID（防止快照清理后引用断裂），仅保存评分副本。
    """
    return {
        "source": "discovery_candidate",
        "candidate_id": candidate.id,
        "scan_run_id": candidate.scan_run_id,
        "symbol": candidate.symbol,
        "quality_score": getattr(candidate, "quality_score", None),
        "timing_score": getattr(candidate, "timing_score", None),
        "priority_score": getattr(candidate, "priority_score", None),
        "scope": getattr(candidate, "scope", None),
        "action": getattr(candidate, "action", None),
        "stage": getattr(candidate, "stage", None),
        "data_credibility": getattr(candidate, "data_credibility", None),
        "captured_at": _now().isoformat(),
    }


def _build_candidate_reason(candidate: DiscoveryCandidate) -> dict[str, Any]:
    """构造候选加入原因 dict。"""
    return {
        "source": "candidate",
        "candidate_id": candidate.id,
        "scan_run_id": getattr(candidate, "scan_run_id", None),
        "scope": getattr(candidate, "scope", None),
        "action": getattr(candidate, "action", None),
        "stage": getattr(candidate, "stage", None),
        "priority_score": getattr(candidate, "priority_score", None),
    }


def add_candidate_to_observation(
    db: Session,
    *,
    candidate_id: int,
    watchlist_id: int,
    note: str | None = None,
    priority: int = 0,
    tags: list[str] | None = None,
    target_portfolio_id: int | None = None,
) -> WatchlistItem:
    """候选加入观察池（spec Scenario "候选加入观察池"）。

    同一事务：
    1. 读取 DiscoveryCandidate（来源）
    2. 构造评分快照（参照 candidate_promote._build_score_snapshot_json 的复制逻辑）
    3. 调用 idempotent_add_observation 写入观察项 + 来源 + 评分快照

    参照 spec line 164："同一事务写入观察项和来源/评分快照"
    参照 spec line 165："同名单同标的重复请求返回已有记录（非 409）"

    不重写已稳定的 candidate_promote.py，仅复用其评分快照复制逻辑思路。
    """
    # 1. 读取候选（使用 db.get 走 identity map，与 candidate_promote.py 一致）
    candidate = db.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"Candidate {candidate_id} not found")

    # 2. 解析 symbol_id（DiscoveryCandidate 通过 symbol 字符串关联 symbols）
    symbol_id = _get_candidate_symbol_id(db, candidate)

    # 3. 构造评分快照与原因（best-effort 复制候选字段）
    score_snapshot = _build_candidate_score_snapshot(candidate)
    reason = _build_candidate_reason(candidate)

    # 4. 幂等加入（单事务写入观察项 + 来源 + 评分快照）
    return idempotent_add_observation(
        db,
        watchlist_id=watchlist_id,
        symbol_id=symbol_id,
        origin_type=ORIGIN_CANDIDATE,
        origin_id=candidate.id,
        reason=reason,
        score_snapshot=score_snapshot,
        priority=priority,
        tags=tags,
        target_portfolio_id=target_portfolio_id,
        note=note,
    )


# ----------------------------------------------------------------------------
# Task 4 & 5: 富读模型 - 子查询实现（best-effort）
# ----------------------------------------------------------------------------


def _query_watchlist_name(db: Session, *, watchlist_id: int) -> str | None:
    """查询 watchlist 名（best-effort）。"""
    wl = db.execute(
        select(Watchlist).where(Watchlist.id == watchlist_id)
    ).scalars().first()
    return wl.name if wl else None


def _query_symbol_code(db: Session, *, symbol_id: int) -> str | None:
    """查询标的代码（best-effort）。"""
    sym = db.execute(
        select(Symbol).where(Symbol.id == symbol_id)
    ).scalars().first()
    return sym.symbol if sym else None


def _query_latest_price(
    db: Session, *, symbol_id: int
) -> tuple[float | None, str | None]:
    """查询最新行情（best-effort）。

    返回 (latest_price, latest_price_date_iso)。
    """
    bar = db.execute(
        select(DailyBar)
        .where(DailyBar.symbol_id == symbol_id)
        .order_by(DailyBar.trade_date.desc())
        .limit(1)
    ).scalars().first()
    if bar is None:
        return None, None
    price = float(bar.close) if bar.close is not None else None
    date_str = bar.trade_date.isoformat() if bar.trade_date else None
    return price, date_str


def _query_latest_score(
    db: Session, *, symbol_id: int
) -> dict[str, Any]:
    """查询最新评分（best-effort）。

    返回 dict，包含 total_score / quality_score / timing_score / score_date。
    Score 模型无 total_score 字段，使用 priority_score 作为总分等价（与
    candidate_promote.py 的 ranking 逻辑一致）。
    """
    score = db.execute(
        select(Score)
        .where(Score.symbol_id == symbol_id)
        .order_by(Score.trade_date.desc())
        .limit(1)
    ).scalars().first()
    if score is None:
        return {}
    return {
        "latest_total_score": (
            float(score.priority_score)
            if getattr(score, "priority_score", None) is not None
            else None
        ),
        "latest_quality_score": (
            float(score.quality_score)
            if getattr(score, "quality_score", None) is not None
            else None
        ),
        "latest_timing_score": (
            float(score.timing_score)
            if getattr(score, "timing_score", None) is not None
            else None
        ),
        "latest_score_date": (
            score.trade_date.isoformat() if score.trade_date else None
        ),
    }


def _query_data_health(
    db: Session, *, symbol_id: int
) -> tuple[int | None, str | None]:
    """查询数据健康（best-effort）。

    返回 (bar_count, data_credibility_label)。
    data_credibility 简单判定：>=250 high，>=60 medium，>0 low，0 时 None（无数据不可评估）。
    """
    count = db.execute(
        select(func.count(DailyBar.id)).where(DailyBar.symbol_id == symbol_id)
    ).scalar()
    if count is None:
        return None, None
    if count == 0:
        return 0, None
    if count >= 250:
        credibility = "high"
    elif count >= 60:
        credibility = "medium"
    else:
        credibility = "low"
    return count, credibility


def _query_position(
    db: Session, *, symbol_id: int
) -> tuple[bool, str | None]:
    """查询持仓关系（best-effort）。

    返回 (has_position, position_portfolio_name)。
    仅查询 quantity != 0 的未平仓持仓，取第一条。
    """
    row = db.execute(
        select(Position, Portfolio)
        .join(Portfolio, Portfolio.id == Position.portfolio_id)
        .where(
            and_(
                Position.symbol_id == symbol_id,
                Position.quantity != 0,
            )
        )
        .limit(1)
    ).first()
    if row is None:
        return False, None
    _pos, portfolio = row
    return True, portfolio.name


def _query_target_portfolio_name(
    db: Session, *, portfolio_id: int
) -> str | None:
    """查询目标组合名（best-effort）。"""
    tp = db.execute(
        select(Portfolio).where(Portfolio.id == portfolio_id)
    ).scalars().first()
    return tp.name if tp else None


def _safe_call(func, *args, **kwargs) -> tuple[Any, str | None]:
    """安全调用子查询，失败时返回 (None, 错误消息) 不抛异常。

    参照 app/services/symbol_relationships.py 的 best-effort 模式。
    """
    try:
        return func(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001 - 子查询失败不中断整体响应
        return None, str(exc)


def _parse_json_field(raw: str | None) -> Any:
    """安全解析 JSON 字段，失败时返回 None。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _parse_tags_field(raw: str | None) -> list[str]:
    """安全解析 tags_json 字段，失败时返回空列表。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:  # noqa: BLE001
        return []


def get_observation_rich(
    db: Session,
    *,
    watchlist_item_id: int,
) -> ObservationRich:
    """获取观察项富读模型。

    返回标的、最新行情、最新评分、数据健康、来源、组合关系等富信息。
    子查询 best-effort：任一失败时 degraded=True 但不抛异常。
    """
    # 1. 读取基础记录
    item = db.execute(
        select(WatchlistItem).where(WatchlistItem.id == watchlist_item_id)
    ).scalars().first()
    if item is None:
        raise ValueError(f"WatchlistItem {watchlist_item_id} not found")

    degraded_reasons: list[str] = []

    # 2. 富读各部分（best-effort）
    watchlist_name, err = _safe_call(
        _query_watchlist_name, db, watchlist_id=item.watchlist_id
    )
    if err:
        degraded_reasons.append(f"watchlist: {err}")

    symbol, err = _safe_call(_query_symbol_code, db, symbol_id=item.symbol_id)
    if err:
        degraded_reasons.append(f"symbol: {err}")

    price_pair, err = _safe_call(_query_latest_price, db, symbol_id=item.symbol_id)
    if err:
        degraded_reasons.append(f"price: {err}")
    latest_price, latest_price_date = price_pair if price_pair else (None, None)

    score_data, err = _safe_call(_query_latest_score, db, symbol_id=item.symbol_id)
    if err:
        degraded_reasons.append(f"score: {err}")
    score_data = score_data or {}

    health_pair, err = _safe_call(_query_data_health, db, symbol_id=item.symbol_id)
    if err:
        degraded_reasons.append(f"health: {err}")
    bar_count, data_credibility = health_pair if health_pair else (None, None)

    pos_pair, err = _safe_call(_query_position, db, symbol_id=item.symbol_id)
    if err:
        degraded_reasons.append(f"position: {err}")
    has_position, position_portfolio_name = pos_pair if pos_pair else (False, None)

    target_portfolio_name: str | None = None
    if item.target_portfolio_id:
        target_portfolio_name, err = _safe_call(
            _query_target_portfolio_name,
            db,
            portfolio_id=item.target_portfolio_id,
        )
        if err:
            degraded_reasons.append(f"target_portfolio: {err}")

    # 3. 解析 JSON 字段（best-effort，失败时返回 None / []）
    reason = _parse_json_field(getattr(item, "reason_json", None))
    score_snapshot = _parse_json_field(getattr(item, "score_snapshot_json", None))
    tags = _parse_tags_field(getattr(item, "tags_json", None))

    return ObservationRich(
        watchlist_item_id=item.id,
        watchlist_id=item.watchlist_id,
        watchlist_name=watchlist_name,
        symbol_id=item.symbol_id,
        symbol=symbol,
        added_at=item.added_at,
        updated_at=getattr(item, "updated_at", None),
        archived_at=getattr(item, "archived_at", None),
        origin_type=item.origin_type,
        origin_id=getattr(item, "origin_id", None),
        reason=reason,
        score_snapshot=score_snapshot,
        status=item.status,
        priority=item.priority,
        tags=tags,
        note=item.note,
        target_portfolio_id=getattr(item, "target_portfolio_id", None),
        target_portfolio_name=target_portfolio_name,
        latest_price=latest_price,
        latest_price_date=latest_price_date,
        price_change_pct=None,  # WP2 后续扩展：相对加入时价格变化
        latest_total_score=score_data.get("latest_total_score"),
        latest_quality_score=score_data.get("latest_quality_score"),
        latest_timing_score=score_data.get("latest_timing_score"),
        latest_score_date=score_data.get("latest_score_date"),
        data_credibility=data_credibility,
        bar_count=bar_count,
        has_position=has_position,
        position_portfolio_name=position_portfolio_name,
        degraded=bool(degraded_reasons),
        degraded_reason="; ".join(degraded_reasons) if degraded_reasons else None,
    )


# ----------------------------------------------------------------------------
# Task 5: 列表富读
# ----------------------------------------------------------------------------


def list_observations_rich(
    db: Session,
    *,
    watchlist_id: int | None = None,
    status: str | None = None,
    origin_type: str | None = None,
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ObservationRich]:
    """列出观察项富读模型。

    支持按 watchlist_id / status / origin_type / tag 筛选。
    默认不返回 archived 状态（除非显式筛选 status='archived'）。

    注：tag 筛选在 Python 层进行（tags_json 是 TEXT 字段，无法直接 SQL 索引）。
    """
    query = select(WatchlistItem)

    conditions = []
    if watchlist_id is not None:
        conditions.append(WatchlistItem.watchlist_id == watchlist_id)
    if status is not None:
        conditions.append(WatchlistItem.status == status)
    else:
        # 默认不返回 archived
        conditions.append(WatchlistItem.status != STATUS_ARCHIVED)
    if origin_type is not None:
        conditions.append(WatchlistItem.origin_type == origin_type)

    if conditions:
        query = query.where(and_(*conditions))

    query = query.order_by(WatchlistItem.added_at.desc()).limit(limit).offset(offset)

    items = db.execute(query).scalars().all()

    # 逐个富读（性能优化：可批量查询，但第一版逐个以简化）
    rich_list = [
        get_observation_rich(db, watchlist_item_id=item.id) for item in items
    ]

    # tag 筛选在 Python 层（tags_json 是 JSON 数组）
    if tag is not None:
        rich_list = [r for r in rich_list if tag in r.tags]

    return rich_list


# ----------------------------------------------------------------------------
# Task 6: 更新 / 归档 / 恢复
# ----------------------------------------------------------------------------


def update_observation(
    db: Session,
    *,
    watchlist_item_id: int,
    priority: int | None = None,
    tags: list[str] | None = None,
    reason: dict | None = None,
    target_portfolio_id: int | None = None,
    note: str | None = None,
    status: str | None = None,
) -> WatchlistItem | None:
    """更新观察项字段。

    仅更新显式传入的非 None 字段（target_portfolio_id 例外，允许设为 None 以清除关联）。
    """
    item = db.execute(
        select(WatchlistItem).where(WatchlistItem.id == watchlist_item_id)
    ).scalars().first()
    if item is None:
        return None

    if priority is not None:
        item.priority = priority
    if tags is not None:
        item.tags_json = json.dumps(tags, ensure_ascii=False)
    if reason is not None:
        item.reason_json = json.dumps(reason, ensure_ascii=False)
    if target_portfolio_id is not None:
        item.target_portfolio_id = target_portfolio_id
    if note is not None:
        item.note = note
    if status is not None:
        item.status = status

    item.updated_at = _now()
    db.commit()
    return item


def archive_observation(
    db: Session,
    *,
    watchlist_item_id: int,
) -> WatchlistItem | None:
    """归档观察项（不物理删除）。

    归档后默认不出现在 list_observations_rich 结果中，但可通过 status='archived' 筛选恢复。
    """
    item = db.execute(
        select(WatchlistItem).where(WatchlistItem.id == watchlist_item_id)
    ).scalars().first()
    if item is None:
        return None

    item.status = STATUS_ARCHIVED
    item.archived_at = _now()
    item.updated_at = _now()
    db.commit()
    return item


def restore_observation(
    db: Session,
    *,
    watchlist_item_id: int,
) -> WatchlistItem | None:
    """恢复归档的观察项。"""
    item = db.execute(
        select(WatchlistItem).where(WatchlistItem.id == watchlist_item_id)
    ).scalars().first()
    if item is None:
        return None

    item.status = STATUS_WATCHING
    item.archived_at = None
    item.updated_at = _now()
    db.commit()
    return item


__all__ = [
    "ObservationRich",
    "ORIGIN_MANUAL",
    "ORIGIN_CANDIDATE",
    "ORIGIN_SCAN_RESULT",
    "ORIGIN_ALERT",
    "ORIGIN_LEGACY_MANUAL_UNKNOWN",
    "STATUS_WATCHING",
    "STATUS_READY",
    "STATUS_INVALID",
    "STATUS_ARCHIVED",
    "idempotent_add_observation",
    "add_candidate_to_observation",
    "get_observation_rich",
    "list_observations_rich",
    "update_observation",
    "archive_observation",
    "restore_observation",
]
