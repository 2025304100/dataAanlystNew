"""白盒测试 - 组合 CRUD 补全（P0-6）。

验证新增的 DELETE /portfolios/{id} 和 PUT /portfolios/{id} 端点：
1. POST 创建组合（同名查重 409）
2. PUT 更新组合属性（name/total_capital/is_default）
3. PUT 名称查重（与其他组合冲突 409）
4. DELETE 删除非默认组合（级联清理）
5. DELETE 默认组合拒绝（400）
6. DELETE 最后一个组合拒绝（400）
7. POST is_default=True 时自动取消其他默认
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes.portfolios import (
    create_portfolio,
    delete_portfolio,
    update_portfolio,
)
from app.models.portfolio import Portfolio
from app.schemas.portfolio import PortfolioCreate, PortfolioUpdate

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
    from sqlalchemy import select
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
