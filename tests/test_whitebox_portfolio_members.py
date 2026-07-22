"""白盒测试 - WP4.2 组合成员服务。

覆盖 app/services/portfolio_members.py 的所有接口：
1. create_member 基本流程 + 默认值
2. 同一组合同一标的唯一性（ValueError）
3. 归档后可重新创建
4. archive_member 基本流程（effective_to 非空，status='archived'）
5. archive_member 存在持仓时抛出 MemberHasPositionError
6. archive_member force=True 强制归档（Position 不被删除）
7. pause_member 暂停买入（status='paused'，effective_to 仍为 None）
8. 已暂停成员仍有持仓（Position 不变）
9. restore_member 恢复归档
10. restore_member 已有新成员时抛出 ValueError
11. list_members 默认不含归档
12. list_members include_archived=True
13. list_members 按 portfolio_id 筛选
14. update_member 更新字段
15. has_position 检查
16. get_active_member
17. manual_lock 字段
18. 成员归档不会误删持仓（spec 验收）

测试用 SQLite 内存库（db_session fixture），每个用例独立 session。

参照 spec line 331-334 的硬约束：
- 归档默认不物理删除，设置 effective_to
- 已持仓成员即使暂停买入也必须允许卖出规则继续风控退出
- 删除成员默认归档，存在持仓时提示选择"仅停止买入"或先卖出
- 成员归档不会误删持仓
"""
from __future__ import annotations

import pytest

from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    EXECUTION_CONFIRM,
    EXECUTION_MANUAL,
    PortfolioMember,
    SOURCE_CANDIDATE,
    SOURCE_LEGACY_POSITION,
    SOURCE_MANUAL,
    SOURCE_OBSERVATION,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PAUSED,
)
from app.models.symbol import Symbol
from app.services import portfolio_members as pm_svc


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_portfolio(db_session, name: str = "QA-PM-PF") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="cash",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


def _make_symbol(db_session, symbol: str = "600000") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market="cn",
        theme="测试",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_position(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    quantity: float = 100.0,
    asset_type: str = "stock",
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=10.0,
        latest_price=12.0,
        market_value=1200.0,
        position_pct=0.1,
        asset_type=asset_type,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


# ----------------------------------------------------------------------------
# 1. create_member 基本流程
# ----------------------------------------------------------------------------


def test_create_member_basic(db_session):
    """【WP4.2】创建成员基础字段正确写入。"""
    pf = _make_portfolio(db_session, name="QA-Create-PF")
    sym = _make_symbol(db_session, symbol="600100")

    member = pm_svc.create_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        source_type=SOURCE_MANUAL,
        note="测试成员",
    )

    assert member.id is not None
    assert member.portfolio_id == pf.id
    assert member.symbol_id == sym.id
    assert member.status == STATUS_ACTIVE
    assert member.execution_mode == EXECUTION_MANUAL
    assert member.source_type == SOURCE_MANUAL
    assert member.source_id is None
    assert member.entry_rule_version_id is None
    assert member.exit_rule_version_id is None
    assert member.effective_from is not None
    assert member.effective_to is None
    assert member.manual_lock is False
    assert member.priority == 0
    assert member.note == "测试成员"
    assert member.created_at is not None


def test_create_member_with_all_fields(db_session):
    """【WP4.2】创建成员时可指定所有字段。"""
    pf = _make_portfolio(db_session, name="QA-Create-All-PF")
    sym = _make_symbol(db_session, symbol="600101")

    member = pm_svc.create_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        status=STATUS_PAUSED,
        execution_mode=EXECUTION_CONFIRM,
        source_type=SOURCE_CANDIDATE,
        source_id=99,
        entry_rule_version_id=1001,
        exit_rule_version_id=1002,
        manual_lock=True,
        priority=7,
        note="完整字段",
    )

    assert member.status == STATUS_PAUSED
    assert member.execution_mode == EXECUTION_CONFIRM
    assert member.source_type == SOURCE_CANDIDATE
    assert member.source_id == 99
    assert member.entry_rule_version_id == 1001
    assert member.exit_rule_version_id == 1002
    assert member.manual_lock is True
    assert member.priority == 7
    assert member.note == "完整字段"


