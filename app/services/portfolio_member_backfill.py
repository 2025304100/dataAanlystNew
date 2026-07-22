"""持仓回填服务（WP4.3）。

对每条现有 positions 创建 portfolio_members，source_type=legacy_position。
effective_from 优先取 Position.opened_at，缺失时取迁移时间并标记日期不确定。
不改变 Position ID、数量、成本、最新价、持仓比例。
无持仓组合不凭最新扫描结果自动创建成员。
回填脚本可重复运行，使用组合+标的幂等检查。

参照 spec line 201-205 "持仓回填" Scenario：
- 对每条现有 positions 创建成员，source_type=legacy_position
- effective_from 优先取 Position.opened_at
- Position ID、数量、成本、最新价和持仓比例完全不变
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    EXECUTION_MANUAL,
    PortfolioMember,
    SOURCE_LEGACY_POSITION,
    STATUS_ACTIVE,
)
from app.services.portfolio_members import create_member, get_active_member


def _now_utc() -> datetime:
    """当前 UTC 时间（naive，与项目其他模型一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def backfill_positions_to_members(
    db: Session,
    *,
    portfolio_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """回填持仓为组合成员（WP4.3）。

    对每条现有 positions 创建成员：
    - source_type=legacy_position
    - effective_from 优先取 Position.opened_at，缺失时取迁移时间
    - 不改变 Position 任何字段

    幂等：使用组合+标的检查，已有有效成员的跳过（不覆盖用户手动创建的成员）。

    参数：
        portfolio_id: 指定组合 ID，None=所有组合
        dry_run: True 时只返回统计，不实际写入

    返回：
        {
            "total_positions": N,
            "created_members": N,
            "skipped_existing": N,
            "skipped_zero_quantity": N,
            "errors": [...],
        }
    """
    stats = {
        "total_positions": 0,
        "created_members": 0,
        "skipped_existing": 0,
        "skipped_zero_quantity": 0,
        "errors": [],
    }

    # 1. 查询所有未平仓持仓（quantity != 0）
    #    spec line 339："无持仓组合不凭最新扫描结果自动创建成员"
    #    quantity=0 的视为无持仓，跳过
    query = select(Position).where(Position.quantity != 0)
    if portfolio_id is not None:
        query = query.where(Position.portfolio_id == portfolio_id)

    positions = db.execute(query).scalars().all()
    stats["total_positions"] = len(positions)

    for pos in positions:
        try:
            # 2. 幂等检查：组合+标的已有有效成员则跳过
            #    spec line 340："回填脚本可重复运行，使用组合+标的幂等检查"
            existing = get_active_member(
                db, portfolio_id=pos.portfolio_id, symbol_id=pos.symbol_id
            )
            if existing is not None:
                # 不覆盖用户手动创建的成员（无论 source_type 是什么都跳过）
                stats["skipped_existing"] += 1
                continue

            # 3. 确定 effective_from
            #    spec line 204："effective_from 优先取 Position.opened_at"
            #    spec line 337："缺失时取迁移时间并标记日期不确定"
            effective_from: datetime
            date_uncertain = False
            if pos.opened_at is not None:
                effective_from = pos.opened_at
            else:
                effective_from = _now_utc()
                date_uncertain = True

            # 4. 构造备注（含日期不确定标记）
            note_parts = ["WP4.3 持仓回填"]
            if date_uncertain:
                note_parts.append("opened_at 缺失，effective_from 取迁移时间")
            note = "; ".join(note_parts)

            if dry_run:
                stats["created_members"] += 1
                continue

            # 5. 创建成员
            #    spec line 203："source_type=legacy_position"
            #    spec line 205："Position ID、数量、成本、最新价、持仓比例完全不变"
            #    source_id 指向 Position.id（不修改 Position 本身）
            create_member(
                db,
                portfolio_id=pos.portfolio_id,
                symbol_id=pos.symbol_id,
                status=STATUS_ACTIVE,
                execution_mode=EXECUTION_MANUAL,
                source_type=SOURCE_LEGACY_POSITION,
                source_id=pos.id,
                effective_from=effective_from,
                priority=0,
                note=note,
            )
            stats["created_members"] += 1

        except Exception as e:
            # 错误收集，不中断后续回填
            stats["errors"].append(
                {
                    "position_id": pos.id,
                    "portfolio_id": pos.portfolio_id,
                    "symbol_id": pos.symbol_id,
                    "error": str(e),
                }
            )

    return stats


def verify_backfill(db: Session, *, portfolio_id: int | None = None) -> dict:
    """验证回填结果。

    检查：
    1. 每条未平仓持仓都有对应的 legacy_position 有效成员
    2. Position 字段不被回填修改（实际字段比对在测试中完成）

    参数：
        portfolio_id: 指定组合 ID，None=所有组合

    返回：
        {
            "positions_count": N,
            "members_count": N,
            "matched": N,
            "missing_members": [...],
            "position_fields_unchanged": bool,
        }
    """
    result = {
        "positions_count": 0,
        "members_count": 0,
        "matched": 0,
        "missing_members": [],
        "position_fields_unchanged": True,
    }

    # 1. 查询所有未平仓持仓
    query = select(Position).where(Position.quantity != 0)
    if portfolio_id is not None:
        query = query.where(Position.portfolio_id == portfolio_id)
    positions = db.execute(query).scalars().all()
    result["positions_count"] = len(positions)

    # 2. 查询 legacy_position 有效成员
    member_query = select(PortfolioMember).where(
        and_(
            PortfolioMember.source_type == SOURCE_LEGACY_POSITION,
            PortfolioMember.effective_to.is_(None),
        )
    )
    if portfolio_id is not None:
        member_query = member_query.where(
            PortfolioMember.portfolio_id == portfolio_id
        )
    members = db.execute(member_query).scalars().all()
    result["members_count"] = len(members)

    # 3. 匹配检查：每条持仓都应有对应的 legacy_position 成员
    member_map = {(m.portfolio_id, m.symbol_id): m for m in members}
    for pos in positions:
        key = (pos.portfolio_id, pos.symbol_id)
        if key in member_map:
            result["matched"] += 1
            # Position 字段比对在测试中完成（这里只检查成员存在性）
        else:
            result["missing_members"].append(
                {
                    "position_id": pos.id,
                    "portfolio_id": pos.portfolio_id,
                    "symbol_id": pos.symbol_id,
                }
            )

    return result


__all__ = [
    "backfill_positions_to_members",
    "verify_backfill",
]
