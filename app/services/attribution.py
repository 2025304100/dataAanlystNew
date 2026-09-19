"""组合绩效归因服务（WP8）。

扩展绩效归因维度，让用户能解释一个组合收益来自哪些成员和执行模式。

6 类归因维度：
1. 按组合成员贡献（attribute_by_member）
2. 按手动/确认/自动执行模式贡献（attribute_by_execution_mode）
3. 按候选来源或观察标签贡献（attribute_by_source）
4. 按规则版本/信号类型/退出原因贡献（attribute_by_rule_signal）
5. 回测与模拟账户同期偏差（compute_backtest_vs_sim_diff）
6. 成本/滑点/未成交/风控阻断影响（attribute_cost_impact）

统一入口 get_attribution_report 聚合全部维度，并生成文本总结。

数据来源：
- SimTrade（已平仓卖出交易，realized_pnl 非 None）join SimOrder（归因字段）
- BacktestRun（回测快照，用于回测 vs 模拟偏差）
- PortfolioEquitySnapshot（组合净值时序，用于模拟绩效计算）

样本数提示：
- 交易笔数 < MIN_TRADE_SAMPLE 或成员数 < MIN_MEMBER_SAMPLE 时标注"样本不足，结论仅供参考"
- 不展示具有误导性的稳定结论
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestRun
from app.models.portfolio import Portfolio
from app.models.sim_account import SimOrder, SimTrade
from app.services.portfolio_performance import compute_portfolio_performance


logger = logging.getLogger(__name__)


# 样本数阈值：低于此值时标注"样本不足，结论仅供参考"
MIN_TRADE_SAMPLE = 5
MIN_MEMBER_SAMPLE = 3

# source_type 人类可读标签映射（覆盖 SimOrder 与 PortfolioMember 所有可能值）
_SOURCE_LABELS = {
    "member": "组合成员",
    "scan": "扫描结果",
    "legacy": "历史回填",
    "manual": "手动",
    "observation": "观察项",
    "candidate": "候选",
    "legacy_position": "持仓回填",
    "unknown": "未知",
}

# execution_mode 人类可读标签映射
_EXECUTION_MODE_LABELS = {
    "manual": "手动",
    "confirm": "确认",
    "auto": "自动",
    "unknown": "未知",
}

# 风控阻断相关的 rejection_code 关键字（用于区分风控阻断与其他拒绝原因）
_RISK_BLOCK_KEYWORDS = ("risk", "风控", "limit", "exceed", "blocked", "violation")


def _sample_warning(sample_count: int, threshold: int = MIN_TRADE_SAMPLE) -> str | None:
    """样本数不足时返回提示文本，否则返回 None。"""
    if sample_count < threshold:
        return "样本不足，结论仅供参考"
    return None


def _collect_attribution_trades(
    db: Session, portfolio_id: int, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    """收集已平仓交易及其归因字段（SimTrade join SimOrder）。

    返回每笔交易的归因维度字段，供各 attribute_by_* 函数分组。
    仅包含 side='sell' 且 realized_pnl 非 None 的交易（已平仓）。
    """
    stmt = (
        select(SimTrade, SimOrder)
        .join(SimOrder, SimTrade.order_id == SimOrder.id)
        .where(
            SimTrade.portfolio_id == portfolio_id,
            SimTrade.side == "sell",
            SimTrade.realized_pnl.is_not(None),
        )
        .order_by(SimTrade.created_at.asc())
    )
    if start_date is not None:
        stmt = stmt.where(
            SimTrade.created_at >= datetime.combine(start_date, datetime.min.time())
        )
    if end_date is not None:
        end_dt = datetime.combine(end_date, datetime.max.time())
        stmt = stmt.where(SimTrade.created_at <= end_dt)

    rows = db.execute(stmt).all()
    trades: list[dict[str, Any]] = []
    for trade, order in rows:
        trades.append({
            "trade_id": trade.id,
            "order_id": trade.order_id,
            "symbol_id": trade.symbol_id,
            "pnl": float(trade.realized_pnl or 0),
            "fee": float(trade.fee or 0),
            "amount": float(trade.amount or 0),
            "quantity": float(trade.quantity or 0),
            "price": float(trade.price or 0),
            "created_at": trade.created_at,
            # 归因字段（来自 SimOrder）
            "member_id": order.member_id,
            "execution_mode": order.execution_mode,
            "source_type": order.source_type,
            "source_id": order.source_id,
            "signal_id": order.signal_id,
            "rule_version_id": order.rule_version_id,
            "submitted_price": float(order.submitted_price or 0),
            "filled_price": float(order.filled_price or 0),
            "filled_quantity": float(order.filled_quantity or 0),
            "order_fee": float(order.fee or 0),
            "order_status": order.status,
            "rejection_code": order.rejection_code,
            "note": order.note,
            "side": order.side,
        })
    return trades


def _compute_total_pnl(trades: list[dict[str, Any]]) -> float:
    """计算所有交易的总盈亏。"""
    return round(sum(t["pnl"] for t in trades), 2)


def _contribution_pct(group_pnl: float, total_pnl: float) -> float:
    """计算贡献百分比。

    total_pnl 为 0 时返回 0（避免除零）。
    total_pnl 为负时（整体亏损），正向盈利的成员贡献为负，亏损成员贡献为正，
    这种情况下结论可能误导，由调用方在 summary 中标注。
    """
    if total_pnl == 0:
        return 0.0
    return round(group_pnl / total_pnl * 100, 2)


# ============================================================================
# 1. 按组合成员贡献归因
# ============================================================================


def attribute_by_member(
    db: Session, portfolio_id: int, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    """按成员贡献归因。

    返回 [{member_id, symbol_id, contribution_pct, pnl, trade_count, ...}]
    member_id 为 None 的交易归入 "unknown" 组（历史订单未带归因字段）。
    """
    trades = _collect_attribution_trades(db, portfolio_id, start_date, end_date)
    total_pnl = _compute_total_pnl(trades)

    # 按 (member_id, symbol_id) 分组
    groups: dict[tuple, dict[str, Any]] = {}
    for t in trades:
        key = (t["member_id"], t["symbol_id"])
        if key not in groups:
            groups[key] = {
                "member_id": t["member_id"],
                "symbol_id": t["symbol_id"],
                "pnl": 0.0,
                "trade_count": 0,
                "win_count": 0,
            }
        g = groups[key]
        g["pnl"] += t["pnl"]
        g["trade_count"] += 1
        if t["pnl"] > 0:
            g["win_count"] += 1

    result = []
    for key, g in groups.items():
        pnl = round(g["pnl"], 2)
        result.append({
            "member_id": g["member_id"],
            "symbol_id": g["symbol_id"],
            "contribution_pct": _contribution_pct(pnl, total_pnl),
            "pnl": pnl,
            "trade_count": g["trade_count"],
            "win_count": g["win_count"],
            "win_rate": round(g["win_count"] / g["trade_count"], 4) if g["trade_count"] else 0.0,
        })

    # 按 pnl 降序
    result.sort(key=lambda x: x["pnl"], reverse=True)

    member_count = len({t["member_id"] for t in trades if t["member_id"] is not None})
    # 样本不足判定：交易笔数 < MIN_TRADE_SAMPLE 或成员数 < MIN_MEMBER_SAMPLE 任一成立即提示
    warning = _sample_warning(len(trades))
    if warning is None and member_count < MIN_MEMBER_SAMPLE:
        warning = "样本不足，结论仅供参考"
    return {
        "items": result,
        "total_pnl": total_pnl,
        "total_trades": len(trades),
        "member_count": member_count,
        "sample_warning": warning,
    }


# ============================================================================
# 2. 按执行模式归因
# ============================================================================


def attribute_by_execution_mode(
    db: Session, portfolio_id: int, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    """按执行模式归因（auto/confirm/manual）。

    返回 [{execution_mode, contribution_pct, pnl, trade_count, ...}]
    execution_mode 为 None 的交易归入 "unknown" 组。
    """
    trades = _collect_attribution_trades(db, portfolio_id, start_date, end_date)
    total_pnl = _compute_total_pnl(trades)

    groups: dict[str, dict[str, Any]] = {}
    for t in trades:
        mode = t["execution_mode"] or "unknown"
        if mode not in groups:
            groups[mode] = {
                "execution_mode": mode,
                "execution_label": _EXECUTION_MODE_LABELS.get(mode, mode),
                "pnl": 0.0,
                "trade_count": 0,
                "win_count": 0,
            }
        g = groups[mode]
        g["pnl"] += t["pnl"]
        g["trade_count"] += 1
        if t["pnl"] > 0:
            g["win_count"] += 1

    result = []
    for mode, g in groups.items():
        pnl = round(g["pnl"], 2)
        result.append({
            "execution_mode": g["execution_mode"],
            "execution_label": g["execution_label"],
            "contribution_pct": _contribution_pct(pnl, total_pnl),
            "pnl": pnl,
            "trade_count": g["trade_count"],
            "win_count": g["win_count"],
            "win_rate": round(g["win_count"] / g["trade_count"], 4) if g["trade_count"] else 0.0,
        })

    result.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "items": result,
        "total_pnl": total_pnl,
        "total_trades": len(trades),
        "sample_warning": _sample_warning(len(trades)),
    }


# ============================================================================
# 3. 按候选来源或观察标签归因
# ============================================================================


def attribute_by_source(
    db: Session, portfolio_id: int, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    """按候选来源或观察标签归因。

    返回 [{source_type, source_label, contribution_pct, pnl, ...}]
    source_type 为 None 的交易归入 "unknown" 组。
    """
    trades = _collect_attribution_trades(db, portfolio_id, start_date, end_date)
    total_pnl = _compute_total_pnl(trades)

    groups: dict[str, dict[str, Any]] = {}
    for t in trades:
        src = t["source_type"] or "unknown"
        if src not in groups:
            groups[src] = {
                "source_type": src,
                "source_label": _SOURCE_LABELS.get(src, src),
                "pnl": 0.0,
                "trade_count": 0,
                "win_count": 0,
            }
        g = groups[src]
        g["pnl"] += t["pnl"]
        g["trade_count"] += 1
        if t["pnl"] > 0:
            g["win_count"] += 1

    result = []
    for src, g in groups.items():
        pnl = round(g["pnl"], 2)
        result.append({
            "source_type": g["source_type"],
            "source_label": g["source_label"],
            "contribution_pct": _contribution_pct(pnl, total_pnl),
            "pnl": pnl,
            "trade_count": g["trade_count"],
            "win_count": g["win_count"],
            "win_rate": round(g["win_count"] / g["trade_count"], 4) if g["trade_count"] else 0.0,
        })

    result.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "items": result,
        "total_pnl": total_pnl,
        "total_trades": len(trades),
        "sample_warning": _sample_warning(len(trades)),
    }


# ============================================================================
# 4. 按规则版本/信号类型/退出原因归因
# ============================================================================


def _extract_exit_reason(note: str | None) -> str:
    """从 SimOrder.note 中提取退出原因。

    约定：note 若以 "exit:" 前缀开头，则后续内容为退出原因；
    否则使用 note 原值（若非空）或 "unspecified"。
    """
    if not note:
        return "unspecified"
    if note.lower().startswith("exit:"):
        return note[5:].strip() or "unspecified"
    return note


def attribute_by_rule_signal(
    db: Session, portfolio_id: int, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    """按规则版本/信号类型/退出原因归因。

    返回 [{rule_version_id, signal_id, exit_reason, contribution_pct, pnl, ...}]
    按 (rule_version_id, signal_id, exit_reason) 元组分组。
    """
    trades = _collect_attribution_trades(db, portfolio_id, start_date, end_date)
    total_pnl = _compute_total_pnl(trades)

    groups: dict[tuple, dict[str, Any]] = {}
    for t in trades:
        rule_ver = t["rule_version_id"]
        sig_id = t["signal_id"]
        exit_reason = _extract_exit_reason(t["note"])
        key = (rule_ver, sig_id, exit_reason)
        if key not in groups:
            groups[key] = {
                "rule_version_id": rule_ver,
                "signal_id": sig_id,
                "exit_reason": exit_reason,
                "pnl": 0.0,
                "trade_count": 0,
                "win_count": 0,
            }
        g = groups[key]
        g["pnl"] += t["pnl"]
        g["trade_count"] += 1
        if t["pnl"] > 0:
            g["win_count"] += 1

    result = []
    for key, g in groups.items():
        pnl = round(g["pnl"], 2)
        result.append({
            "rule_version_id": g["rule_version_id"],
            "signal_id": g["signal_id"],
            "exit_reason": g["exit_reason"],
            "contribution_pct": _contribution_pct(pnl, total_pnl),
            "pnl": pnl,
            "trade_count": g["trade_count"],
            "win_count": g["win_count"],
            "win_rate": round(g["win_count"] / g["trade_count"], 4) if g["trade_count"] else 0.0,
        })

    result.sort(key=lambda x: x["pnl"], reverse=True)

    return {
        "items": result,
        "total_pnl": total_pnl,
        "total_trades": len(trades),
        "sample_warning": _sample_warning(len(trades)),
    }


# ============================================================================
# 5. 回测与模拟账户同期偏差
# ============================================================================


def compute_backtest_vs_sim_diff(
    db: Session,
    portfolio_id: int,
    backtest_run_id: int,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """回测与模拟账户同期偏差。

    返回 {backtest_metrics, sim_metrics, diff: {return_diff, drawdown_diff, ...}, explanation}
    """
    backtest_run = db.get(BacktestRun, backtest_run_id)
    if backtest_run is None:
        raise ValueError(f"BacktestRun not found: {backtest_run_id}")

    # 回测指标（直接从 BacktestRun 读取已持久化的结果）
    backtest_metrics = {
        "run_id": backtest_run.id,
        "run_name": backtest_run.run_name,
        "total_return_pct": float(backtest_run.total_return_pct or 0),
        "max_drawdown_pct": float(backtest_run.max_drawdown_pct or 0),
        "sharpe_ratio": float(backtest_run.sharpe_ratio or 0),
        "win_rate": float(backtest_run.win_rate or 0),
        "profit_factor": float(backtest_run.profit_factor or 0),
        "trade_count": int(backtest_run.trade_count or 0),
        "avg_holding_days": float(backtest_run.avg_holding_days or 0),
        "start_date": backtest_run.start_date.isoformat() if backtest_run.start_date else None,
        "end_date": backtest_run.end_date.isoformat() if backtest_run.end_date else None,
    }

    # 模拟账户指标（实时计算）
    sim_perf = compute_portfolio_performance(
        db, portfolio_id, start_date=start_date, end_date=end_date, benchmark=None,
    )
    sim_stats = sim_perf["stats"]
    sim_metrics = {
        "total_return_pct": float(sim_stats.get("total_return_pct", 0)),
        "max_drawdown_pct": float(sim_stats.get("max_drawdown_pct", 0)),
        "sharpe_ratio": float(sim_stats.get("sharpe_ratio", 0)),
        "win_rate": float(sim_stats.get("win_rate", 0)),
        "profit_factor": float(sim_stats.get("profit_factor", 0)),
        "trade_count": int(sim_stats.get("trade_count", 0)),
        "avg_holding_days": float(sim_stats.get("avg_holding_days", 0)),
    }

    # 计算偏差（sim - backtest）
    diff = {
        "return_diff_pct": round(sim_metrics["total_return_pct"] - backtest_metrics["total_return_pct"], 4),
        "drawdown_diff_pct": round(sim_metrics["max_drawdown_pct"] - backtest_metrics["max_drawdown_pct"], 4),
        "sharpe_diff": round(sim_metrics["sharpe_ratio"] - backtest_metrics["sharpe_ratio"], 2),
        "win_rate_diff": round(sim_metrics["win_rate"] - backtest_metrics["win_rate"], 4),
        "trade_count_diff": sim_metrics["trade_count"] - backtest_metrics["trade_count"],
    }

    # 生成解释文本
    explanations: list[str] = []
    if abs(diff["return_diff_pct"]) < 0.005:
        explanations.append("模拟与回测收益率基本一致")
    elif diff["return_diff_pct"] > 0:
        explanations.append(f"模拟收益率高于回测 {diff['return_diff_pct']:.2%}，实盘表现优于回测假设")
    else:
        explanations.append(f"模拟收益率低于回测 {abs(diff['return_diff_pct']):.2%}，实盘表现不及回测假设")

    if abs(diff["sharpe_diff"]) >= 0.3:
        if diff["sharpe_diff"] > 0:
            explanations.append(f"夏普比率高出 {diff['sharpe_diff']:.2f}，实盘风险调整后收益更好")
        else:
            explanations.append(f"夏普比率低出 {abs(diff['sharpe_diff']):.2f}，实盘风险调整后收益较弱")

    if diff["trade_count_diff"] != 0:
        explanations.append(
            f"交易笔数{'多' if diff['trade_count_diff'] > 0 else '少'}"
            f" {abs(diff['trade_count_diff'])} 笔"
        )

    # 样本不足提示
    bt_trades = backtest_metrics["trade_count"]
    sim_trades = sim_metrics["trade_count"]
    if bt_trades < MIN_TRADE_SAMPLE or sim_trades < MIN_TRADE_SAMPLE:
        explanations.append("样本不足，结论仅供参考")

    return {
        "backtest_metrics": backtest_metrics,
        "sim_metrics": sim_metrics,
        "diff": diff,
        "explanation": "；".join(explanations) if explanations else "无显著偏差",
        "sample_warning": (
            "样本不足，结论仅供参考"
            if bt_trades < MIN_TRADE_SAMPLE or sim_trades < MIN_TRADE_SAMPLE
            else None
        ),
    }


# ============================================================================
# 6. 成本/滑点/未成交/风控阻断影响
# ============================================================================


def attribute_cost_impact(
    db: Session, portfolio_id: int, start_date: date, end_date: date
) -> dict[str, Any]:
    """成本/滑点/未成交/风控阻断影响。

    返回 {total_cost, slippage_cost, rejected_count, risk_blocked_count, impact_pct}
    """
    # 查询时间范围内所有订单（含已成交和未成交）
    stmt = select(SimOrder).where(SimOrder.portfolio_id == portfolio_id)
    if start_date is not None:
        stmt = stmt.where(
            SimOrder.created_at >= datetime.combine(start_date, datetime.min.time())
        )
    if end_date is not None:
        end_dt = datetime.combine(end_date, datetime.max.time())
        stmt = stmt.where(SimOrder.created_at <= end_dt)

    orders = list(db.execute(stmt).scalars().all())

    total_fee = 0.0  # 手续费总额
    slippage_cost = 0.0  # 滑点成本
    rejected_count = 0  # 未成交/拒绝订单数
    risk_blocked_count = 0  # 风控阻断数
    filled_amount_total = 0.0  # 已成交金额（用于计算 impact_pct）

    for o in orders:
        fee = float(o.fee or 0)
        total_fee += fee

        # 滑点成本 = |filled_price - submitted_price| * filled_quantity
        submitted = float(o.submitted_price or 0)
        filled = float(o.filled_price or 0)
        filled_qty = float(o.filled_quantity or 0)
        if submitted > 0 and filled_qty > 0:
            slippage_cost += abs(filled - submitted) * filled_qty

        filled_amount_total += float(o.filled_amount or 0)

        # 未成交/拒绝订单
        if o.status and o.status not in ("filled", "partial"):
            rejected_count += 1
        elif o.rejection_code:
            rejected_count += 1

        # 风控阻断
        if o.rejection_code:
            code_lower = o.rejection_code.lower()
            if any(kw in code_lower for kw in _RISK_BLOCK_KEYWORDS):
                risk_blocked_count += 1

    total_cost = round(total_fee + slippage_cost, 2)

    # 影响百分比 = 总成本 / 已成交金额
    impact_pct = round(total_cost / filled_amount_total * 100, 4) if filled_amount_total > 0 else 0.0

    return {
        "total_cost": total_cost,
        "fee_cost": round(total_fee, 2),
        "slippage_cost": round(slippage_cost, 2),
        "rejected_count": rejected_count,
        "risk_blocked_count": risk_blocked_count,
        "filled_amount": round(filled_amount_total, 2),
        "impact_pct": impact_pct,
        "sample_warning": _sample_warning(len(orders)),
    }


# ============================================================================
# 统一归因入口
# ============================================================================


def get_attribution_report(
    db: Session,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    dimensions: list[str] | None = None,
    backtest_run_id: int | None = None,
) -> dict[str, Any]:
    """获取完整归因报告。

    Args:
        portfolio_id: 组合 ID
        start_date: 起始日期（含）
        end_date: 结束日期（含）
        dimensions: 需要计算的维度列表，None 表示全部维度。
            可选值：by_member, by_execution_mode, by_source, by_rule_signal,
                    backtest_vs_sim, cost_impact
        backtest_run_id: 回测运行 ID（仅 backtest_vs_sim 维度需要）

    Returns:
        {
            "portfolio_id": int,
            "start_date": str,
            "end_date": str,
            "by_member": {...},
            "by_execution_mode": {...},
            "by_source": {...},
            "by_rule_signal": {...},
            "backtest_vs_sim": {...} | None,
            "cost_impact": {...},
            "summary": "文本总结",
        }
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError("Portfolio not found")

    # 默认全部维度
    if dimensions is None:
        dimensions = [
            "by_member",
            "by_execution_mode",
            "by_source",
            "by_rule_signal",
            "backtest_vs_sim",
            "cost_impact",
        ]

    report: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "by_member": None,
        "by_execution_mode": None,
        "by_source": None,
        "by_rule_signal": None,
        "backtest_vs_sim": None,
        "cost_impact": None,
        "summary": "",
    }

    warnings: list[str] = []

    if "by_member" in dimensions:
        report["by_member"] = attribute_by_member(db, portfolio_id, start_date, end_date)
        if report["by_member"].get("sample_warning"):
            warnings.append("成员归因样本不足")

    if "by_execution_mode" in dimensions:
        report["by_execution_mode"] = attribute_by_execution_mode(db, portfolio_id, start_date, end_date)

    if "by_source" in dimensions:
        report["by_source"] = attribute_by_source(db, portfolio_id, start_date, end_date)

    if "by_rule_signal" in dimensions:
        report["by_rule_signal"] = attribute_by_rule_signal(db, portfolio_id, start_date, end_date)

    if "backtest_vs_sim" in dimensions:
        if backtest_run_id is not None:
            try:
                report["backtest_vs_sim"] = compute_backtest_vs_sim_diff(
                    db, portfolio_id, backtest_run_id, start_date, end_date
                )
            except ValueError as e:
                report["backtest_vs_sim"] = {"error": str(e)}
                warnings.append(f"回测对比失败：{e}")
        else:
            report["backtest_vs_sim"] = None

    if "cost_impact" in dimensions:
        report["cost_impact"] = attribute_cost_impact(db, portfolio_id, start_date, end_date)

    # 生成总结
    report["summary"] = _build_summary(report, warnings)

    return report