# ----------------------------------------------------------------------------
# 2. 同一组合同一标的唯一性
# ----------------------------------------------------------------------------


def test_create_member_duplicate_raises_value_error(db_session):
    """【WP4.2】同一组合同一标的已存在有效成员时抛出 ValueError。

    参照 spec line 199："同一组合同一标的只能存在一条当前有效成员关系"。
    """
    pf = _make_portfolio(db_session, name="QA-Dup-PF")
    sym = _make_symbol(db_session, symbol="600200")

    pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    with pytest.raises(ValueError, match="成员已存在"):
        pm_svc.create_member(
            db_session, portfolio_id=pf.id, symbol_id=sym.id
        )


def test_create_member_different_portfolio_same_symbol_allowed(db_session):
    """【WP4.2】不同组合可以有相同标的的有效成员。"""
    pf1 = _make_portfolio(db_session, name="QA-Dup-PF1")
    pf2 = _make_portfolio(db_session, name="QA-Dup-PF2")
    sym = _make_symbol(db_session, symbol="600201")

    m1 = pm_svc.create_member(
        db_session, portfolio_id=pf1.id, symbol_id=sym.id
    )
    m2 = pm_svc.create_member(
        db_session, portfolio_id=pf2.id, symbol_id=sym.id
    )

    assert m1.id != m2.id


# ----------------------------------------------------------------------------
# 3. 归档后可重新创建
# ----------------------------------------------------------------------------


def test_archive_then_recreate_allowed(db_session):
    """【WP4.2】归档旧成员后可重新创建新有效成员。"""
    pf = _make_portfolio(db_session, name="QA-Recreate-PF")
    sym = _make_symbol(db_session, symbol="600300")

    member_a = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pm_svc.archive_member(db_session, member_id=member_a.id)

    member_b = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    assert member_b.id != member_a.id
    assert member_b.effective_to is None
    assert member_b.status == STATUS_ACTIVE


# ----------------------------------------------------------------------------
# 4. archive_member 基本流程
# ----------------------------------------------------------------------------


def test_archive_member_sets_effective_to_and_status(db_session):
    """【WP4.2】归档设置 effective_to 非空且 status='archived'。"""
    pf = _make_portfolio(db_session, name="QA-Archive-PF")
    sym = _make_symbol(db_session, symbol="600400")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert member.effective_to is None
    assert member.status == STATUS_ACTIVE

    archived = pm_svc.archive_member(db_session, member_id=member.id)

    assert archived is not None
    assert archived.effective_to is not None
    assert archived.status == STATUS_ARCHIVED
    assert archived.updated_at is not None


def test_archive_member_not_found_returns_none(db_session):
    """【WP4.2】归档不存在的成员返回 None。"""
    result = pm_svc.archive_member(db_session, member_id=99999)
    assert result is None


# ----------------------------------------------------------------------------
# 5. archive_member 存在持仓时抛出异常
# ----------------------------------------------------------------------------


def test_archive_member_with_position_raises(db_session):
    """【WP4.2】归档存在持仓的成员抛出 MemberHasPositionError。

    参照 spec line 213-215："用户尝试归档存在持仓的成员时，
    提示并要求选择'仅停止买入'或先卖出"。
    """
    pf = _make_portfolio(db_session, name="QA-Archive-Pos-PF")
    sym = _make_symbol(db_session, symbol="600500")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )

    with pytest.raises(pm_svc.MemberHasPositionError) as exc_info:
        pm_svc.archive_member(db_session, member_id=member.id, force=False)

    # 异常信息含持仓数量
    assert exc_info.value.portfolio_id == pf.id
    assert exc_info.value.symbol_id == sym.id
    assert exc_info.value.quantity == 100.0

    # 成员未被归档（effective_to 仍为 None）
    db_session.refresh(member)
    assert member.effective_to is None
    assert member.status == STATUS_ACTIVE


