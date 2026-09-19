"""组合成员服务（WP4.2）。

提供 CRUD：创建、查询、更新、归档（默认归档不物理删除，设置 effective_to）。
已持仓成员即使暂停买入也允许卖出规则继续风控退出。
删除成员默认归档，存在持仓时提示选择"仅停止买入"或先卖出。

参照 spec line 331-334：
- 归档默认不物理删除，设置 effective_to
- 已持仓成员即使暂停买入也必须允许卖出规则继续风控退出
- 删除成员默认归档，存在持仓时提示选择"仅停止买入"或先卖出
- 成员归档不会误删持仓

不重写已稳定的 transitions 服务（可复用，但不修改）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.portfolio import Position
from app.models.portfolio_member import (
    EXECUTION_MANUAL,
    PortfolioMember,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PAUSED,
)


def _now_utc() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MemberHasPositionError(Exception):
    """成员存在持仓时不能直接归档，需用户选择"仅停止买入"或先卖出。

    参照 spec line 213-215："用户尝试归档存在持仓的成员时，
    提示并要求选择'仅停止买入'或先卖出，不误删持仓"。
    """

    def __init__(self, portfolio_id: int, symbol_id: int, quantity: float):
        self.portfolio_id = portfolio_id
        self.symbol_id = symbol_id
        self.quantity = quantity
        super().__init__(
            f"成员 (portfolio_id={portfolio_id}, symbol_id={symbol_id}) 存在持仓 {quantity}，"
            f"请选择'仅停止买入'(status=paused)或先卖出"
        )


# ----------------------------------------------------------------------------
# Task 1: 创建成员
# ----------------------------------------------------------------------------


def create_member(
    db: Session,
    *,
    portfolio_id: int,
    symbol_id: int,
    status: str = STATUS_ACTIVE,
    execution_mode: str = EXECUTION_MANUAL,
    source_type: str = "manual",
    source_id: int | None = None,
    entry_rule_version_id: int | None = None,
    exit_rule_version_id: int | None = None,
    effective_from: datetime | None = None,
    manual_lock: bool = False,
    priority: int = 0,
    note: str | None = None,
) -> PortfolioMember:
    """创建组合成员。

    约束（spec line 199）："同一组合同一标的只能存在一条当前有效成员关系"。
    如果已存在有效成员（effective_to IS NULL），抛出 ValueError。
    """
    # 检查是否已有有效成员
    existing = db.execute(
        select(PortfolioMember).where(
            and_(
                PortfolioMember.portfolio_id == portfolio_id,
                PortfolioMember.symbol_id == symbol_id,
                PortfolioMember.effective_to.is_(None),
            )
        )
    ).scalars().first()

    if existing is not None:
        raise ValueError(
            f"成员已存在 (portfolio_id={portfolio_id}, symbol_id={symbol_id}, "
            f"id={existing.id})"
        )

    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=execution_mode,
        source_type=source_type,
        source_id=source_id,
        entry_rule_version_id=entry_rule_version_id,
        exit_rule_version_id=exit_rule_version_id,
        effective_from=effective_from or _now_utc(),
        manual_lock=manual_lock,
        priority=priority,
        note=note,
        created_at=_now_utc(),
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


# ----------------------------------------------------------------------------
# Task 2: 查询成员
# ----------------------------------------------------------------------------


def get_member(db: Session, *, member_id: int) -> PortfolioMember | None:
    """获取成员。"""
    return db.get(PortfolioMember, member_id)


def get_active_member(
    db: Session,
    *,
    portfolio_id: int,
    symbol_id: int,
) -> PortfolioMember | None:
    """获取组合同标的的当前有效成员（effective_to IS NULL）。

    参照 spec line 199："同一组合同一标的只能存在一条当前有效成员关系"。
    """
    return db.execute(
        select(PortfolioMember).where(
            and_(
                PortfolioMember.portfolio_id == portfolio_id,
                PortfolioMember.symbol_id == symbol_id,
                PortfolioMember.effective_to.is_(None),
            )
        )
    ).scalars().first()


def list_members(
    db: Session,
    *,
    portfolio_id: int | None = None,
    symbol_id: int | None = None,
    status: str | None = None,
    source_type: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[PortfolioMember]:
    """列出成员。

    默认不返回归档成员（effective_to IS NOT NULL），除非 include_archived=True。
    """
    query = select(PortfolioMember)

    conditions = []
    if portfolio_id is not None:
        conditions.append(PortfolioMember.portfolio_id == portfolio_id)
    if symbol_id is not None:
        conditions.append(PortfolioMember.symbol_id == symbol_id)
    if status is not None:
        conditions.append(PortfolioMember.status == status)
    if source_type is not None:
        conditions.append(PortfolioMember.source_type == source_type)
    if not include_archived:
        conditions.append(PortfolioMember.effective_to.is_(None))

    if conditions:
        query = query.where(and_(*conditions))

    query = (
        query.order_by(PortfolioMember.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.execute(query).scalars().all())


# ----------------------------------------------------------------------------
# Task 3: 更新成员
# ----------------------------------------------------------------------------


def update_member(
    db: Session,
    *,
    member_id: int,
    status: str | None = None,
    execution_mode: str | None = None,
    entry_rule_version_id: int | None = None,
    exit_rule_version_id: int | None = None,
    manual_lock: bool | None = None,
    priority: int | None = None,
    note: str | None = None,
) -> PortfolioMember | None:
    """更新成员字段。

    仅更新显式传入的非 None 字段（manual_lock / priority 例外，允许显式传入
    False / 0 等假值）。
    """
    member = db.get(PortfolioMember, member_id)
    if member is None:
        return None

    if status is not None:
        member.status = status
    if execution_mode is not None:
        member.execution_mode = execution_mode
    if entry_rule_version_id is not None:
        member.entry_rule_version_id = entry_rule_version_id
    if exit_rule_version_id is not None:
        member.exit_rule_version_id = exit_rule_version_id
    if manual_lock is not None:
        member.manual_lock = manual_lock
    if priority is not None:
        member.priority = priority
    if note is not None:
        member.note = note

    member.updated_at = _now_utc()
    db.commit()
    return member


# ----------------------------------------------------------------------------
# Task 4: 持仓检查
# ----------------------------------------------------------------------------


def has_position(
    db: Session, *, portfolio_id: int, symbol_id: int
) -> tuple[bool, float]:
    """检查组合同标的是否有持仓。

    返回 (has_position, quantity)。仅查询 quantity != 0 的未平仓持仓。
    """
    pos = db.execute(
        select(Position).where(
            and_(
                Position.portfolio_id == portfolio_id,
                Position.symbol_id == symbol_id,
                Position.quantity != 0,
            )
        )
    ).scalars().first()

    if pos is None:
        return False, 0.0
    return True, float(pos.quantity)


# ----------------------------------------------------------------------------
# Task 5: 归档 / 暂停 / 恢复
# ----------------------------------------------------------------------------


def archive_member(
    db: Session,
    *,
    member_id: int,
    force: bool = False,
) -> PortfolioMember | None:
    """归档成员（设置 effective_to，不物理删除）。

    参照 spec line 333："删除成员默认归档，存在持仓时提示选择'仅停止买入'
    或先卖出"。

    参数：
        force: True 时即使有持仓也强制归档（用于先卖出后归档的场景）。

    异常：
        MemberHasPositionError: 成员存在持仓且 force=False 时抛出。

    注：归档只修改 PortfolioMember 记录，不删除 Position（spec line 334
    "成员归档不会误删持仓"）。
    """
    member = db.get(PortfolioMember, member_id)
    if member is None:
        return None

    # 检查持仓（force=False 时）
    if not force:
        has_pos, qty = has_position(
            db,
            portfolio_id=member.portfolio_id,
            symbol_id=member.symbol_id,
        )
        if has_pos:
            raise MemberHasPositionError(
                member.portfolio_id, member.symbol_id, qty
            )

    # 归档：设置 effective_to + status=archived（不物理删除）
    member.effective_to = _now_utc()
    member.status = STATUS_ARCHIVED
    member.updated_at = _now_utc()
    db.commit()
    return member


def pause_member(db: Session, *, member_id: int) -> PortfolioMember | None:
    """暂停成员（仅停止买入，卖出规则继续风控退出）。

    参照 spec line 332："已持仓成员即使暂停买入也必须允许卖出规则继续风控退出"。

    pause 不归档：effective_to 仍为 None，成员关系仍有效，仅 status='paused'。
    Position 不受影响（卖出规则继续工作）。
    """
    return update_member(db, member_id=member_id, status=STATUS_PAUSED)


def restore_member(db: Session, *, member_id: int) -> PortfolioMember | None:
    """恢复归档的成员。

    如果同一组合同标的已有新的有效成员，抛出 ValueError
    （参照 spec line 199 唯一性约束）。
    """
    member = db.get(PortfolioMember, member_id)
    if member is None:
        return None

    # 检查是否已有新的有效成员
    existing = get_active_member(
        db,
        portfolio_id=member.portfolio_id,
        symbol_id=member.symbol_id,
    )
    if existing is not None and existing.id != member.id:
        raise ValueError(
            f"已有新的有效成员 (id={existing.id})，不能恢复旧成员 (id={member_id})"
        )

    member.effective_to = None
    member.status = STATUS_ACTIVE
    member.updated_at = _now_utc()
    db.commit()
    return member


__all__ = [
    "MemberHasPositionError",
    "create_member",
    "get_member",
    "get_active_member",
    "list_members",
    "update_member",
    "has_position",
    "archive_member",
    "pause_member",
    "restore_member",
]