def _build_summary(report: dict[str, Any], warnings: list[str]) -> str:
    """生成归因报告文本总结。

    不展示具有误导性的稳定结论：
    - 样本不足时不给出"稳定盈利/亏损"类结论
    - 仅描述事实性数据
    """
    parts: list[str] = []

    by_member = report.get("by_member")
    if by_member and by_member.get("items"):
        total_pnl = by_member["total_pnl"]
        member_count = by_member["member_count"]
        parts.append(f"成员归因覆盖 {member_count} 个成员、{by_member['total_trades']} 笔交易，总盈亏 {total_pnl}")
        if by_member.get("sample_warning"):
            parts.append(by_member["sample_warning"])

    by_mode = report.get("by_execution_mode")
    if by_mode and by_mode.get("items"):
        modes = [f"{it['execution_label']}({it['pnl']:+.2f})" for it in by_mode["items"]]
        parts.append(f"执行模式贡献：{', '.join(modes)}")

    by_source = report.get("by_source")
    if by_source and by_source.get("items"):
        sources = [f"{it['source_label']}({it['pnl']:+.2f})" for it in by_source["items"]]
        parts.append(f"来源贡献：{', '.join(sources)}")

    cost = report.get("cost_impact")
    if cost:
        parts.append(
            f"成本影响：总成本 {cost['total_cost']:.2f}（手续费 {cost['fee_cost']:.2f} + 滑点 {cost['slippage_cost']:.2f}），"
            f"未成交 {cost['rejected_count']} 笔，风控阻断 {cost['risk_blocked_count']} 笔"
        )

    bvs = report.get("backtest_vs_sim")
    if bvs and isinstance(bvs, dict) and bvs.get("explanation"):
        parts.append(f"回测对比：{bvs['explanation']}")

    if warnings:
        parts.append("；".join(warnings))

    return "；".join(parts) if parts else "暂无归因数据"


__all__ = [
    "attribute_by_member",
    "attribute_by_execution_mode",
    "attribute_by_source",
    "attribute_by_rule_signal",
    "compute_backtest_vs_sim_diff",
    "attribute_cost_impact",
    "get_attribution_report",
]
