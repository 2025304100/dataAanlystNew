"""白盒测试 - 自选股与组合 API (P1-3)。

覆盖 app/api/routes/watchlists.py 路由层逻辑：
1. GET /watchlists 列表
2. POST /watchlists 创建（同名查重 409）
3. GET /watchlists/{id}/items 列表
4. POST /watchlists/{id}/items 添加（symbol 重复 409 / watchlist 不存在 404）
5. DELETE /watchlists/{id}/items/{symbol_id} 删除或 404
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes.watchlists import (
    add_watchlist_item,
    create_watchlist,
    delete_watchlist_item,
    list_watchlist_items,
    list_watchlists,
)
from app.models.symbol import Symbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.watchlist import WatchlistCreate, WatchlistItemCreate

pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _make_watchlist(db_session, name="QA-自选", list_type="custom") -> Watchlist:
    wl = Watchlist(name=name, list_type=list_type, description="qa")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


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


# ----------------------------------------------------------------------------
# 1. GET /watchlists 列表
# ----------------------------------------------------------------------------

def test_list_watchlists_empty_returns_empty_list(db_session):
    """【P1-3 API 测试】无自选股分组时 list_watchlists 返回空列表。"""
    result = list_watchlists(db=db_session)
    assert result == []


def test_list_watchlists_returns_all(db_session):
    """【P1-3 API 测试】list_watchlists 返回全部自选股分组。"""
    _make_watchlist(db_session, name="QA-A")
    _make_watchlist(db_session, name="QA-B")

    result = list_watchlists(db=db_session)
    assert len(result) == 2
    names = {wl.name for wl in result}
    assert names == {"QA-A", "QA-B"}


# ----------------------------------------------------------------------------
# 2. POST /watchlists 创建
# ----------------------------------------------------------------------------

def test_create_watchlist_success(db_session):
    """【P1-3 API 测试】create_watchlist 成功创建自选股分组。"""
    payload = WatchlistCreate(name="QA-New", list_type="custom", description="测试")
    result = create_watchlist(payload, db=db_session)
    assert result.id is not None
    assert result.name == "QA-New"
    assert result.list_type == "custom"
    assert result.description == "测试"


def test_create_watchlist_duplicate_name_returns_409(db_session):
    """【P1-3 API 测试】create_watchlist 同名查重抛 409。

    防止前端重复创建相同名称的自选股分组。
    """
    _make_watchlist(db_session, name="QA-Dup")
    payload = WatchlistCreate(name="QA-Dup", list_type="custom")
    with pytest.raises(HTTPException) as exc:
        create_watchlist(payload, db=db_session)
    assert exc.value.status_code == 409
    assert "exists" in exc.value.detail.lower() or "already" in exc.value.detail.lower()


# ----------------------------------------------------------------------------
# 3. GET /watchlists/{id}/items 列表
# ----------------------------------------------------------------------------

def test_list_watchlist_items_returns_items(db_session):
    """【P1-3 API 测试】list_watchlist_items 返回自选股列表。"""
    wl = _make_watchlist(db_session, name="QA-Items")
    sym1 = _make_symbol(db_session, symbol="600001")
    sym2 = _make_symbol(db_session, symbol="600002")
    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym1.id))
    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym2.id))
    db_session.commit()

    result = list_watchlist_items(wl.id, db=db_session)
    assert len(result) == 2
    symbol_ids = {item.symbol_id for item in result}
    assert symbol_ids == {sym1.id, sym2.id}


def test_list_watchlist_items_watchlist_not_found_raises_404(db_session):
    """【P1-3 API 测试】watchlist 不存在时 list_watchlist_items 抛 404。"""
    with pytest.raises(HTTPException) as exc:
        list_watchlist_items(99999, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 4. POST /watchlists/{id}/items 添加
# ----------------------------------------------------------------------------

def test_add_watchlist_item_success(db_session):
    """【P1-3 API 测试】add_watchlist_item 成功添加自选股。"""
    wl = _make_watchlist(db_session, name="QA-Add")
    sym = _make_symbol(db_session, symbol="600010")
    payload = WatchlistItemCreate(symbol_id=sym.id, note="测试笔记")

    result = add_watchlist_item(wl.id, payload, db=db_session)
    assert result.id is not None
    assert result.watchlist_id == wl.id
    assert result.symbol_id == sym.id
    assert result.note == "测试笔记"


def test_add_watchlist_item_duplicate_symbol_returns_409(db_session):
    """【P1-3 API 测试】add_watchlist_item symbol 已存在抛 409。"""
    wl = _make_watchlist(db_session, name="QA-DupSym")
    sym = _make_symbol(db_session, symbol="600011")
    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id))
    db_session.commit()

    payload = WatchlistItemCreate(symbol_id=sym.id)
    with pytest.raises(HTTPException) as exc:
        add_watchlist_item(wl.id, payload, db=db_session)
    assert exc.value.status_code == 409
    assert "already" in exc.value.detail.lower()


def test_add_watchlist_item_watchlist_not_found_returns_404(db_session):
    """【P1-3 API 测试】add_watchlist_item watchlist 不存在抛 404。"""
    sym = _make_symbol(db_session, symbol="600012")
    payload = WatchlistItemCreate(symbol_id=sym.id)
    with pytest.raises(HTTPException) as exc:
        add_watchlist_item(99999, payload, db=db_session)
    assert exc.value.status_code == 404


def test_add_watchlist_item_symbol_not_found_returns_404(db_session):
    """【P1-3 API 测试】add_watchlist_item symbol 不存在抛 404。"""
    wl = _make_watchlist(db_session, name="QA-NoSym")
    payload = WatchlistItemCreate(symbol_id=99999)
    with pytest.raises(HTTPException) as exc:
        add_watchlist_item(wl.id, payload, db=db_session)
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------------
# 5. DELETE /watchlists/{id}/items/{symbol_id}
# ----------------------------------------------------------------------------

def test_delete_watchlist_item_success(db_session):
    """【P1-3 API 测试】delete_watchlist_item 删除成功返回 {"deleted": True}。"""
    wl = _make_watchlist(db_session, name="QA-Del")
    sym = _make_symbol(db_session, symbol="600020")
    db_session.add(WatchlistItem(watchlist_id=wl.id, symbol_id=sym.id))
    db_session.commit()

    result = delete_watchlist_item(wl.id, sym.id, db=db_session)
    assert result == {"deleted": True}
    # 验证已删除
    items = list_watchlist_items(wl.id, db=db_session)
    assert len(items) == 0


def test_delete_watchlist_item_not_found_raises_404(db_session):
    """【P1-3 API 测试】delete_watchlist_item 不存在抛 404。"""
    wl = _make_watchlist(db_session, name="QA-Del404")
    with pytest.raises(HTTPException) as exc:
        delete_watchlist_item(wl.id, 99999, db=db_session)
    assert exc.value.status_code == 404
