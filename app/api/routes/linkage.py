"""WP8.2 跨模块联动 API 路由。

实现订单/成交上下文、告警上下文、今日决策待办、回测成员快照等端点，
让用户能解释一笔交易"为何进入、谁触发、用哪套规则、成本多少、结果如何"。

新增端点：
- GET /orders/{order_id}/context               订单完整上下文
- GET /trades/{trade_id}/context               成交完整上下文
- GET /alerts/{alert_id}/context               告警关联上下文
- GET /dashboard/today-decision                今日待办聚合
- GET /portfolios/{id}/backtests/{run_id}/members   回测成员快照
- GET /portfolios/{id}/attribution/suggest-review   归因异常建议复盘

所有端点遵循现有 routes 风格：
- 依赖注入 get_db
- HTTPException 404/400
- 不修改已稳定的业务逻辑
- 异常 best-effort：关联数据缺失时返回 null，不阻塞主响应
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.alert import AlertEvent, AlertRule
from app.models.async_task import AsyncTaskRecord
from app.models.backtest import BacktestRun
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_member import PortfolioMember, STATUS_ARCHIVED
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.models.watchlist import WatchlistItem

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================================================
# 辅助函数
# ============================================================================


def _serialize_model(obj: Any) -> dict[str, Any] | None:
    """将 SQLAlchemy 模型实例序列化为 dict，None 返回 None。"""
    if obj is None:
        return None
    result: dict[str, Any] = {}
    for column in obj.__table__.columns:
        value = getattr(obj, column.name)
        if isinstance(value, (datetime, date)):
            result[column.name] = value.isoformat()
        else:
            result[column.name] = value
    return result


def _safe_load_json(text: str | None) -> Any:
    """安全解析 JSON 字符串，失败时返回 None。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_cost_breakdown(order: SimOrder) -> dict[str, float]:
    """构建订单成本拆解。

    - commission：手续费（直接读 order.fee）
    - stamp_duty：印花税（仅卖出，从 decision_snapshot_json 中尝试提取）
    - slippage：滑点成本 = |filled_price - submitted_price| * filled_quantity
    """
    commission = float(order.fee or 0)
    submitted = float(order.submitted_price or 0)
    filled = float(order.filled_price or 0)
    filled_qty = float(order.filled_quantity or 0)
    slippage = abs(filled - submitted) * filled_qty if submitted > 0 and filled_qty > 0 else 0.0

    # 尝试从 decision_snapshot_json 提取印花税（仅卖出订单有）
    stamp_duty = 0.0
    snapshot = _safe_load_json(order.decision_snapshot_json)
    if isinstance(snapshot, dict):
        cost_info = snapshot.get("cost_breakdown") or snapshot.get("cost")
        if isinstance(cost_info, dict):
            stamp_duty = float(cost_info.get("stamp_duty") or cost_info.get("stamp") or 0)

    return {
        "commission": round(commission, 4),
        "stamp_duty": round(stamp_duty, 4),
        "slippage": round(slippage, 4),
        "total": round(commission + stamp_duty + slippage, 4),
    }


def _build_trade_result(order: SimOrder, db: Session) -> dict[str, Any] | None:
    """构建订单的成交结果摘要。

    包含成交均价、成交数量、成交金额、已实现盈亏（卖出时）。
    """
    if order.status not in ("filled", "partial"):
        return {
            "status": order.status,
            "filled_quantity": float(order.filled_quantity or 0),
            "filled_price": float(order.filled_price or 0),
            "filled_amount": float(order.filled_amount or 0),
            "realized_pnl": None,
            "rejection_code": order.rejection_code,
            "rejection_detail": order.rejection_detail,
        }

    # 查询该订单关联的所有 trade
    trades = db.execute(
        select(SimTrade)
        .where(SimTrade.order_id == order.id)
        .order_by(SimTrade.id.asc())
    ).scalars().all()

    realized_pnl = None
    if trades:
        realized_pnl = sum(float(t.realized_pnl or 0) for t in trades)

    return {
        "status": order.status,
        "filled_quantity": float(order.filled_quantity or 0),
        "filled_price": float(order.filled_price or 0),
        "filled_amount": float(order.filled_amount or 0),
        "realized_pnl": round(realized_pnl, 2) if realized_pnl is not None else None,
        "trade_count": len(trades),
        "rejection_code": order.rejection_code,
        "rejection_detail": order.rejection_detail,
    }


