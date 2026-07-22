"""标的关联状态查询服务（WP1.5）。

提供富读模型，一次查询返回候选/观察/组合成员/持仓/告警五类状态。
任何子查询失败时降级（degraded=True），不抛异常中断整个响应，
保证前端徽标显示"状态未知"而非误报"未加入"。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Tuple

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.alert import AlertEvent, AlertRule
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.portfolio import Portfolio, Position
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.symbol_relationships import (
    AlertRelationship,
    CandidateRelationship,
    ObservationRelationship,
    PortfolioMemberRelationship,
    PositionRelationship,
    SymbolRelationships,
)


def _now_iso() -> str:
    """当前 UTC 时间 ISO 8601 字符串（不含时区后缀，与现有模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _safe_call(func: Callable, *args, **kwargs) -> Tuple:
    """安全调用子查询，失败时返回 (None, 错误消息) 不抛异常。

    用于实现 best-effort 查询：任一子查询失败时记录原因但继续后续查询，
    最终在响应中标记 degraded=True。
    """
    try:
        return func(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001 - 子查询失败不中断整体响应
        return None, str(exc)


def get_symbol_relationships(
    db: Session,
    *,
    symbol_id: int,
) -> SymbolRelationships:
    """查询标的关联状态。

    子查询 best-effort：任一失败时 degraded=True 但不抛异常。
    返回结构包含候选/观察/组合成员/持仓/告警五类状态。
    """
    degraded_reasons: list[str] = []

    # 1. 标的基础信息
    symbol_info, err = _safe_call(_query_symbol_info, db, symbol_id=symbol_id)
    if err:
        degraded_reasons.append(f"symbol: {err}")

    # 2. 候选状态
    candidate_data, err = _safe_call(_query_candidate, db, symbol_id=symbol_id)
    if err:
        degraded_reasons.append(f"candidate: {err}")

    # 3. 观察项状态
    observation_data, err = _safe_call(_query_observation, db, symbol_id=symbol_id)
    if err:
        degraded_reasons.append(f"observation: {err}")

    # 4. 组合成员状态（第一阶段只读，WP4 后填充；当前永远为空）
    portfolio_member_data: dict | None = None  # 暂无 portfolio_members 表

    # 5. 持仓状态
    position_data, err = _safe_call(_query_position, db, symbol_id=symbol_id)
    if err:
        degraded_reasons.append(f"position: {err}")

    # 6. 告警状态
    alert_data, err = _safe_call(_query_alert, db, symbol_id=symbol_id)
    if err:
        degraded_reasons.append(f"alert: {err}")

    return SymbolRelationships(
        symbol_id=symbol_id,
        symbol=symbol_info.get("symbol") if symbol_info else None,
        candidate=CandidateRelationship(**(candidate_data or {})),
        observation=ObservationRelationship(**(observation_data or {})),
        portfolio_member=PortfolioMemberRelationship(**(portfolio_member_data or {})),
        position=PositionRelationship(**(position_data or {})),
        alert=AlertRelationship(**(alert_data or {})),
        fetched_at=_now_iso(),
        degraded=bool(degraded_reasons),
        degraded_reason="; ".join(degraded_reasons) if degraded_reasons else None,
    )


# ── 子查询实现 ─────────────────────────────────────────


def _query_symbol_info(db: Session, *, symbol_id: int) -> dict:
    """查询标的代码与名称。"""
    symbol = db.execute(
        select(Symbol).where(Symbol.id == symbol_id)
    ).scalars().first()
    if symbol is None:
        return {}
    return {"symbol": symbol.symbol}


def _query_candidate(db: Session, *, symbol_id: int) -> dict:
    """查询最新候选状态。

    DiscoveryCandidate 不直接关联 symbols 表（universe_symbol_id 指向 universe_symbols），
    通过 DiscoveryCandidate.symbol 字符串与 Symbol.symbol 匹配。
    可选 join ScanRun 获取 snapshot_id 等缓存元数据。
    """
    # 单条最新候选：按 created_at 倒序取首条
    candidate = db.execute(
        select(DiscoveryCandidate)
        .join(Symbol, Symbol.symbol == DiscoveryCandidate.symbol)
        .where(Symbol.id == symbol_id)
        .order_by(DiscoveryCandidate.created_at.desc())
        .limit(1)
    ).scalars().first()

    if candidate is None:
        return {}

    # 尝试获取关联 ScanRun 的 snapshot_id（best-effort，失败时忽略）
    snapshot_id: int | None = None
    snapshot_generated_at: str | None = None
    if candidate.scan_run_id:
        scan_run = db.execute(
            select(ScanRun).where(ScanRun.id == candidate.scan_run_id)
        ).scalars().first()
        if scan_run is not None:
            snapshot_id = getattr(scan_run, "snapshot_id", None)
            if getattr(scan_run, "finished_at", None):
                snapshot_generated_at = scan_run.finished_at.isoformat()

    return {
        "has_candidate": True,
        "candidate_id": candidate.id,
        "scope": None,  # 当前 DiscoveryCandidate 无 scope 字段（WP2+ 后扩展）
        "stage": candidate.stage,
        "action": candidate.action,
        "priority_score": candidate.priority_score,
        "quality_score": candidate.quality_score,
        "timing_score": candidate.timing_score,
        "data_credibility": None,  # 当前 DiscoveryCandidate 无 data_credibility 字段
        "generated_at": candidate.created_at.isoformat() if candidate.created_at else None,
        "scan_run_id": candidate.scan_run_id,
        "snapshot_id": snapshot_id,
        "snapshot_generated_at": snapshot_generated_at,
    }


def _query_observation(db: Session, *, symbol_id: int) -> dict:
    """查询观察项状态。

    第一阶段 WatchlistItem 仅有基础字段（origin_type/status/priority/tags 等 WP2 后扩展），
    使用 getattr 兼容缺失字段，未扩展时返回 None。
    """
    item = db.execute(
        select(WatchlistItem)
        .where(WatchlistItem.symbol_id == symbol_id)
        .order_by(WatchlistItem.added_at.desc())
        .limit(1)
    ).scalars().first()

    if item is None:
        return {}

    # 查 watchlist 名
    watchlist_name: str | None = None
    if item.watchlist_id:
        wl = db.execute(
            select(Watchlist).where(Watchlist.id == item.watchlist_id)
        ).scalars().first()
        if wl:
            watchlist_name = wl.name

    # 第一阶段默认 watching（WP2 后由 status 字段提供）
    status = getattr(item, "status", None) or "watching"
    origin_type = getattr(item, "origin_type", None) or "legacy_manual_unknown"

    return {
        "has_observation": True,
        "watchlist_id": item.watchlist_id,
        "watchlist_name": watchlist_name,
        "watchlist_item_id": item.id,
        "origin_type": origin_type,
        "status": status,
        "priority": getattr(item, "priority", None),
        "tags": [],  # WP2 后从 tags_json 解析
        "target_portfolio_id": getattr(item, "target_portfolio_id", None),
        "added_at": item.added_at.isoformat() if item.added_at else None,
    }


def _query_position(db: Session, *, symbol_id: int) -> dict:
    """查询持仓状态（多组合可能多条，取第一条作为主持仓展示）。

    仅查询 quantity != 0 的未平仓持仓；已平仓（quantity=0）不显示。
    """
    rows = db.execute(
        select(Position, Portfolio)
        .join(Portfolio, Portfolio.id == Position.portfolio_id)
        .where(
            and_(
                Position.symbol_id == symbol_id,
                Position.quantity != 0,
            )
        )
        .order_by(Position.id.asc())
    ).all()

    if not rows:
        return {}

    pos, portfolio = rows[0]
    return {
        "has_position": True,
        "portfolio_id": pos.portfolio_id,
        "portfolio_name": portfolio.name,
        "position_id": pos.id,
        "quantity": float(pos.quantity) if pos.quantity is not None else None,
        # Position 模型字段为 avg_cost，对外字段名为 cost_price
        "cost_price": float(pos.avg_cost) if pos.avg_cost is not None else None,
        "latest_price": float(pos.latest_price) if pos.latest_price is not None else None,
        "market_value": float(pos.market_value) if pos.market_value is not None else None,
        "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
    }


def _query_alert(db: Session, *, symbol_id: int) -> dict:
    """查询告警状态。

    AlertRule 不直接持有 symbol_id（symbols 存于 config_json），
    AlertEvent 直接持有 symbol_id 与 rule_id，因此以 AlertEvent 为入口反查规则。

    has_active_alert 定义：存在未确认（acknowledged=0）的事件且其规则启用（enabled=1）。
    """
    # 1. 查询该标的未确认的告警事件
    active_events = db.execute(
        select(AlertEvent)
        .where(
            and_(
                AlertEvent.symbol_id == symbol_id,
                AlertEvent.acknowledged == 0,
            )
        )
        .order_by(AlertEvent.created_at.desc())
    ).scalars().all()

    if not active_events:
        return {}

    # 2. 关联规则并过滤启用的规则
    rule_ids_raw = {e.rule_id for e in active_events if e.rule_id is not None}
    enabled_rule_ids: list[int] = []
    if rule_ids_raw:
        enabled_rules = db.execute(
            select(AlertRule.id).where(
                and_(
                    AlertRule.id.in_(rule_ids_raw),
                    AlertRule.enabled == 1,
                )
            )
        ).scalars().all()
        enabled_rule_ids = list(enabled_rules)

    if not enabled_rule_ids:
        return {}

    # 3. 过滤出 enabled_rule_ids 对应的事件
    enabled_set = set(enabled_rule_ids)
    filtered_events = [e for e in active_events if e.rule_id in enabled_set]

    if not filtered_events:
        return {}

    # active_events 已按 created_at 倒序，filtered_events 保留相对顺序，第一条即最新
    latest_event = filtered_events[0]

    return {
        "has_active_alert": True,
        "alert_rule_ids": enabled_rule_ids,
        "active_alert_events": len(filtered_events),
        "latest_alert_severity": getattr(latest_event, "severity", None),
        "latest_alert_at": latest_event.created_at.isoformat() if latest_event.created_at else None,
    }


__all__ = ["get_symbol_relationships"]