# ----------------------------------------------------------------------------
# 6. archive_member force=True 强制归档
# ----------------------------------------------------------------------------


def test_archive_member_force_with_position_succeeds(db_session):
    """【WP4.2】force=True 时即使有持仓也强制归档（用于先卖出后归档场景）。"""
    pf = _make_portfolio(db_session, name="QA-Archive-Force-PF")
    sym = _make_symbol(db_session, symbol="600600")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pos = _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )

    archived = pm_svc.archive_member(
        db_session, member_id=member.id, force=True
    )

    assert archived is not None
    assert archived.effective_to is not None
    assert archived.status == STATUS_ARCHIVED

    # Position 未被删除（spec line 334：成员归档不会误删持仓）
    db_session.refresh(pos)
    assert pos.quantity == 100.0


# ----------------------------------------------------------------------------
# 7. pause_member 暂停买入
# ----------------------------------------------------------------------------


def test_pause_member_sets_status_not_effective_to(db_session):
    """【WP4.2】pause_member 设置 status='paused' 但 effective_to 仍为 None。

    参照 spec line 332："已持仓成员即使暂停买入也必须允许卖出规则继续风控退出"。
    pause 不归档：成员关系仍有效，仅状态变更。
    """
    pf = _make_portfolio(db_session, name="QA-Pause-PF")
    sym = _make_symbol(db_session, symbol="600700")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    paused = pm_svc.pause_member(db_session, member_id=member.id)

    assert paused is not None
    assert paused.status == STATUS_PAUSED
    # effective_to 仍为 None（不是归档）
    assert paused.effective_to is None


def test_pause_member_not_found_returns_none(db_session):
    """【WP4.2】暂停不存在的成员返回 None。"""
    result = pm_svc.pause_member(db_session, member_id=99999)
    assert result is None


# ----------------------------------------------------------------------------
# 8. 已暂停成员仍有持仓
# ----------------------------------------------------------------------------


def test_paused_member_keeps_position(db_session):
    """【WP4.2】已暂停成员的持仓不变（卖出规则继续风控退出）。

    参照 spec line 332："已持仓成员即使暂停买入也必须允许卖出规则继续风控退出"。
    """
    pf = _make_portfolio(db_session, name="QA-Pause-Pos-PF")
    sym = _make_symbol(db_session, symbol="600800")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pos = _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=200.0
    )

    pm_svc.pause_member(db_session, member_id=member.id)

    # Position 不变
    db_session.refresh(pos)
    assert pos.quantity == 200.0

    # 成员状态变更但 effective_to 仍为 None
    db_session.refresh(member)
    assert member.status == STATUS_PAUSED
    assert member.effective_to is None


# ----------------------------------------------------------------------------
# 9. restore_member 恢复归档
# ----------------------------------------------------------------------------


def test_restore_member_resets_status_and_effective_to(db_session):
    """【WP4.2】恢复归档成员设置 status='active' 且 effective_to=None。"""
    pf = _make_portfolio(db_session, name="QA-Restore-PF")
    sym = _make_symbol(db_session, symbol="600900")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pm_svc.archive_member(db_session, member_id=member.id)

    restored = pm_svc.restore_member(db_session, member_id=member.id)

    assert restored is not None
    assert restored.status == STATUS_ACTIVE
    assert restored.effective_to is None
    assert restored.updated_at is not None


def test_restore_member_not_found_returns_none(db_session):
    """【WP4.2】恢复不存在的成员返回 None。"""
    result = pm_svc.restore_member(db_session, member_id=99999)
    assert result is None


# ----------------------------------------------------------------------------
# 10. restore_member 已有新成员时抛出异常
# ----------------------------------------------------------------------------


