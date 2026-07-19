"""组合整体回测服务（P2-2）。

在已有 run_backtest 基础上，提供"组合级"回测入口：
- 自动从组合持仓 + 最新 scan 候选池推导 symbol_ids
- 自动构造与 auto_trade 信号逻辑一致的 rule_config（基于 Score.action）
- 复用 portfolio 的 active rule 限制仓位与持仓数

前提：组合 auto_trade_enabled=1（组内标的标签都是自动下单/程序）。
否则回测的信号源（Score.action）与实际执行逻辑不一致，结果无意义。

设计要点：
- 不重新实现回测引擎，直接调用 backtest.run_backtest
- rule_config 使用 v1 格式（buy_conditions.actions / sell_conditions.score_actions）
- position_config 从 PortfolioRule 映射（max_single_position_pct → value, max_open_positions → max_positions）
- initial_capital 沿用 run_backtest 默认行为（portfolio.total_capital），保持与单标的回测一致
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.scan import ScanResult, ScanRun
from app.services.allocation import get_active_rule
from app.services.backtest import run_backtest

logger = logging.getLogger(__name__)

# 与 auto_trade_task 保持一致的信号集合
_BUY_ACTIONS = ["open", "buy_dip"]
_SELL_ACTIONS = ["exit", "reduce"]


def _derive_symbol_ids(db: Session, portfolio_id: int) -> list[int]:
    """从组合当前持仓 + 最新 scan executable 候选推导回测标的列表。

    逻辑：
    1. 当前持仓的 symbol_id（这些可能在回测期间被卖出）
    2. 最新一次成功 scan 的 executable 候选 symbol_id（这些可能在回测期间被买入）
    3. 去重后返回

    若两者都为空，抛 ValueError（无标的可回测）。
    """
    symbol_ids: set[int] = set()

    # 当前持仓
    positions = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ).scalars().all()
    for pos in positions:
        symbol_ids.add(pos.symbol_id)

    # 最新 scan 候选
    latest_run = db.execute(
        select(ScanRun)
        .where(ScanRun.portfolio_id == portfolio_id, ScanRun.status == "done")
        .order_by(desc(ScanRun.id))
        .limit(1)
    ).scalars().first()
    if latest_run is not None:
        candidates = db.execute(
            select(ScanResult).where(
                ScanResult.scan_run_id == latest_run.id,
                ScanResult.result_type == "executable",
            )
        ).scalars().all()
        for cand in candidates:
            symbol_ids.add(cand.symbol_id)

    if not symbol_ids:
        raise ValueError(
            f"Portfolio {portfolio_id} has no positions and no scan candidates. "
            "Cannot run whole-portfolio backtest."
        )

    return sorted(symbol_ids)


def _build_rule_config(rule: PortfolioRule | None) -> dict[str, Any]:
    """从 PortfolioRule 构造与 auto_trade 信号逻辑一致的 v1 rule_config。

    信号逻辑（与 auto_trade_task 对齐）：
    - 买入：Score.action ∈ {open, buy_dip}
    - 卖出：Score.action ∈ {exit, reduce}

    仓位限制（从 PortfolioRule 映射）：
    - position_config.value = max_single_position_pct（每标的最大仓位百分比）
    - position_config.max_positions = max_open_positions（最大持仓数）
    """
    if rule is None:
        # 无 active rule 时使用保守默认值
        max_single = 0.1
        max_positions = 5
    else:
        max_single = float(rule.max_single_position_pct or 0.1)
        max_positions = int(rule.max_open_positions or 5)

    return {
        "buy_conditions": {
            "actions": _BUY_ACTIONS,
        },
        "sell_conditions": {
            "score_actions": _SELL_ACTIONS,
        },
        "position_config": {
            "type": "fixed_pct",
            "value": max_single,
            "max_positions": max_positions,
        },
        "execution_config": {
            "entry_timing": "signal_close",
            "exit_timing": "signal_close",
        },
    }


def run_portfolio_backtest(
    db: Session,
    portfolio_id: int,
    *,
    start_date: date,
    end_date: date,
    run_name: str | None = None,
) -> dict[str, Any]:
    """对组合执行整体回测。

    Args:
        db: 数据库会话
        portfolio_id: 目标组合 ID
        start_date: 回测起始日期
        end_date: 回测结束日期
        run_name: 回测名称，None 时自动生成

    Returns:
        BacktestRun 的字典形式（含 id, status, trade_count 等字段）

    Raises:
        ValueError: 组合不存在/非模拟/未开启自动交易/无标的可回测/total_capital<=0
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError(f"Portfolio {portfolio_id} is not simulated")
    if not portfolio.auto_trade_enabled:
        raise ValueError(
            f"Portfolio {portfolio_id} auto_trade_enabled is 0. "
            "Whole-portfolio backtest requires auto_trade_enabled=1 "
            "(signals are based on Score.action which mirrors auto-trade logic)."
        )
    if not portfolio.total_capital or float(portfolio.total_capital) <= 0:
        raise ValueError(
            f"Portfolio {portfolio_id} total_capital is {portfolio.total_capital}. "
            "Configure total_capital before running backtest."
        )

    # 推导标的列表
    symbol_ids = _derive_symbol_ids(db, portfolio_id)

    # 构造 rule_config
    rule = get_active_rule(db, portfolio_id)
    rule_config = _build_rule_config(rule)

    # 回测名称
    if run_name is None:
        run_name = f"组合整体回测 {start_date.isoformat()}~{end_date.isoformat()}"

    # 调用已有 run_backtest（initial_capital 由 run_backtest 内部从 portfolio.total_capital 取）
    run = run_backtest(
        db=db,
        portfolio_id=portfolio_id,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        rule_config=rule_config,
        run_name=run_name,
    )

    return {
        "run_id": run.id,
        "portfolio_id": portfolio_id,
        "symbol_ids": symbol_ids,
        "symbol_count": len(symbol_ids),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "initial_capital": float(portfolio.total_capital),
        "status": run.status,
        "run_name": run.run_name,
    }


__all__ = [
    "run_portfolio_backtest",
]