# ============================================================================
# 1. 订单上下文 API
# ============================================================================


@router.get(
    "/orders/{order_id}/context",
    tags=["linkage"],
    summary="WP8.2：订单完整上下文",
)
def get_order_context(
    order_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回订单完整上下文：member / signal / rule_version / cost / trade_result。

    让用户能解释一笔订单：
    - 为何进入（source_type + signal）
    - 谁触发（member）
    - 用哪套规则（rule_version）
    - 成本多少（cost_breakdown）
    - 结果如何（trade_result）
    """
    order = db.get(SimOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    # 关联 member
    member: dict[str, Any] | None = None
    if order.member_id is not None:
        m = db.get(PortfolioMember, order.member_id)
        member = _serialize_model(m)

    # 关联 signal（signal_id 当前仅作 ID 引用，无独立 Signal 表，回退到 signal_snapshot_json）
    signal: dict[str, Any] | None = None
    if order.signal_id is not None:
        signal = {"id": order.signal_id, "snapshot": _safe_load_json(order.signal_snapshot_json)}

    # rule_version（当前仅记录 ID，无独立 RuleVersion 表）
    rule_version: dict[str, Any] | None = None
    if order.rule_version_id is not None:
        rule_version = {"id": order.rule_version_id}

    # 标的
    symbol = db.get(Symbol, order.symbol_id)

    return {
        "order": _serialize_model(order),
        "symbol": _serialize_model(symbol),
        "member": member,
        "signal": signal,
        "rule_version": rule_version,
        "execution_mode": order.execution_mode,
        "source_type": order.source_type,
        "source_id": order.source_id,
        "decision_snapshot": _safe_load_json(order.decision_snapshot_json),
        "cost_breakdown": _build_cost_breakdown(order),
        "trade_result": _build_trade_result(order, db),
    }


# ============================================================================
# 2. 成交上下文 API
# ============================================================================


@router.get(
    "/trades/{trade_id}/context",
    tags=["linkage"],
    summary="WP8.2：成交完整上下文",
)
def get_trade_context(
    trade_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回成交完整上下文：通过 order_id 反查 member / signal / rule_version。"""
    trade = db.get(SimTrade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    order = db.get(SimOrder, trade.order_id)
    order_context: dict[str, Any] | None = None
    member: dict[str, Any] | None = None
    signal: dict[str, Any] | None = None
    rule_version: dict[str, Any] | None = None
    execution_mode: str | None = None
    source_type: str | None = None
    cost_breakdown: dict[str, float] | None = None
    trade_result: dict[str, Any] | None = None

    if order is not None:
        order_context = _serialize_model(order)
        execution_mode = order.execution_mode
        source_type = order.source_type
        if order.member_id is not None:
            m = db.get(PortfolioMember, order.member_id)
            member = _serialize_model(m)
        if order.signal_id is not None:
            signal = {"id": order.signal_id, "snapshot": _safe_load_json(order.signal_snapshot_json)}
        if order.rule_version_id is not None:
            rule_version = {"id": order.rule_version_id}
        cost_breakdown = _build_cost_breakdown(order)
        trade_result = _build_trade_result(order, db)

    symbol = db.get(Symbol, trade.symbol_id)

    return {
        "trade": _serialize_model(trade),
        "order": order_context,
        "symbol": _serialize_model(symbol),
        "member": member,
        "signal": signal,
        "rule_version": rule_version,
        "execution_mode": execution_mode,
        "source_type": source_type,
        "cost_breakdown": cost_breakdown,
        "trade_result": trade_result,
    }


# ============================================================================
# 3. 告警上下文 API
# ============================================================================


@router.get(
    "/alerts/{alert_id}/context",
    tags=["linkage"],
    summary="WP8.2：告警关联上下文",
)
def get_alert_context(
    alert_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回告警关联的观察项、成员或持仓上下文。

    告警 rule 的 symbol_id 关联到 watchlist_items / portfolio_members / positions。
    """
    alert = db.get(AlertEvent, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert event not found")

    alert_rule: dict[str, Any] | None = None
    if alert.rule_id:
        rule = db.get(AlertRule, alert.rule_id)
        alert_rule = _serialize_model(rule)

    symbol: dict[str, Any] | None = None
    related_watchlist_item: dict[str, Any] | None = None
    related_member: dict[str, Any] | None = None
    related_position: dict[str, Any] | None = None

    if alert.symbol_id is not None:
        symbol = _serialize_model(db.get(Symbol, alert.symbol_id))

        # 关联观察项（任意组合中该 symbol 的最新观察项）
        wl_item = db.execute(
            select(WatchlistItem)
            .where(WatchlistItem.symbol_id == alert.symbol_id)
            .order_by(desc(WatchlistItem.added_at))
            .limit(1)
        ).scalars().first()
        related_watchlist_item = _serialize_model(wl_item)

        # 关联成员（任意组合中该 symbol 的当前有效成员）
        member = db.execute(
            select(PortfolioMember)
            .where(
                PortfolioMember.symbol_id == alert.symbol_id,
                PortfolioMember.effective_to.is_(None),
            )
            .order_by(desc(PortfolioMember.id))
            .limit(1)
        ).scalars().first()
        related_member = _serialize_model(member)

        # 关联持仓（任意组合中该 symbol 的持仓）
        position = db.execute(
            select(Position)
            .where(Position.symbol_id == alert.symbol_id)
            .order_by(desc(Position.id))
            .limit(1)
        ).scalars().first()
        related_position = _serialize_model(position)

    return {
        "alert": _serialize_model(alert),
        "alert_rule": alert_rule,
        "related_watchlist_item": related_watchlist_item,
        "related_member": related_member,
        "related_position": related_position,
        "symbol": symbol,
        "alert_data": _safe_load_json(alert.data_json),
    }


# ============================================================================
# 4. 今日决策待办 API
# ============================================================================


@router.get(
    "/dashboard/today-decision",
    tags=["linkage"],
    summary="WP8.2：今日决策待办聚合",
)
def get_today_decision(
    portfolio_id: int = Query(..., description="组合 ID"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """今日待办聚合：待确认订单 + 数据门禁阻断 + 成员失效 + 告警摘要。

    - pending_orders：status='pending' 的订单
    - data_gate_blocks：失败的 AsyncTaskRecord（market_data_sync 等）
    - member_issues：规则过期 / 成员归档 / 数据过期
    - alert_summary：未确认告警数 + critical 数
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # 1. 待确认订单（status='pending'，回退到 status not in ('filled', 'cancelled', 'rejected')）
    pending_orders: list[dict[str, Any]] = []
    pending_rows = db.execute(
        select(SimOrder, Symbol)
        .join(Symbol, Symbol.id == SimOrder.symbol_id)
        .outerjoin(PortfolioMember, PortfolioMember.id == SimOrder.member_id)
        .where(
            SimOrder.portfolio_id == portfolio_id,
            SimOrder.status.notin_(("filled", "cancelled", "rejected")),
        )
        .order_by(desc(SimOrder.created_at))
        .limit(50)
    ).all()
    for order, symbol in pending_rows:
        member_label = None
        if order.member_id is not None:
            m = db.get(PortfolioMember, order.member_id)
            if m is not None:
                member_label = {
                    "id": m.id,
                    "symbol_id": m.symbol_id,
                    "execution_mode": m.execution_mode,
                    "status": m.status,
                }
        pending_orders.append({
            "order_id": order.id,
            "symbol_id": order.symbol_id,
            "symbol": symbol.symbol,
            "name": symbol.name,
            "side": order.side,
            "qty": float(order.quantity or 0),
            "submitted_price": float(order.submitted_price or 0),
            "status": order.status,
            "member": member_label,
            "execution_mode": order.execution_mode,
            "source_type": order.source_type,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        })

    # 2. 数据门禁阻断：最近 24 小时内失败的异步任务
    cutoff = _utcnow_naive() - timedelta(hours=24)
    data_gate_blocks: list[dict[str, Any]] = []
    failed_tasks = db.execute(
        select(AsyncTaskRecord)
        .where(
            AsyncTaskRecord.status == "failed",
            AsyncTaskRecord.created_at >= cutoff,
        )
        .order_by(desc(AsyncTaskRecord.created_at))
        .limit(20)
    ).scalars().all()
    for task in failed_tasks:
        data_gate_blocks.append({
            "task_id": task.id,
            "task_type": task.task_type,
            "reason": task.message or "task failed",
            "blocked_at": task.finished_at.isoformat() if task.finished_at else (
                task.updated_at.isoformat() if task.updated_at else None
            ),
            "errors": _safe_load_json(task.errors_json),
        })

    # 3. 成员失效待办
    member_issues: list[dict[str, Any]] = []
    members = db.execute(
        select(PortfolioMember, Symbol)
        .join(Symbol, Symbol.id == PortfolioMember.symbol_id)
        .where(PortfolioMember.portfolio_id == portfolio_id)
        .order_by(desc(PortfolioMember.updated_at))
    ).all()
    now = _utcnow_naive()
    stale_threshold = now - timedelta(days=7)
    for member, symbol in members:
        issue: str | None = None
        if member.status == STATUS_ARCHIVED:
            issue = "member_archived"
        elif member.entry_rule_version_id is None and member.exit_rule_version_id is None:
            issue = "rule_expired"
        elif member.updated_at is not None and member.updated_at < stale_threshold:
            issue = "data_stale"

        if issue is not None:
            member_issues.append({
                "member_id": member.id,
                "symbol_id": member.symbol_id,
                "symbol": symbol.symbol,
                "name": symbol.name,
                "issue": issue,
                "status": member.status,
                "execution_mode": member.execution_mode,
                "updated_at": member.updated_at.isoformat() if member.updated_at else None,
            })

    # 4. 告警摘要
    active_alerts = db.execute(
        select(AlertEvent).where(AlertEvent.acknowledged == 0)
    ).scalars().all()
    active_count = len(active_alerts)
    critical_count = sum(1 for a in active_alerts if a.severity == "error")

    return {
        "portfolio_id": portfolio_id,
        "pending_orders": pending_orders,
        "data_gate_blocks": data_gate_blocks,
        "member_issues": member_issues,
        "alert_summary": {
            "active_count": active_count,
            "critical_count": critical_count,
        },
        "summary": {
            "pending_order_count": len(pending_orders),
            "data_gate_block_count": len(data_gate_blocks),
            "member_issue_count": len(member_issues),
        },
    }


# ============================================================================
# 5. 回测成员快照 API
# ============================================================================


@router.get(
    "/portfolios/{portfolio_id}/backtests/{run_id}/members",
    tags=["linkage"],
    summary="WP8.2：回测使用的成员快照",
)
def get_backtest_member_snapshot(
    portfolio_id: int,
    run_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回该回测使用的成员快照。

    解析 BacktestRun.member_snapshot_json 返回成员列表。
    若 member_snapshot_json 为空（历史回测），返回 has_snapshot=false 与提示。
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    if run.portfolio_id != portfolio_id:
        raise HTTPException(
            status_code=400,
            detail="Backtest run does not belong to this portfolio",
        )

    snapshot_raw = _safe_load_json(run.member_snapshot_json)
    if not snapshot_raw:
        return {
            "portfolio_id": portfolio_id,
            "run_id": run_id,
            "has_snapshot": False,
            "members": [],
            "member_count": 0,
            "message": "该回测无成员快照（历史回测未记录成员快照）",
            "run_meta": {
                "run_name": run.run_name,
                "start_date": run.start_date.isoformat() if run.start_date else None,
                "end_date": run.end_date.isoformat() if run.end_date else None,
                "source_type": run.source_type,
            },
        }

    # member_snapshot_json 结构：[{member_id, symbol_id, effective_from, effective_to,
    #                              execution_mode, entry_rule_version_id, exit_rule_version_id}]
    members: list[dict[str, Any]] = []
    raw_list = snapshot_raw if isinstance(snapshot_raw, list) else [snapshot_raw]
    symbol_ids = {item.get("symbol_id") for item in raw_list if isinstance(item, dict) and item.get("symbol_id")}
    symbol_map: dict[int, Symbol] = {}
    if symbol_ids:
        rows = db.execute(select(Symbol).where(Symbol.id.in_(symbol_ids))).scalars().all()
        symbol_map = {s.id: s for s in rows}

    for item in raw_list:
        if not isinstance(item, dict):
            continue
        symbol_id = item.get("symbol_id")
        sym = symbol_map.get(symbol_id) if symbol_id else None
        members.append({
            "member_id": item.get("member_id"),
            "symbol_id": symbol_id,
            "symbol": sym.symbol if sym else None,
            "name": sym.name if sym else None,
            "effective_from": item.get("effective_from"),
            "effective_to": item.get("effective_to"),
            "execution_mode": item.get("execution_mode"),
            "entry_rule_version_id": item.get("entry_rule_version_id"),
            "exit_rule_version_id": item.get("exit_rule_version_id"),
        })

    # 排除的成员
    excluded_raw = _safe_load_json(run.excluded_members_json) or []

    return {
        "portfolio_id": portfolio_id,
        "run_id": run_id,
        "has_snapshot": True,
        "members": members,
        "excluded_members": excluded_raw if isinstance(excluded_raw, list) else [],
        "member_count": len(members),
        "run_meta": {
            "run_name": run.run_name,
            "start_date": run.start_date.isoformat() if run.start_date else None,
            "end_date": run.end_date.isoformat() if run.end_date else None,
            "source_type": run.source_type,
            "score_mode": run.score_mode,
            "data_cutoff_at": run.data_cutoff_at.isoformat() if run.data_cutoff_at else None,
        },
    }


# ============================================================================
# 6. 归因异常建议复盘
# ============================================================================


@router.get(
    "/portfolios/{portfolio_id}/attribution/suggest-review",
    tags=["linkage"],
    summary="WP8.2：归因异常建议复盘",
)
def get_attribution_suggest_review(
    portfolio_id: int,
    start_date: date | None = Query(None, description="起始日期"),
    end_date: date | None = Query(None, description="结束日期"),
    backtest_run_id: int | None = Query(None, description="回测运行 ID"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """检测归因异常并返回 suggested_review 标记。

    异常判定规则：
    - 某成员贡献为负（pnl < 0）
    - 回测偏差大（|return_diff_pct| >= 0.05 或 |sharpe_diff| >= 0.5）
    - 风控阻断数 > 0
    - 样本不足
    """
    from app.services.attribution import get_attribution_report

    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # 默认查最近 30 天
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=30)

    try:
        report = get_attribution_report(
            db,
            portfolio_id,
            start_date=start_date,
            end_date=end_date,
            backtest_run_id=backtest_run_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    anomalies: list[dict[str, Any]] = []

    # 1. 成员贡献为负
    by_member = report.get("by_member") or {}
    for item in by_member.get("items", []):
        if float(item.get("pnl") or 0) < 0:
            anomalies.append({
                "type": "negative_member",
                "member_id": item.get("member_id"),
                "symbol_id": item.get("symbol_id"),
                "pnl": item.get("pnl"),
                "contribution_pct": item.get("contribution_pct"),
                "message": f"成员 {item.get('member_id')} 贡献为负 ({item.get('pnl')})",
            })

    # 2. 回测偏差大
    bvs = report.get("backtest_vs_sim")
    if bvs and isinstance(bvs, dict) and bvs.get("diff"):
        diff = bvs["diff"]
        if abs(float(diff.get("return_diff_pct") or 0)) >= 0.05:
            anomalies.append({
                "type": "backtest_return_diff",
                "value": diff.get("return_diff_pct"),
                "message": f"回测与模拟收益率偏差 {diff.get('return_diff_pct'):.2%}",
            })
        if abs(float(diff.get("sharpe_diff") or 0)) >= 0.5:
            anomalies.append({
                "type": "backtest_sharpe_diff",
                "value": diff.get("sharpe_diff"),
                "message": f"夏普比率偏差 {diff.get('sharpe_diff'):.2f}",
            })

    # 3. 风控阻断
    cost_impact = report.get("cost_impact") or {}
    if int(cost_impact.get("risk_blocked_count") or 0) > 0:
        anomalies.append({
            "type": "risk_blocked",
            "value": cost_impact.get("risk_blocked_count"),
            "message": f"风控阻断 {cost_impact.get('risk_blocked_count')} 笔",
        })

    # 4. 样本不足
    for dim_name in ("by_member", "by_execution_mode", "by_source", "by_rule_signal"):
        dim = report.get(dim_name) or {}
        if dim.get("sample_warning"):
            anomalies.append({
                "type": "sample_insufficient",
                "dimension": dim_name,
                "message": dim.get("sample_warning"),
            })

    return {
        "portfolio_id": portfolio_id,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "suggested_review": len(anomalies) > 0,
        "anomalies": anomalies,
        "anomaly_count": len(anomalies),
        "summary": report.get("summary", ""),
    }


__all__ = ["router"]
