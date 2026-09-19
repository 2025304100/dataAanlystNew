"""get_backtest_explanation 工具：返回回测解释。"""
from __future__ import annotations

import json
import logging

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def get_backtest_explanation(db: Session, context: ContextPack) -> dict:
    """返回回测解释：策略、参数、结果、关键指标。

    依赖 context.references.backtest_id 或 portfolio_id（取最近一次回测）。

    Returns:
        {
            "status": "ok",
            "data": {
                "run": {...},  # 回测基本信息
                "config": {...},  # 策略配置
                "metrics": {...},  # 关键指标
                "trades_count": int,
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    backtest_id = context.references.get("backtest_id")
    portfolio_id = context.references.get("portfolio_id")

    try:
        from app.models.backtest import BacktestRun, BacktestTrade
    except Exception as exc:
        logger.warning("get_backtest_explanation model import failed: %s", exc)
        return {"status": "no_data", "message": "回测模型不可用"}

    # 查询回测记录
    try:
        stmt = select(BacktestRun)
        if backtest_id is not None:
            stmt = stmt.where(BacktestRun.id == backtest_id)
        elif portfolio_id is not None:
            stmt = stmt.where(BacktestRun.portfolio_id == portfolio_id)
        else:
            return {"status": "no_data", "message": "未指定 backtest_id 或 portfolio_id"}
        stmt = stmt.order_by(desc(BacktestRun.id)).limit(1)
        run = db.execute(stmt).scalars().first()
    except Exception as exc:
        logger.warning("get_backtest_explanation run query failed: %s", exc)
        return {"status": "no_data", "message": f"回测查询失败：{type(exc).__name__}"}

    if run is None:
        return {"status": "no_data", "message": "无可用回测记录"}

    # 解析策略配置
    rule_config = {}
    if run.rule_config_json:
        try:
            rule_config = json.loads(run.rule_config_json) or {}
        except (ValueError, TypeError):
            rule_config = {}

    cost_config = {}
    if run.cost_config_json:
        try:
            cost_config = json.loads(run.cost_config_json) or {}
        except (ValueError, TypeError):
            cost_config = {}

    symbols: list = []
    if run.symbols_json:
        try:
            symbols = json.loads(run.symbols_json) or []
        except (ValueError, TypeError):
            symbols = []

    # 关键指标
    metrics = {
        "total_return": float(run.total_return) if run.total_return is not None else None,
        "total_return_pct": float(run.total_return_pct) if run.total_return_pct is not None else None,
        "max_drawdown": float(run.max_drawdown) if run.max_drawdown is not None else None,
        "max_drawdown_pct": float(run.max_drawdown_pct) if run.max_drawdown_pct is not None else None,
        "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio is not None else None,
        "win_rate": float(run.win_rate) if run.win_rate is not None else None,
        "profit_factor": float(run.profit_factor) if run.profit_factor is not None else None,
        "trade_count": run.trade_count,
        "avg_holding_days": float(run.avg_holding_days) if run.avg_holding_days is not None else None,
    }

    # 交易数（实际表内计数）
    trades_count = 0
    try:
        from sqlalchemy import func as sa_func
        trades_count = db.execute(
            select(sa_func.count(BacktestTrade.id)).where(BacktestTrade.run_id == run.id)
        ).scalar_one()
    except Exception as exc:
        logger.debug("get_backtest_explanation trades count failed: %s", exc)
        trades_count = int(run.trade_count or 0)

    return {
        "status": "ok",
        "data": {
            "run": {
                "id": run.id,
                "run_name": run.run_name,
                "portfolio_id": run.portfolio_id,
                "status": run.status,
                "start_date": run.start_date.isoformat() if run.start_date else None,
                "end_date": run.end_date.isoformat() if run.end_date else None,
                "initial_capital": float(run.initial_capital) if run.initial_capital is not None else None,
                "score_weight_mode": run.score_weight_mode,
                "factor_model_run_id": run.factor_model_run_id,
                "engine_name": run.engine_name,
                "engine_version": run.engine_version,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "error_message": run.error_message,
            },
            "config": {
                "symbols": symbols,
                "rule_config": rule_config,
                "cost_config": cost_config,
            },
            "metrics": metrics,
            "trades_count": int(trades_count),
        },
    }


__all__ = ["get_backtest_explanation"]
