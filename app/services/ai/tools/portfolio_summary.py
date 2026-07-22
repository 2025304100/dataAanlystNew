"""get_portfolio_summary 工具：返回组合摘要。"""
from __future__ import annotations

import logging

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def get_portfolio_summary(db: Session, context: ContextPack) -> dict:
    """返回组合摘要：持仓、净值、成员、风控。

    依赖 context.references.portfolio_id。

    Returns:
        {
            "status": "ok",
            "data": {
                "portfolio": {...},
                "account_summary": {...},  # 现金/市值/总权益
                "positions": [...],
                "members": [...],
                "rules": [...],
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    portfolio_id = context.references.get("portfolio_id")
    if portfolio_id is None:
        return {"status": "no_data", "message": "未指定 portfolio_id"}

    # 组合基本信息
    try:
        from app.models.portfolio import Portfolio, PortfolioRule, Position
        portfolio = db.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id)
        ).scalars().first()
    except Exception as exc:
        logger.warning("get_portfolio_summary portfolio query failed: %s", exc)
        return {"status": "no_data", "message": f"组合查询失败：{type(exc).__name__}"}

    if portfolio is None:
        return {"status": "no_data", "message": f"组合 portfolio_id={portfolio_id} 不存在"}

    portfolio_info = {
        "id": portfolio.id,
        "name": portfolio.name,
        "account_type": portfolio.account_type,
        "total_capital": float(portfolio.total_capital) if portfolio.total_capital is not None else 0.0,
        "investable_ratio": float(portfolio.investable_ratio) if portfolio.investable_ratio is not None else 0.0,
        "cash_reserve_ratio": float(portfolio.cash_reserve_ratio) if portfolio.cash_reserve_ratio is not None else 0.0,
        "currency": portfolio.currency,
        "auto_trade_enabled": bool(portfolio.auto_trade_enabled),
        "auto_trade_last_run_at": portfolio.auto_trade_last_run_at.isoformat() if portfolio.auto_trade_last_run_at else None,
    }

    # 账户摘要（仅模拟账户）
    account_summary: dict | None = None
    if portfolio.account_type == "simulated":
        try:
            from app.services.sim_accounts import build_sim_account_summary
            account_summary = build_sim_account_summary(db, portfolio)
        except Exception as exc:
            logger.warning("get_portfolio_summary account summary failed: %s", exc)

    # 持仓
    positions: list[dict] = []
    try:
        rows = db.execute(
            select(Position)
            .where(Position.portfolio_id == portfolio_id)
            .order_by(desc(Position.market_value))
        ).scalars().all()
        for p in rows:
            positions.append({
                "symbol_id": p.symbol_id,
                "quantity": float(p.quantity) if p.quantity is not None else 0.0,
                "avg_cost": float(p.avg_cost) if p.avg_cost is not None else None,
                "latest_price": float(p.latest_price) if p.latest_price is not None else None,
                "market_value": float(p.market_value) if p.market_value is not None else 0.0,
                "position_pct": float(p.position_pct) if p.position_pct is not None else 0.0,
                "asset_type": p.asset_type,
                "theme": p.theme,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            })
    except Exception as exc:
        logger.warning("get_portfolio_summary positions query failed: %s", exc)

    # 组合成员
    members: list[dict] = []
    try:
        from app.models.portfolio_member import PortfolioMember
        rows = db.execute(
            select(PortfolioMember)
            .where(
                PortfolioMember.portfolio_id == portfolio_id,
                PortfolioMember.effective_to.is_(None),
            )
            .order_by(PortfolioMember.priority.desc())
        ).scalars().all()
        for m in rows:
            members.append({
                "id": m.id,
                "symbol_id": m.symbol_id,
                "status": m.status,
                "execution_mode": m.execution_mode,
                "source_type": m.source_type,
                "priority": m.priority,
                "manual_lock": bool(m.manual_lock),
                "effective_from": m.effective_from.isoformat() if m.effective_from else None,
            })
    except Exception as exc:
        logger.warning("get_portfolio_summary members query failed: %s", exc)

    # 规则
    rules: list[dict] = []
    try:
        rows = db.execute(
            select(PortfolioRule)
            .where(PortfolioRule.portfolio_id == portfolio_id)
            .order_by(PortfolioRule.id.desc())
        ).scalars().all()
        for r in rows:
            rules.append({
                "id": r.id,
                "rule_name": r.rule_name,
                "max_single_position_pct": float(r.max_single_position_pct) if r.max_single_position_pct is not None else None,
                "max_sector_position_pct": float(r.max_sector_position_pct) if r.max_sector_position_pct is not None else None,
                "max_stock_position_pct": float(r.max_stock_position_pct) if r.max_stock_position_pct is not None else None,
                "max_loss_per_trade_pct": float(r.max_loss_per_trade_pct) if r.max_loss_per_trade_pct is not None else None,
                "max_open_positions": r.max_open_positions,
                "is_active": bool(r.is_active),
            })
    except Exception as exc:
        logger.warning("get_portfolio_summary rules query failed: %s", exc)

    return {
        "status": "ok",
        "data": {
            "portfolio": portfolio_info,
            "account_summary": account_summary,
            "positions": positions,
            "members": members,
            "rules": rules,
        },
    }


__all__ = ["get_portfolio_summary"]