def test_restore_member_blocked_when_new_active_exists(db_session):
    """【WP4.2】恢复归档成员时若已有新的有效成员，抛出 ValueError。

    参照 spec line 199："同一组合同一标的只能存在一条当前有效成员关系"。
    """
    pf = _make_portfolio(db_session, name="QA-Restore-Block-PF")
    sym = _make_symbol(db_session, symbol="601000")

    member_a = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pm_svc.archive_member(db_session, member_id=member_a.id)

    # 创建新成员 B
    pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    # 恢复 A 应抛出 ValueError
    with pytest.raises(ValueError, match="已有新的有效成员"):
        pm_svc.restore_member(db_session, member_id=member_a.id)


# ----------------------------------------------------------------------------
# 11. list_members 默认不含归档
# ----------------------------------------------------------------------------


def test_list_members_excludes_archived_by_default(db_session):
    """【WP4.2】list_members 默认不返回归档成员。"""
    pf = _make_portfolio(db_session, name="QA-List-Default-PF")

    # 创建 3 个标的，3 个成员
    sym_ids = []
    for i in range(3):
        sym = _make_symbol(db_session, symbol=f"60100{i}")
        sym_ids.append(sym.id)
        pm_svc.create_member(
            db_session, portfolio_id=pf.id, symbol_id=sym.id
        )

    # 归档 1 个
    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 3
    pm_svc.archive_member(db_session, member_id=members[0].id)

    # 默认只返回 2 个 active
    result = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(result) == 2
    for m in result:
        assert m.effective_to is None


# ----------------------------------------------------------------------------
# 12. list_members include_archived=True
# ----------------------------------------------------------------------------


def test_list_members_includes_archived_when_requested(db_session):
    """【WP4.2】include_archived=True 时返回所有成员含归档。"""
    pf = _make_portfolio(db_session, name="QA-List-Included-PF")

    sym_ids = []
    for i in range(3):
        sym = _make_symbol(db_session, symbol=f"60110{i}")
        sym_ids.append(sym.id)
        pm_svc.create_member(
            db_session, portfolio_id=pf.id, symbol_id=sym.id
        )

    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    pm_svc.archive_member(db_session, member_id=members[0].id)

    # include_archived=True 返回 3 个
    result = pm_svc.list_members(
        db_session, portfolio_id=pf.id, include_archived=True
    )
    assert len(result) == 3

    archived_count = sum(1 for m in result if m.effective_to is not None)
    assert archived_count == 1


# ----------------------------------------------------------------------------
# 13. list_members 按 portfolio_id 筛选
# ----------------------------------------------------------------------------


def test_list_members_filter_by_portfolio_id(db_session):
    """【WP4.2】list_members 按 portfolio_id 筛选只返回该组合的成员。"""
    pf1 = _make_portfolio(db_session, name="QA-List-PF1")
    pf2 = _make_portfolio(db_session, name="QA-List-PF2")

    sym1 = _make_symbol(db_session, symbol="601200")
    sym2 = _make_symbol(db_session, symbol="601201")
    sym3 = _make_symbol(db_session, symbol="601202")

    pm_svc.create_member(db_session, portfolio_id=pf1.id, symbol_id=sym1.id)
    pm_svc.create_member(db_session, portfolio_id=pf1.id, symbol_id=sym2.id)
    pm_svc.create_member(db_session, portfolio_id=pf2.id, symbol_id=sym3.id)

    result_pf1 = pm_svc.list_members(db_session, portfolio_id=pf1.id)
    result_pf2 = pm_svc.list_members(db_session, portfolio_id=pf2.id)

    assert len(result_pf1) == 2
    assert len(result_pf2) == 1
    assert all(m.portfolio_id == pf1.id for m in result_pf1)
    assert all(m.portfolio_id == pf2.id for m in result_pf2)


