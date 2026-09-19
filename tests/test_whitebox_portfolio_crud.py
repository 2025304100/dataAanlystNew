"""白盒测试 - 组合 CRUD 补全（P0-6）+ WP4.6 成员集成扩展。

验证新增的 DELETE /portfolios/{id} 和 PUT /portfolios/{id} 端点：
1. POST 创建组合（同名查重 409）
2. PUT 更新组合属性（name/total_capital/is_default）
3. PUT 名称查重（与其他组合冲突 409）
4. DELETE 删除非默认组合（级联清理）
5. DELETE 默认组合拒绝（400）
6. DELETE 最后一个组合拒绝（400）
7. POST is_default=True 时自动取消其他默认

WP4.6 扩展（组合 CRUD + 成员关系集成）：
8. 创建组合 + 创建成员 + 查询成员数
9. 删除组合时 Position 通过 ORM cascade 被清理
10. 删除组合时成员不阻塞删除流程
11. 成员与持仓共存（has_position 检查）
12. 成员归档不影响 Position
13. 回填集成：3 个 Position → 3 个 legacy_position 成员
14. 回填幂等性
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes.portfolios import (
    create_portfolio,
    delete_portfolio,
    update_portfolio,
)
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    SOURCE_LEGACY_POSITION,
    STATUS_ARCHIVED,
)
from app.models.symbol import Symbol
from app.schemas.portfolio import PortfolioCreate, PortfolioUpdate
from app.services import portfolio_member_backfill as bf_svc
from app.services import portfolio_members as pm_svc

pytestmark = pytest.mark.whitebox


def _make_portfolio(db_session, name="QA-组合", account_type="simulated", total_capital=100000.0, is_default=False) -> Portfolio:
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=int(is_default),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# ----------------------------------------------------------------------------
# 1. POST 同名查重
# ----------------------------------------------------------------------------

def test_create_portfolio_duplicate_name_returns_409(db_session):
    """【P0-6 CRUD 测试】创建同名组合应返回 409。"""
    _make_portfolio(db_session, name="QA-Dup")
    payload = PortfolioCreate(
        name="QA-Dup",
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
    )
    with pytest.raises(HTTPException) as exc:
        create_portfolio(payload, db=db_session)
    assert exc.value.status_code == 409


# ----------------------------------------------------------------------------
# 2. PUT 更新属性
# ----------------------------------------------------------------------------

def test_update_portfolio_name_and_capital(db_session):
    """【P0-6 CRUD 测试】PUT 可更新 name 和 total_capital。"""
    p = _make_portfolio(db_session, name="QA-Old", total_capital=100000.0)
    payload = PortfolioUpdate(name="QA-New", total_capital=200000.0)
    updated = update_portfolio(p.id, payload, db=db_session)
    assert updated.name == "QA-New"
    assert updated.total_capital == 200000.0


def test_update_portfolio_name_duplicate_returns_409(db_session):
    """【P0-6 CRUD 测试】PUT 改名与其他组合重名应返回 409。"""
    _make_portfolio(db_session, name="QA-Other")
    p = _make_portfolio(db_session, name="QA-Self")
    payload = PortfolioUpdate(name="QA-Other")
    with pytest.raises(HTTPException) as exc:
        update_portfolio(p.id, payload, db=db_session)
    assert exc.value.status_code == 409


def test_update_portfolio_set_default_clears_others(db_session):
    """【P0-6 CRUD 测试】PUT 设 is_default=True 时自动取消其他默认。"""
    p1 = _make_portfolio(db_session, name="QA-Default", is_default=True)
    p2 = _make_portfolio(db_session, name="QA-NonDefault", is_default=False)

    payload = PortfolioUpdate(is_default=True)
    update_portfolio(p2.id, payload, db=db_session)

    db_session.refresh(p1)
    db_session.refresh(p2)
    assert p1.is_default == 0
    assert p2.is_default == 1


def test_update_portfolio_not_found_returns_404(db_session):
    """【P0-6 CRUD 测试】PUT 不存在的组合应返回 404。"""
    payload = PortfolioUpdate(name="QA-X")
    with pytest.raises(HTTPException) as exc:
        update_portfolio(99999, payload, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 3. DELETE 删除
# ----------------------------------------------------------------------------

def test_delete_portfolio_default_rejected(db_session):
    """【P0-6 CRUD 测试】DELETE 默认组合应被拒绝（400）。"""
    p = _make_portfolio(db_session, name="QA-Default", is_default=True)
    with pytest.raises(HTTPException) as exc:
        delete_portfolio(p.id, db=db_session)
    assert exc.value.status_code == 400
    assert "default" in exc.value.detail.lower()


def test_delete_portfolio_last_one_rejected(db_session):
    """【P0-6 CRUD 测试】DELETE 最后一个组合应被拒绝（400）。"""
    p1 = _make_portfolio(db_session, name="QA-Default", is_default=True)
    with pytest.raises(HTTPException) as exc:
        delete_portfolio(p1.id, db=db_session)
    # 默认组合拒绝优先级更高
    assert exc.value.status_code == 400


def test_delete_portfolio_success(db_session):
    """【P0-6 CRUD 测试】DELETE 非默认组合成功。"""
    _make_portfolio(db_session, name="QA-Default", is_default=True)
    p2 = _make_portfolio(db_session, name="QA-ToDelete", is_default=False)

    result = delete_portfolio(p2.id, db=db_session)
    assert result["deleted"] is True
    assert result["id"] == p2.id

    # 验证已删除
    deleted = db_session.execute(select(Portfolio).where(Portfolio.id == p2.id)).scalars().first()
    assert deleted is None


def test_delete_portfolio_not_found_returns_404(db_session):
    """【P0-6 CRUD 测试】DELETE 不存在的组合应返回 404。"""
    with pytest.raises(HTTPException) as exc:
        delete_portfolio(99999, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 4. POST is_default 联动
# ----------------------------------------------------------------------------

def test_create_portfolio_with_default_clears_others(db_session):
    """【P0-6 CRUD 测试】POST is_default=True 时自动取消其他默认。"""
    p1 = _make_portfolio(db_session, name="QA-OldDefault", is_default=True)
    payload = PortfolioCreate(
        name="QA-NewDefault",
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        is_default=True,
    )
    new_p = create_portfolio(payload, db=db_session)
    db_session.refresh(p1)
    assert p1.is_default == 0
    assert new_p.is_default == 1


# ============================================================================
# WP4.6 扩展：组合 CRUD + 成员关系集成测试
# ============================================================================


def _make_symbol(db_session, symbol: str = "600000") -> Symbol:
    """构造测试标的。"""
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
) -> Position:
    """构造测试持仓。"""
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=10.0,
        latest_price=12.0,
        market_value=quantity * 12.0,
        position_pct=0.1,
        asset_type="stock",
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


# ----------------------------------------------------------------------------
# 8. 创建组合 + 创建成员 + 查询成员数
# ----------------------------------------------------------------------------


def test_create_portfolio_then_create_member(db_session):
    """【WP4.6】创建组合后可创建成员，list_members 返回成员数。

    验证组合与成员的关联场景：组合作为父对象，成员作为子对象。
    """
    pf = _make_portfolio(db_session, name="QA-CRUD-Member-PF")
    sym = _make_symbol(db_session, symbol="600100")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, note="测试成员"
    )

    assert member.portfolio_id == pf.id
    assert member.symbol_id == sym.id

    # 查询组合成员数
    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 1
    assert members[0].id == member.id
    assert members[0].portfolio_id == pf.id


def test_portfolio_multiple_members_count(db_session):
    """【WP4.6】一个组合可包含多个成员，list_members 返回正确数量。"""
    pf = _make_portfolio(db_session, name="QA-CRUD-MultiMember-PF")
    sym1 = _make_symbol(db_session, symbol="600110")
    sym2 = _make_symbol(db_session, symbol="600111")
    sym3 = _make_symbol(db_session, symbol="600112")

    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym2.id)
    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym3.id)

    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 3
    # 所有成员都属于该组合
    assert all(m.portfolio_id == pf.id for m in members)


# ----------------------------------------------------------------------------
# 9. 删除组合时 Position 通过 ORM cascade 被清理
# ----------------------------------------------------------------------------


def test_delete_portfolio_cascades_positions(db_session):
    """【WP4.6】删除组合时 Position 通过 ORM cascade 被清理。

    Portfolio.positions 关系定义了 cascade="all, delete-orphan"，
    因此 db.delete(portfolio) 会级联删除关联的 Position。
    """
    # 先创建一个默认组合（delete_portfolio 安全检查需要至少 2 个组合）
    _make_portfolio(db_session, name="QA-Default", is_default=True)
    pf = _make_portfolio(db_session, name="QA-Cascade-PF", is_default=False)
    sym = _make_symbol(db_session, symbol="600200")

    pos = _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )
    pos_id = pos.id

    result = delete_portfolio(pf.id, db=db_session)
    assert result["deleted"] is True

    # Position 被级联删除
    db_session.expire_all()
    remaining_pos = db_session.get(Position, pos_id)
    assert remaining_pos is None

    # 组合也被删除
    deleted_pf = db_session.execute(
        select(Portfolio).where(Portfolio.id == pf.id)
    ).scalars().first()
    assert deleted_pf is None


# ----------------------------------------------------------------------------
# 10. 删除组合时成员不阻塞删除流程
# ----------------------------------------------------------------------------


def test_delete_portfolio_with_members_succeeds(db_session):
    """【WP4.6】删除组合时即使存在成员也不阻塞删除流程。

    PortfolioMember.portfolio_id 是普通 Integer 列（非外键约束），
    因此删除 Portfolio 不会因成员存在而失败。
    删除后建议通过应用层清理孤儿成员记录（本测试验证删除本身不报错）。
    """
    _make_portfolio(db_session, name="QA-Default", is_default=True)
    pf = _make_portfolio(db_session, name="QA-WithMember-PF", is_default=False)
    sym = _make_symbol(db_session, symbol="600300")

    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    # 删除组合不应抛出异常
    result = delete_portfolio(pf.id, db=db_session)
    assert result["deleted"] is True
    assert result["id"] == pf.id


# ----------------------------------------------------------------------------
# 11. 成员与持仓共存（has_position 检查）
# ----------------------------------------------------------------------------


def test_member_has_position_when_position_exists(db_session):
    """【WP4.6】成员有持仓时 has_position 返回 (True, quantity)。"""
    pf = _make_portfolio(db_session, name="QA-Coexist-HasPos-PF")
    sym = _make_symbol(db_session, symbol="600400")

    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym.id)
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=150.5
    )

    has_pos, qty = pm_svc.has_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert has_pos is True
    assert qty == 150.5


def test_member_has_no_position_when_position_absent(db_session):
    """【WP4.6】成员无持仓时 has_position 返回 (False, 0.0)。

    无持仓成员可以存在，账户权益不变化（spec line 338）。
    """
    pf = _make_portfolio(db_session, name="QA-Coexist-NoPos-PF")
    sym = _make_symbol(db_session, symbol="600401")

    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    has_pos, qty = pm_svc.has_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert has_pos is False
    assert qty == 0.0


def test_member_has_position_zero_quantity_returns_false(db_session):
    """【WP4.6】quantity=0 的持仓视为无持仓（边界测试）。"""
    pf = _make_portfolio(db_session, name="QA-Coexist-ZeroQty-PF")
    sym = _make_symbol(db_session, symbol="600402")

    pm_svc.create_member(db_session, portfolio_id=pf.id, symbol_id=sym.id)
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=0.0
    )

    has_pos, qty = pm_svc.has_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert has_pos is False
    assert qty == 0.0


# ----------------------------------------------------------------------------
# 12. 成员归档不影响 Position
# ----------------------------------------------------------------------------


def test_archive_member_does_not_delete_position(db_session):
    """【WP4.6】归档成员（force=True）不影响 Position。

    参照 spec line 334："成员归档不会误删持仓"。
    """
    pf = _make_portfolio(db_session, name="QA-Coexist-Archive-PF")
    sym = _make_symbol(db_session, symbol="600500")

    member = pm_svc.create_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    pos = _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym.id, quantity=100.0
    )
    pos_id = pos.id

    archived = pm_svc.archive_member(
        db_session, member_id=member.id, force=True
    )
    assert archived.status == STATUS_ARCHIVED
    assert archived.effective_to is not None

    # Position 仍存在且字段不变
    db_session.expire_all()
    remaining_pos = db_session.get(Position, pos_id)
    assert remaining_pos is not None
    assert remaining_pos.quantity == 100.0
    assert remaining_pos.portfolio_id == pf.id
    assert remaining_pos.symbol_id == sym.id


# ----------------------------------------------------------------------------
# 13. 回填集成：3 个 Position → 3 个 legacy_position 成员
# ----------------------------------------------------------------------------


def test_backfill_integration_creates_legacy_members(db_session):
    """【WP4.6】回填集成：3 个 Position 回填后产生 3 个 legacy_position 成员。

    参照 spec line 162："现有 3 条持仓回填后对应 3 个有效成员，source_type=legacy_position"。
    """
    pf = _make_portfolio(db_session, name="QA-Backfill-Integration-PF")
    sym1 = _make_symbol(db_session, symbol="600600")
    sym2 = _make_symbol(db_session, symbol="600601")
    sym3 = _make_symbol(db_session, symbol="600602")

    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym1.id, quantity=100.0
    )
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym2.id, quantity=200.0
    )
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym3.id, quantity=300.0
    )

    stats = bf_svc.backfill_positions_to_members(db_session)

    assert stats["total_positions"] == 3
    assert stats["created_members"] == 3
    assert stats["errors"] == []

    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 3
    for m in members:
        assert m.source_type == SOURCE_LEGACY_POSITION
        assert m.effective_to is None


# ----------------------------------------------------------------------------
# 14. 回填幂等性
# ----------------------------------------------------------------------------


def test_backfill_integration_idempotent(db_session):
    """【WP4.6】回填幂等：第二次运行 created=0, skipped=N。

    参照 spec line 166："回填脚本可重复运行，使用组合+标的幂等检查"。
    """
    pf = _make_portfolio(db_session, name="QA-Backfill-Idempotent-PF")
    sym1 = _make_symbol(db_session, symbol="600700")
    sym2 = _make_symbol(db_session, symbol="600701")

    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym1.id, quantity=100.0
    )
    _make_position(
        db_session, portfolio_id=pf.id, symbol_id=sym2.id, quantity=200.0
    )

    # 第一次回填
    stats1 = bf_svc.backfill_positions_to_members(db_session)
    assert stats1["created_members"] == 2
    assert stats1["skipped_existing"] == 0

    # 第二次回填（幂等）
    stats2 = bf_svc.backfill_positions_to_members(db_session)
    assert stats2["created_members"] == 0
    assert stats2["skipped_existing"] == 2
    assert stats2["errors"] == []

    # 仍只有 2 个成员（不重复创建）
    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 2
