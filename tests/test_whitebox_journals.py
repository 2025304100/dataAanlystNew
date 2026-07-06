"""白盒测试 - 交易日志 API (P2-3)。

覆盖 app/api/routes/journals.py 路由层逻辑：
1. GET /journals 列表（按 portfolio_id / symbol_id / entry_type 过滤，按 created_at desc 排序）
2. POST /journals 创建（portfolio/symbol 不存在 404，trade_setup_id 不存在 404）
3. PATCH /journals/{id} 部分更新（journal 不存在 404，follow_system bool→int 转换）
4. DELETE /journals/{id} 删除或 404
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.routes.journals import (
    create_journal,
    delete_journal,
    list_journals,
    update_journal,
)
from app.models.journal_entry import JournalEntry
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.schemas.journal import JournalCreate, JournalUpdate

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _make_portfolio(db_session, name="QA-组合") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="simulated",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


def _make_symbol(db_session, symbol="600000") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market="cn",
        theme="银行",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_journal(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    entry_type: str = "trade",
    title: str = "QA-日志",
    content: str | None = "测试内容",
    follow_system: int = 1,
    created_at: datetime | None = None,
) -> JournalEntry:
    """直接构造 JournalEntry 对象，跳过路由层的校验，用于 list/update/delete 测试准备数据。"""
    journal = JournalEntry(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        entry_type=entry_type,
        title=title,
        content=content,
        follow_system=follow_system,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db_session.add(journal)
    db_session.commit()
    db_session.refresh(journal)
    return journal


# ----------------------------------------------------------------------------
# 1. GET /journals 列表
# ----------------------------------------------------------------------------

def test_list_journals_empty_returns_empty_list(db_session):
    """【P2-3 API 测试】无日志时 list_journals 返回空列表。"""
    result = list_journals(portfolio_id=None, symbol_id=None, entry_type=None, db=db_session)
    assert result == []


def test_list_journals_returns_all(db_session):
    """【P2-3 API 测试】list_journals 无过滤条件时返回全部日志。"""
    pf = _make_portfolio(db_session, name="QA-List-All")
    sym = _make_symbol(db_session, symbol="600001")
    _make_journal(db_session, pf.id, sym.id, title="QA-J1")
    _make_journal(db_session, pf.id, sym.id, title="QA-J2")

    result = list_journals(portfolio_id=None, symbol_id=None, entry_type=None, db=db_session)
    assert len(result) == 2
    titles = {j.title for j in result}
    assert titles == {"QA-J1", "QA-J2"}


def test_list_journals_filters_by_portfolio_id(db_session):
    """【P2-3 API 测试】list_journals 按 portfolio_id 过滤。"""
    pf1 = _make_portfolio(db_session, name="QA-PF1")
    pf2 = _make_portfolio(db_session, name="QA-PF2")
    sym = _make_symbol(db_session, symbol="600002")
    _make_journal(db_session, pf1.id, sym.id, title="QA-PF1-J")
    _make_journal(db_session, pf2.id, sym.id, title="QA-PF2-J")

    result = list_journals(portfolio_id=pf1.id, symbol_id=None, entry_type=None, db=db_session)
    assert len(result) == 1
    assert result[0].title == "QA-PF1-J"
    assert result[0].portfolio_id == pf1.id


def test_list_journals_filters_by_symbol_id(db_session):
    """【P2-3 API 测试】list_journals 按 symbol_id 过滤。"""
    pf = _make_portfolio(db_session, name="QA-PF-Sym")
    sym1 = _make_symbol(db_session, symbol="600010")
    sym2 = _make_symbol(db_session, symbol="600011")
    _make_journal(db_session, pf.id, sym1.id, title="QA-Sym1-J")
    _make_journal(db_session, pf.id, sym2.id, title="QA-Sym2-J")

    result = list_journals(portfolio_id=None, symbol_id=sym1.id, entry_type=None, db=db_session)
    assert len(result) == 1
    assert result[0].symbol_id == sym1.id


def test_list_journals_filters_by_entry_type(db_session):
    """【P2-3 API 测试】list_journals 按 entry_type 过滤。"""
    pf = _make_portfolio(db_session, name="QA-PF-Type")
    sym = _make_symbol(db_session, symbol="600020")
    _make_journal(db_session, pf.id, sym.id, entry_type="trade", title="QA-Trade")
    _make_journal(db_session, pf.id, sym.id, entry_type="review", title="QA-Review")

    result = list_journals(portfolio_id=None, symbol_id=None, entry_type="trade", db=db_session)
    assert len(result) == 1
    assert result[0].entry_type == "trade"
    assert result[0].title == "QA-Trade"


def test_list_journals_orders_by_created_at_desc(db_session):
    """【P2-3 API 测试】list_journals 按 created_at 降序返回（最新在前）。"""
    pf = _make_portfolio(db_session, name="QA-PF-Order")
    sym = _make_symbol(db_session, symbol="600030")
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _make_journal(db_session, pf.id, sym.id, title="QA-Old", created_at=base)
    _make_journal(db_session, pf.id, sym.id, title="QA-New", created_at=base + timedelta(days=2))
    _make_journal(db_session, pf.id, sym.id, title="QA-Mid", created_at=base + timedelta(days=1))

    result = list_journals(portfolio_id=None, symbol_id=None, entry_type=None, db=db_session)
    assert len(result) == 3
    assert result[0].title == "QA-New"
    assert result[1].title == "QA-Mid"
    assert result[2].title == "QA-Old"


# ----------------------------------------------------------------------------
# 2. POST /journals 创建
# ----------------------------------------------------------------------------

def test_create_journal_success(db_session):
    """【P2-3 API 测试】create_journal 成功创建日志，follow_system bool 被转为 int。"""
    pf = _make_portfolio(db_session, name="QA-Create")
    sym = _make_symbol(db_session, symbol="600100")
    payload = JournalCreate(
        portfolio_id=pf.id,
        symbol_id=sym.id,
        entry_type="trade",
        title="QA-新建日志",
        content="测试内容",
        follow_system=True,
        outcome="win",
        review_note="按系统执行",
        stage="accumulate",
        action="buy",
    )

    result = create_journal(payload, db=db_session)
    assert result.id is not None
    assert result.portfolio_id == pf.id
    assert result.symbol_id == sym.id
    assert result.entry_type == "trade"
    assert result.title == "QA-新建日志"
    assert result.content == "测试内容"
    # follow_system 在模型中是 int，路由层应将 bool 转为 int
    assert result.follow_system == 1
    assert result.outcome == "win"
    assert result.stage == "accumulate"
    assert result.action == "buy"


def test_create_journal_follow_system_false_converts_to_zero(db_session):
    """【P2-3 API 测试】create_journal follow_system=False 时转为 0。"""
    pf = _make_portfolio(db_session, name="QA-Create-False")
    sym = _make_symbol(db_session, symbol="600101")
    payload = JournalCreate(
        portfolio_id=pf.id,
        symbol_id=sym.id,
        entry_type="review",
        title="QA-未按系统",
        follow_system=False,
    )

    result = create_journal(payload, db=db_session)
    assert result.follow_system == 0


def test_create_journal_portfolio_not_found_raises_404(db_session):
    """【P2-3 API 测试】create_journal portfolio 不存在抛 404。"""
    sym = _make_symbol(db_session, symbol="600200")
    payload = JournalCreate(
        portfolio_id=99999,
        symbol_id=sym.id,
        entry_type="trade",
        title="QA-无组合",
    )
    with pytest.raises(HTTPException) as exc:
        create_journal(payload, db=db_session)
    assert exc.value.status_code == 404
    assert "Portfolio" in exc.value.detail or "portfolio" in exc.value.detail.lower()


def test_create_journal_symbol_not_found_raises_404(db_session):
    """【P2-3 API 测试】create_journal symbol 不存在抛 404。"""
    pf = _make_portfolio(db_session, name="QA-Create-NoSym")
    payload = JournalCreate(
        portfolio_id=pf.id,
        symbol_id=99999,
        entry_type="trade",
        title="QA-无标的",
    )
    with pytest.raises(HTTPException) as exc:
        create_journal(payload, db=db_session)
    assert exc.value.status_code == 404


def test_create_journal_trade_setup_not_found_raises_404(db_session):
    """【P2-3 API 测试】create_journal trade_setup_id 不存在抛 404。"""
    pf = _make_portfolio(db_session, name="QA-Create-NoSetup")
    sym = _make_symbol(db_session, symbol="600300")
    payload = JournalCreate(
        portfolio_id=pf.id,
        symbol_id=sym.id,
        entry_type="trade",
        title="QA-无计划",
        trade_setup_id=99999,
    )
    with pytest.raises(HTTPException) as exc:
        create_journal(payload, db=db_session)
    assert exc.value.status_code == 404
    assert "Trade setup" in exc.value.detail or "trade setup" in exc.value.detail.lower()


# ----------------------------------------------------------------------------
# 3. PATCH /journals/{id} 部分更新
# ----------------------------------------------------------------------------

def test_update_journal_partial_update(db_session):
    """【P2-3 API 测试】update_journal 部分更新只修改传入字段，未传字段保持原值。"""
    pf = _make_portfolio(db_session, name="QA-Update")
    sym = _make_symbol(db_session, symbol="600400")
    journal = _make_journal(
        db_session, pf.id, sym.id,
        title="QA-原标题", content="原内容", follow_system=0,
    )

    payload = JournalUpdate(title="QA-新标题", follow_system=True)
    result = update_journal(journal.id, payload, db=db_session)
    assert result.title == "QA-新标题"
    # follow_system bool 应被转为 int
    assert result.follow_system == 1
    # 未传字段保持原值
    assert result.content == "原内容"


def test_update_journal_not_found_raises_404(db_session):
    """【P2-3 API 测试】update_journal journal 不存在抛 404。"""
    payload = JournalUpdate(title="QA-不存在")
    with pytest.raises(HTTPException) as exc:
        update_journal(99999, payload, db=db_session)
    assert exc.value.status_code == 404
    assert "Journal" in exc.value.detail or "journal" in exc.value.detail.lower()


# ----------------------------------------------------------------------------
# 4. DELETE /journals/{id}
# ----------------------------------------------------------------------------

def test_delete_journal_success(db_session):
    """【P2-3 API 测试】delete_journal 删除成功返回 {"deleted": id}。"""
    pf = _make_portfolio(db_session, name="QA-Delete")
    sym = _make_symbol(db_session, symbol="600500")
    journal = _make_journal(db_session, pf.id, sym.id, title="QA-待删除")

    result = delete_journal(journal.id, db=db_session)
    assert result == {"deleted": journal.id}
    # 验证已从数据库删除
    assert db_session.get(JournalEntry, journal.id) is None


def test_delete_journal_not_found_raises_404(db_session):
    """【P2-3 API 测试】delete_journal journal 不存在抛 404。"""
    with pytest.raises(HTTPException) as exc:
        delete_journal(99999, db=db_session)
    assert exc.value.status_code == 404