def test_list_members_filter_by_status(db_session):
    """【WP4.2】list_members 按 status 筛选只返回对应状态的成员。"""
    pf = _make_portfolio(db_session, name="QA-List-Status-PF")

    sym1 = _make_symbol(db_session, symbol="601300")
    sym2 = _make_symbol(db_session, symbol="601301")

    m1 = pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym2.id)

    pm_svc.pause_member(db_session, member_id=m1.id)

    paused = pm_svc.list_members(
        db_session, portfolio_id=pf.id, status=STATUS_PAUSED
    )
    active = pm_svc.list_members(
        db_session, portfolio_id=pf.id, status=STATUS_ACTIVE
    )

    assert len(paused) == 1
    assert paused[0].status == STATUS_PAUSED
    assert len(active) == 1
    assert active[0].status == STATUS_ACTIVE


# ----------------------------------------------------------------------------
# 14. update_member
# ----------------------------------------------------------------------------


def test_update_member_updates_fields(db_session):
    """【WP4.2】update_member 更新 status / execution_mode / priority / note 等字段。"""
    pf = _make_portfolio(db_session, name="QA-Update-PF")
    sym = _make_symbol(db_session, symbol="601400")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    updated = pm_svc.update_member(
        db_session,
        member_id=member.id,
        status=STATUS_PAUSED,
        execution_mode=EXECUTION_AUTO,
        entry_rule_version_id=2001,
        exit_rule_version_id=2002,
        manual_lock=True,
        priority=9,
        note="更新后笔记",
    )

    assert updated is not None
    assert updated.status == STATUS_PAUSED
    assert updated.execution_mode == EXECUTION_AUTO
    assert updated.entry_rule_version_id == 2001
    assert updated.exit_rule_version_id == 2002
    assert updated.manual_lock is True
    assert updated.priority == 9
    assert updated.note == "更新后笔记"
    assert updated.updated_at is not None


def test_update_member_partial_update(db_session):
    """【WP4.2】update_member 仅更新显式传入的字段，其他字段保持不变。"""
    pf = _make_portfolio(db_session, name="QA-Update-Partial-PF")
    sym = _make_symbol(db_session, symbol="601401")

    member = pm_svc.create_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        priority=1,
        note="初始笔记",
    )

    updated = pm_svc.update_member(
        db_session, member_id=member.id, priority=99
    )

    assert updated.priority == 99
    # 其他字段不变
    assert updated.note == "初始笔记"
    assert updated.status == STATUS_ACTIVE
    assert updated.execution_mode == EXECUTION_MANUAL


def test_update_member_not_found_returns_none(db_session):
    """【WP4.2】update_member 不存在的成员返回 None。"""
    result = pm_svc.update_member(
        db_session, member_id=99999, priority=5
    )
    assert result is None


def test_update_member_manual_lock_false(db_session):
    """【WP4.2】update_member 可将 manual_lock 从 True 设置为 False。"""
    pf = _make_portfolio(db_session, name="QA-Update-Lock-PF")
    sym = _make_symbol(db_session, symbol="601402")

    member = pm_svc.create_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        manual_lock=True,
    )
    assert member.manual_lock is True

    updated = pm_svc.update_member(
        db_session, member_id=member.id, manual_lock=False
    )
    assert updated.manual_lock is False


# ----------------------------------------------------------------------------
# 15. has_position 检查
# ----------------------------------------------------------------------------


def test_has_position_returns_false_when_no_position(db_session):
    """【WP4.2】has_position 无持仓时返回 (False, 0.0)。"""
    pf = _make_portfolio(db_session, name="QA-HasPos-No-PF")
    sym = _make_symbol(db_session, symbol="601500")

    has_pos, qty = pm_svc.has_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    assert has_pos is False
    assert qty == 0.0


def test_has_position_returns_true_with_quantity(db_session):
    """【WP4.2】has_position 有持仓时返回 (True, quantity)。"""
    pf = _make_portfolio(db_session, name="QA-HasPos-Yes-PF")
    sym = _make_symbol(db_session, symbol="601501")

    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=150.5,
    )

    has_pos, qty = pm_svc.has_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    assert has_pos is True
    assert qty == 150.5


