"""白盒测试 - WP4.3 持仓回填服务。

覆盖 app/services/portfolio_member_backfill.py：
1. backfill 基本流程（3 条持仓 → 3 个成员）
2. effective_from 优先取 opened_at
3. opened_at 缺失时取迁移时间（note 含标记）
4. Position 字段不变（ID/数量/成本/最新价/持仓比例）
5. 幂等性（第二次运行 created=0, skipped=3）
6. 无持仓组合不创建成员
7. quantity=0 的持仓跳过
8. dry_run 模式（只统计不写入）
9. 指定 portfolio_id（只回填该组合）
10. verify_backfill 验证（matched=3, missing=[]）
11. verify_backfill 发现缺失（matched=2, missing 含 1 项）
12. errors 收集（单条失败不中断）
13. 不覆盖非 legacy 成员（source_type=manual 跳过）

测试用 SQLite 内存库（db_session fixture）。

参照 spec line 201-205 "持仓回填" Scenario 硬约束：
- 对每条现有 positions 创建成员，source_type=legacy_position
- effective_from 优先取 Position.opened_at
- Position ID、数量、成本、最新价和持仓比例完全不变
- 无持仓组合不凭最新扫描结果自动创建成员
- 回填脚本可重复运行，使用组合+标的幂等检查
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import (
    PortfolioMember,
    SOURCE_LEGACY_POSITION,
    SOURCE_MANUAL,
    STATUS_ACTIVE,
)
from app.models.symbol import Symbol
from app.services import portfolio_member_backfill as bf_svc
from app.services import portfolio_members as pm_svc


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_portfolio(db_session, name: str = "QA-BF-PF") -> Portfolio:
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
    avg_cost: float = 10.0,
    latest_price: float = 12.0,
    position_pct: float = 0.1,
    opened_at: datetime | None = None,
    asset_type: str = "stock",
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=avg_cost,
        latest_price=latest_price,
        market_value=quantity * latest_price,
        position_pct=position_pct,
        asset_type=asset_type,
        opened_at=opened_at,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


# ----------------------------------------------------------------------------
# 1. backfill 基本流程
# ----------------------------------------------------------------------------


def test_backfill_basic_creates_members_for_positions(db_session):
    """【WP4.3】3 条持仓回填后创建 3 个 legacy_position 成员。

    参照 spec line 341："现有 3 条持仓回填后对应 3 个有效成员"。
    """
    pf = _make_portfolio(db_session, name="QA-BF-Basic-PF")
    sym1 = _make_symbol(db_session, symbol="700001")
    sym2 = _make_symbol(db_session, symbol="700002")
    sym3 = _make_symbol(db_session, symbol="700003")

    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym2.id)
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym3.id)

    stats = bf_svc.backfill_positions_to_members(db_session)

    assert stats["total_positions"] == 3
    assert stats["created_members"] == 3
    assert stats["skipped_existing"] == 0
    assert stats["errors"] == []

    # 验证 3 个成员均为 legacy_position 来源
    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 3
    for m in members:
        assert m.source_type == SOURCE_LEGACY_POSITION
        assert m.status == STATUS_ACTIVE
        assert m.effective_to is None


# ----------------------------------------------------------------------------
# 2. effective_from 优先取 opened_at
# ----------------------------------------------------------------------------


def test_backfill_effective_from_uses_opened_at(db_session):
    """【WP4.3】effective_from 优先取 Position.opened_at。

    参照 spec line 204："effective_from 优先取 Position.opened_at"。
    """
    pf = _make_portfolio(db_session, name="QA-BF-OpenedAt-PF")
    sym = _make_symbol(db_session, symbol="700010")

    opened = datetime(2023, 1, 15, 9, 30, 0)
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        opened_at=opened,
    )

    bf_svc.backfill_positions_to_members(db_session)

    member = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert member is not None
    assert member.effective_from == opened


# ----------------------------------------------------------------------------
# 3. opened_at 缺失时取迁移时间
# ----------------------------------------------------------------------------


def test_backfill_effective_from_fallback_to_now_when_opened_at_missing(db_session):
    """【WP4.3】opened_at 缺失时 effective_from 取迁移时间，note 含标记。

    参照 spec line 337："缺失时取迁移时间并标记日期不确定"。
    """
    pf = _make_portfolio(db_session, name="QA-BF-NoOpenedAt-PF")
    sym = _make_symbol(db_session, symbol="700020")

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        opened_at=None,
    )

    bf_svc.backfill_positions_to_members(db_session)
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    member = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert member is not None
    # effective_in 在 [before, after] 区间内（迁移时间）
    assert before <= member.effective_from <= after
    # note 含 "opened_at 缺失" 标记
    assert member.note is not None
    assert "opened_at 缺失" in member.note


# ----------------------------------------------------------------------------
# 4. Position 字段不变
# ----------------------------------------------------------------------------


def test_backfill_does_not_modify_position_fields(db_session):
    """【WP4.3】回填不改变 Position ID/数量/成本/最新价/持仓比例。

    参照 spec line 205："Position ID、数量、成本、最新价和持仓比例完全不变"。
    """
    pf = _make_portfolio(db_session, name="QA-BF-Unchanged-PF")
    sym = _make_symbol(db_session, symbol="700030")

    pos = _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=100.0,
        avg_cost=50.5,
        latest_price=55.0,
        position_pct=0.15,
    )
    pos_id = pos.id

    bf_svc.backfill_positions_to_members(db_session)

    # 重新查询 Position，验证字段未变
    db_session.expire_all()
    pos_after = db_session.get(Position, pos_id)
    assert pos_after is not None
    assert pos_after.id == pos_id
    assert pos_after.quantity == 100.0
    assert pos_after.avg_cost == 50.5
    assert pos_after.latest_price == 55.0
    assert pos_after.position_pct == 0.15
    assert pos_after.portfolio_id == pf.id
    assert pos_after.symbol_id == sym.id


# ----------------------------------------------------------------------------
# 5. 幂等性
# ----------------------------------------------------------------------------


def test_backfill_idempotent_second_run_skips_existing(db_session):
    """【WP4.3】回填可重复运行，第二次 created=0, skipped=3，仍只有 3 个成员。

    参照 spec line 340："回填脚本可重复运行，使用组合+标的幂等检查"。
    """
    pf = _make_portfolio(db_session, name="QA-BF-Idempotent-PF")
    sym1 = _make_symbol(db_session, symbol="700040")
    sym2 = _make_symbol(db_session, symbol="700041")
    sym3 = _make_symbol(db_session, symbol="700042")

    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym2.id)
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym3.id)

    # 第一次回填
    stats1 = bf_svc.backfill_positions_to_members(db_session)
    assert stats1["created_members"] == 3
    assert stats1["skipped_existing"] == 0

    # 第二次回填（幂等）
    stats2 = bf_svc.backfill_positions_to_members(db_session)
    assert stats2["created_members"] == 0
    assert stats2["skipped_existing"] == 3
    assert stats2["errors"] == []

    # 仍只有 3 个成员（不重复创建）
    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 3


# ----------------------------------------------------------------------------
# 6. 无持仓组合不创建成员
# ----------------------------------------------------------------------------


def test_backfill_no_position_no_member(db_session):
    """【WP4.3】无持仓组合不创建成员。

    参照 spec line 339："无持仓组合不凭最新扫描结果自动创建成员"。
    """
    pf = _make_portfolio(db_session, name="QA-BF-Empty-PF")
    # 不创建任何 Position

    stats = bf_svc.backfill_positions_to_members(db_session)

    assert stats["total_positions"] == 0
    assert stats["created_members"] == 0
    assert stats["errors"] == []

    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 0


# ----------------------------------------------------------------------------
# 7. quantity=0 的持仓跳过
# ----------------------------------------------------------------------------


def test_backfill_skips_zero_quantity_positions(db_session):
    """【WP4.3】quantity=0 的持仓视为无持仓，不计入 total_positions。"""
    pf = _make_portfolio(db_session, name="QA-BF-ZeroQty-PF")
    sym = _make_symbol(db_session, symbol="700050")

    _make_position(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        quantity=0.0,
    )

    stats = bf_svc.backfill_positions_to_members(db_session)

    # quantity != 0 才计入
    assert stats["total_positions"] == 0
    assert stats["created_members"] == 0

    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 0


# ----------------------------------------------------------------------------
# 8. dry_run 模式
# ----------------------------------------------------------------------------


def test_backfill_dry_run_does_not_persist(db_session):
    """【WP4.3】dry_run=True 时只统计不实际写入 DB。"""
    pf = _make_portfolio(db_session, name="QA-BF-DryRun-PF")
    sym = _make_symbol(db_session, symbol="700060")

    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    stats = bf_svc.backfill_positions_to_members(db_session, dry_run=True)

    # 统计正确
    assert stats["total_positions"] == 1
    assert stats["created_members"] == 1

    # 实际未创建成员
    members = pm_svc.list_members(db_session, portfolio_id=pf.id)
    assert len(members) == 0


# ----------------------------------------------------------------------------
# 9. 指定 portfolio_id
# ----------------------------------------------------------------------------


def test_backfill_filter_by_portfolio_id(db_session):
    """【WP4.3】指定 portfolio_id 时只回填该组合的持仓。"""
    pf1 = _make_portfolio(db_session, name="QA-BF-Filter-PF1")
    pf2 = _make_portfolio(db_session, name="QA-BF-Filter-PF2")
    sym1 = _make_symbol(db_session, symbol="700070")
    sym2 = _make_symbol(db_session, symbol="700071")

    _make_position(db_session, portfolio_id=pf1.id, symbol_id=sym1.id)
    _make_position(db_session, portfolio_id=pf2.id, symbol_id=sym2.id)

    stats = bf_svc.backfill_positions_to_members(
        db_session, portfolio_id=pf1.id
    )

    # 只回填 pf1
    assert stats["total_positions"] == 1
    assert stats["created_members"] == 1

    # pf1 有成员，pf2 无成员
    members_pf1 = pm_svc.list_members(db_session, portfolio_id=pf1.id)
    members_pf2 = pm_svc.list_members(db_session, portfolio_id=pf2.id)
    assert len(members_pf1) == 1
    assert len(members_pf2) == 0


# ----------------------------------------------------------------------------
# 10. verify_backfill 验证（完整回填）
# ----------------------------------------------------------------------------


def test_verify_backfill_all_matched(db_session):
    """【WP4.3】完整回填后 verify_backfill 报告 matched=3, missing=[]。"""
    pf = _make_portfolio(db_session, name="QA-BF-Verify-OK-PF")
    sym1 = _make_symbol(db_session, symbol="700080")
    sym2 = _make_symbol(db_session, symbol="700081")
    sym3 = _make_symbol(db_session, symbol="700082")

    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym2.id)
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym3.id)

    bf_svc.backfill_positions_to_members(db_session)

    result = bf_svc.verify_backfill(db_session, portfolio_id=pf.id)

    assert result["positions_count"] == 3
    assert result["members_count"] == 3
    assert result["matched"] == 3
    assert result["missing_members"] == []
    assert result["position_fields_unchanged"] is True


# ----------------------------------------------------------------------------
# 11. verify_backfill 发现缺失
# ----------------------------------------------------------------------------


def test_verify_backfill_finds_missing(db_session):
    """【WP4.3】只回填部分时 verify_backfill 报告缺失。"""
    pf = _make_portfolio(db_session, name="QA-BF-Verify-Missing-PF")
    sym1 = _make_symbol(db_session, symbol="700090")
    sym2 = _make_symbol(db_session, symbol="700091")
    sym3 = _make_symbol(db_session, symbol="700092")

    pos1 = _make_position(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    pos2 = _make_position(db_session, portfolio_id=pf.id, symbol_id=sym2.id)
    pos3 = _make_position(db_session, portfolio_id=pf.id, symbol_id=sym3.id)

    # 只回填前两条
    bf_svc.backfill_positions_to_members(db_session, portfolio_id=pf.id)

    # 手动归档第三条对应的成员（模拟缺失）
    member3 = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym3.id
    )
    assert member3 is not None
    pm_svc.archive_member(db_session, member_id=member3.id, force=True)

    result = bf_svc.verify_backfill(db_session, portfolio_id=pf.id)

    assert result["positions_count"] == 3
    # legacy_position 有效成员只剩 2 个
    assert result["members_count"] == 2
    assert result["matched"] == 2
    assert len(result["missing_members"]) == 1
    assert result["missing_members"][0]["position_id"] == pos3.id


# ----------------------------------------------------------------------------
# 12. errors 收集
# ----------------------------------------------------------------------------


def test_backfill_collects_errors_without_interrupting(db_session):
    """【WP4.3】单条回填失败不中断，错误收集到 stats["errors"]。"""
    pf = _make_portfolio(db_session, name="QA-BF-Error-PF")
    sym1 = _make_symbol(db_session, symbol="700100")
    sym2 = _make_symbol(db_session, symbol="700101")

    pos1 = _make_position(db_session, portfolio_id=pf.id, symbol_id=sym1.id)
    pos2 = _make_position(db_session, portfolio_id=pf.id, symbol_id=sym2.id)

    # 通过 monkeypatch 让第一次调用 create_member 抛异常（模拟 pos1 失败）
    real_create_member = bf_svc.create_member
    call_count = {"n": 0}

    def fake_create_member(db, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("模拟回填失败")
        return real_create_member(db, **kwargs)

    bf_svc.create_member = fake_create_member
    try:
        stats = bf_svc.backfill_positions_to_members(db_session)
    finally:
        bf_svc.create_member = real_create_member

    # 第一条失败，第二条成功
    assert stats["total_positions"] == 2
    assert stats["created_members"] == 1
    assert len(stats["errors"]) == 1
    assert stats["errors"][0]["position_id"] == pos1.id
    assert "模拟回填失败" in stats["errors"][0]["error"]

    # pos2 仍成功创建了成员
    member2 = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym2.id
    )
    assert member2 is not None


# ----------------------------------------------------------------------------
# 13. 不覆盖非 legacy 成员
# ----------------------------------------------------------------------------


def test_backfill_does_not_overwrite_non_legacy_member(db_session):
    """【WP4.3】已有非 legacy 成员（如 manual）时跳过，不覆盖。

    回填不应覆盖用户手动创建的成员。
    """
    pf = _make_portfolio(db_session, name="QA-BF-NoOverwrite-PF")
    sym = _make_symbol(db_session, symbol="700110")

    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    # 先手动创建一个 source_type=manual 的成员
    manual_member = pm_svc.create_member(
        db_session,
        portfolio_id=pf.id,
        symbol_id=sym.id,
        source_type=SOURCE_MANUAL,
        note="用户手动添加",
    )
    manual_member_id = manual_member.id

    stats = bf_svc.backfill_positions_to_members(db_session)

    # 跳过（不覆盖）
    assert stats["total_positions"] == 1
    assert stats["created_members"] == 0
    assert stats["skipped_existing"] == 1
    assert stats["errors"] == []

    # 原成员仍存在，source_type 仍为 manual
    db_session.expire_all()
    member = pm_svc.get_active_member(
        db_session, portfolio_id=pf.id, symbol_id=sym.id
    )
    assert member is not None
    assert member.id == manual_member_id
    assert member.source_type == SOURCE_MANUAL
    assert member.note == "用户手动添加"