def test_has_position_zero_quantity_returns_false(db_session):
    """【WP4.2】has_position quantity=0 视为无持仓。"""
    pf = _make_portfolio(db_session, name="QA-HasPos-Zero-PF")
    sym = _make_symbol(db_session, symbol="601502")

    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=0.0,
    )

    has_pos, qty = pm_svc.has_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    assert has_pos is False
    assert qty == 0.0


# ----------------------------------------------------------------------------
# 16. get_active_member
# ----------------------------------------------------------------------------


def test_get_active_member_returns_active(db_session):
    """【WP4.2】get_active_member 返回当前有效成员。"""
    pf = _make_portfolio(db_session, name="QA-Active-PF")
    sym = _make_symbol(db_session, symbol="601600")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    active = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    assert active is not None
    assert active.id == member.id
    assert active.effective_to is None


def test_get_active_member_returns_none_after_archive(db_session):
    """【WP4.2】归档后 get_active_member 返回 None。"""
    pf = _make_portfolio(db_session, name="QA-Active-None-PF")
    sym = _make_symbol(db_session, symbol="601601")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pm_svc.archive_member(db_session, member_id=member.id)

    active = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert active is None


def test_get_member_by_id(db_session):
    """【WP4.2】get_member 按 ID 查询。"""
    pf = _make_portfolio(db_session, name="QA-Get-PF")
    sym = _make_symbol(db_session, symbol="601602")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    found = pm_svc.get_member(db_session, member_id=member.id)
    assert found is not None
    assert found.id == member.id

    not_found = pm_svc.get_member(db_session, member_id=99999)
    assert not_found is None


# ----------------------------------------------------------------------------
# 17. manual_lock 字段
# ----------------------------------------------------------------------------


def test_create_member_with_manual_lock_true(db_session):
    """【WP4.2】创建成员时 manual_lock=True 可正确存储。"""
    pf = _make_portfolio(db_session, name="QA-Lock-PF")
    sym = _make_symbol(db_session, symbol="601700")

    member = pm_svc.create_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        manual_lock=True,
    )

    assert member.manual_lock is True

    # 重新查询验证持久化
    db_session.refresh(member)
    assert member.manual_lock is True


def test_create_member_manual_lock_default_false(db_session):
    """【WP4.2】不传 manual_lock 时默认为 False。"""
    pf = _make_portfolio(db_session, name="QA-Lock-Default-PF")
    sym = _make_symbol(db_session, symbol="601701")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )

    assert member.manual_lock is False


# ----------------------------------------------------------------------------
# 18. 成员归档不会误删持仓（spec 验收）
# ----------------------------------------------------------------------------


def test_archive_member_does_not_delete_position(db_session):
    """【WP4.2】成员归档不会误删持仓（spec line 334 验收）。

    场景：force=True 归档持仓成员后，Position 仍存在且 quantity 不变。
    """
    pf = _make_portfolio(db_session, name="QA-NoDelete-PF")
    sym = _make_symbol(db_session, symbol="601800")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pos = _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=100.0,
    )
    pos_id = pos.id

    archived = pm_svc.archive_member(
        db_session, member_id=member.id, force=True
    )

    # 成员已归档
    assert archived.effective_to is not None
    assert archived.status == STATUS_ARCHIVED

    # Position 仍存在且 quantity 不变
    db_session.expire_all()
    remaining_pos = db_session.get(Position, pos_id)
    assert remaining_pos is not None
    assert remaining_pos.quantity == 100.0
    assert remaining_pos.portfolio_id == pf.id
    assert remaining_pos.symbol_id == sym.id


def test_archive_member_no_position_when_quantity_zero(db_session):
    """【WP4.2】quantity=0 的持仓视为无持仓，归档不抛异常。"""
    pf = _make_portfolio(db_session, name="QA-Archive-Zero-PF")
    sym = _make_symbol(db_session, symbol="601900")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=0.0,
    )

    # quantity=0 视为无持仓，归档不抛异常
    archived = pm_svc.archive_member(
        db_session, member_id=member.id, force=False
    )

    assert archived is not None
    assert archived.status == STATUS_ARCHIVED
    assert archived.effective_to is not None
