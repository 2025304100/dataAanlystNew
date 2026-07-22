"""组合自动交易服务（P2-3）。

基于已有的"评分 + 候选池 + 持仓 + 风控"能力，自动触发模拟买卖。

设计要点：
- 不重新实现信号评估，复用 Score.action 字段（open/buy_dip/hold/reduce/exit）
- 复用 compute_position_budget 做风控闸门（can_open / blocked_reasons）
- 复用 place_sim_order 实际下单（含 T+1 / 涨跌停 / 手续费 / 滑点）
- 卖出侧：遍历当前持仓，action=exit 全卖，action=reduce 卖一半
- 买入侧：遍历最新 scan 的 executable 候选，action ∈ {open, buy_dip} 且 can_open=True 时买入
- dry_run 模式只返回计划不实际下单，便于审计与首次验证
- 单笔失败隔离：try/except 包住每笔，错误记录到 errors
- 调度入口：create_portfolio_auto_trade_task 异步遍历所有 auto_trade_enabled=1 的模拟组合

边界处理：
- 组合未开启 auto_trade_enabled：拒绝执行（手动触发也要求先开启开关）
- 调度模式下被跳过（不视为错误，因为开关关闭即代表用户主动暂停）
- 无 ScanRun / 无 candidates：买入侧为空，仍处理卖出
- 无 Score：跳过该标的
- compute_position_budget 返回 can_open=False：记录 blocked_reasons，跳过
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.portfolio import Portfolio, Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.sim_account import SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.allocation import compute_position_budget
from app.services.async_tasks import (
    _append_error,
    _now,
    _set_task,
    _start_worker,
    create_async_task,
    list_async_tasks,
)
from app.services.market_rules import lot_size_for_symbol, normalize_order_quantity
from app.services.sim_accounts import (
    _round_money,
    latest_price_for_symbol,
    place_sim_order,
)

logger = logging.getLogger(__name__)
TASK_TYPE = "portfolio_auto_trade"

# 触发买入的 action 集合
_BUY_ACTIONS = {"open", "buy_dip"}
# 触发卖出的 action 集合
_SELL_ACTIONS = {"exit", "reduce"}


def _latest_score_for_symbol(db: Session, symbol_id: int) -> Score | None:
    """查询某标的的最新一条 Score。"""
    return db.execute(
        select(Score)
        .where(Score.symbol_id == symbol_id)
        .order_by(desc(Score.trade_date), desc(Score.id))
        .limit(1)
    ).scalars().first()


def _latest_scan_results(db: Session, portfolio_id: int, *, limit: int = 20) -> list[ScanResult]:
    """查询组合最新一次成功 scan 的 executable 候选。"""
    latest_run = db.execute(
        select(ScanRun)
        .where(ScanRun.portfolio_id == portfolio_id, ScanRun.status == "done")
        .order_by(desc(ScanRun.id))
        .limit(1)
    ).scalars().first()
    if latest_run is None:
        return []
    return db.execute(
        select(ScanResult)
        .where(
            ScanResult.scan_run_id == latest_run.id,
            ScanResult.result_type == "executable",
        )
        .order_by(desc(ScanResult.priority_score), ScanResult.rank_no)
        .limit(limit)
    ).scalars().all()


def _compute_buy_quantity(
    portfolio: Portfolio,
    symbol: Symbol,
    fill_price: float,
    recommended_amount: float,
) -> int:
    """根据建议金额和最新价计算买入手数（已归一化为手数倍）。"""
    if fill_price <= 0 or recommended_amount <= 0:
        return 0
    lot_size = lot_size_for_symbol(symbol)
    raw_qty = int(recommended_amount // fill_price)
    return normalize_order_quantity(symbol, raw_qty)


def run_auto_trade(
    db: Session,
    portfolio_id: int,
    *,
    dry_run: bool = True,
    buy_candidate_limit: int = 10,
) -> dict[str, Any]:
    """对组合执行一次自动交易评估与下单。

    Args:
        db: 数据库会话
        portfolio_id: 目标组合 ID（必须 account_type="simulated" 且 auto_trade_enabled=1）
        dry_run: True 只返回计划不实际下单；False 实际下单
        buy_candidate_limit: 买入侧最多评估的候选数量（防止超大组合一次下太多单）

    Returns:
        {
            "portfolio_id": int,
            "dry_run": bool,
            "sells": list[dict],  # 卖出计划/执行结果
            "buys": list[dict],   # 买入计划/执行结果
            "errors": list[str],  # 单笔失败原因
            "executed_at": str,   # ISO 时间戳
        }

    Raises:
        ValueError: 组合不存在/非模拟/未开启自动交易
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")
    if portfolio.account_type != "simulated":
        raise ValueError(f"Portfolio {portfolio_id} is not simulated")
    if not portfolio.auto_trade_enabled:
        raise ValueError(
            f"Portfolio {portfolio_id} auto_trade_enabled is 0. "
            "Enable auto-trade first."
        )

    sells: list[dict[str, Any]] = []
    buys: list[dict[str, Any]] = []
    errors: list[str] = []

    # ========================================================================
    # 1. 卖出侧：遍历当前持仓
    # ========================================================================
    positions = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id)
    ).scalars().all()

    for position in positions:
        symbol = db.get(Symbol, position.symbol_id)
        if symbol is None:
            errors.append(f"Position {position.id}: symbol {position.symbol_id} not found")
            continue

        try:
            score = _latest_score_for_symbol(db, position.symbol_id)
            if score is None:
                # 无评分无法决策，跳过
                continue

            action = (score.action or "").lower()
            if action not in _SELL_ACTIONS:
                continue

            # 计算卖出数量
            held_qty = float(position.quantity or 0)
            if held_qty <= 0:
                continue

            if action == "exit":
                sell_qty = held_qty  # 全部卖出
                sell_reason = f"action=exit (full exit)"
            else:  # reduce
                # 卖出一半（归一化到手数倍）
                raw_half = held_qty / 2
                sell_qty = normalize_order_quantity(symbol, int(raw_half))
                if sell_qty <= 0:
                    sell_qty = normalize_order_quantity(symbol, 1)
                sell_reason = f"action=reduce (sell half)"

            if sell_qty <= 0:
                continue

            # 使用最新价作为参考（dry_run 时用最新收盘价，实际下单时 place_sim_order 会重新计算）
            ref_price = latest_price_for_symbol(db, symbol.id) or float(position.avg_cost or 0)
            if ref_price <= 0:
                errors.append(f"Sell {symbol.symbol}: no usable price")
                continue

            sell_plan = {
                "symbol_id": symbol.id,
                "symbol": symbol.symbol,
                "name": symbol.name,
                "action": action,
                "held_quantity": held_qty,
                "sell_quantity": sell_qty,
                "ref_price": _round_money(ref_price),
                "reason": sell_reason,
                "stage": score.stage,
                "executed": False,
                "order_id": None,
            }

            if dry_run:
                sells.append(sell_plan)
                continue

            # 实际下单：卖出不需要 enforce_t_plus_1（因为是要平仓），但仍校验涨跌停
            order, trade = place_sim_order(
                db=db,
                portfolio=portfolio,
                symbol=symbol,
                side="sell",
                quantity=sell_qty,
                price=ref_price,
                order_type="market",
                note=f"Auto-trade sell: {sell_reason}",
                enforce_rules=True,
                apply_fees=True,
            )
            sell_plan["executed"] = True
            sell_plan["order_id"] = order.id
            sell_plan["filled_price"] = _round_money(float(order.filled_price))
            sell_plan["fee"] = _round_money(float(order.fee))
            sells.append(sell_plan)
        except Exception as exc:
            errors.append(f"Sell {symbol.symbol}: {exc}")
            logger.warning("auto_trade sell failed for %s: %s", symbol.symbol, exc, exc_info=True)

    # ========================================================================
    # 2. 买入侧：遍历最新 scan 的 executable 候选
    # ========================================================================
    scan_results = _latest_scan_results(db, portfolio_id, limit=buy_candidate_limit)

    for scan_result in scan_results:
        symbol = db.get(Symbol, scan_result.symbol_id)
        if symbol is None:
            errors.append(f"ScanResult {scan_result.id}: symbol {scan_result.symbol_id} not found")
            continue

        try:
            # 跳过已持仓的标的（避免加仓逻辑复杂化，加仓由 reduce/exit 信号处理持仓侧）
            existing_position = db.execute(
                select(Position).where(
                    Position.portfolio_id == portfolio_id,
                    Position.symbol_id == symbol.id,
                )
            ).scalars().first()
            if existing_position is not None:
                continue

            score = _latest_score_for_symbol(db, symbol.id)
            if score is None:
                continue

            action = (score.action or "").lower()
            if action not in _BUY_ACTIONS:
                continue

            # 风控闸门：compute_position_budget
            entry_price = latest_price_for_symbol(db, symbol.id)
            if entry_price is None or entry_price <= 0:
                errors.append(f"Buy {symbol.symbol}: no latest price")
                continue

            budget = compute_position_budget(
                db=db,
                portfolio_id=portfolio_id,
                symbol=symbol,
                stage=score.stage or "start",
                action=action,
                entry_price=entry_price,
            )

            if not budget.get("can_open"):
                # 风控阻断，记录但不下单
                buys.append({
                    "symbol_id": symbol.id,
                    "symbol": symbol.symbol,
                    "name": symbol.name,
                    "action": action,
                    "stage": score.stage,
                    "ref_price": _round_money(entry_price),
                    "can_open": False,
                    "decision": budget.get("decision"),
                    "blocked_reasons": budget.get("blocked_reasons", []),
                    "executed": False,
                })
                # WP-MSG.7：自动交易阻断事件接入通知系统（不阻断主流程）
                try:
                    from app.services.notifications.event_emitter import emit_auto_trade_blocked
                    blocked_reasons = budget.get("blocked_reasons", [])
                    reason_text = "; ".join(str(r) for r in blocked_reasons) if blocked_reasons else str(budget.get("decision") or "blocked")
                    emit_auto_trade_blocked(
                        db,
                        portfolio_id=portfolio_id,
                        symbol_id=symbol.id,
                        symbol=symbol.symbol,
                        reason=reason_text,
                        rule_name=budget.get("decision"),
                    )
                except Exception:
                    logger.warning(
                        "emit_auto_trade_blocked failed for portfolio=%s symbol=%s (non-blocking)",
                        portfolio_id, symbol.symbol, exc_info=True,
                    )
                continue

            recommended_amount = float(budget.get("recommended_amount", 0))
            buy_qty = _compute_buy_quantity(portfolio, symbol, entry_price, recommended_amount)
            if buy_qty <= 0:
                errors.append(
                    f"Buy {symbol.symbol}: computed quantity 0 "
                    f"(recommended_amount={recommended_amount}, price={entry_price})"
                )
                continue

            buy_plan = {
                "symbol_id": symbol.id,
                "symbol": symbol.symbol,
                "name": symbol.name,
                "action": action,
                "stage": score.stage,
                "ref_price": _round_money(entry_price),
                "can_open": True,
                "decision": budget.get("decision"),
                "recommended_amount": _round_money(recommended_amount),
                "buy_quantity": buy_qty,
                "executed": False,
                "order_id": None,
            }

            if dry_run:
                buys.append(buy_plan)
                continue

            order, trade = place_sim_order(
                db=db,
                portfolio=portfolio,
                symbol=symbol,
                side="buy",
                quantity=buy_qty,
                price=entry_price,
                order_type="market",
                note=f"Auto-trade buy: action={action}, stage={score.stage}",
                enforce_rules=True,
                apply_fees=True,
            )
            buy_plan["executed"] = True
            buy_plan["order_id"] = order.id
            buy_plan["filled_price"] = _round_money(float(order.filled_price))
            buy_plan["fee"] = _round_money(float(order.fee))
            buys.append(buy_plan)
        except Exception as exc:
            errors.append(f"Buy {symbol.symbol}: {exc}")
            logger.warning("auto_trade buy failed for %s: %s", symbol.symbol, exc, exc_info=True)

    # 更新 last_run_at
    if not dry_run:
        portfolio.auto_trade_last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    return {
        "portfolio_id": portfolio_id,
        "dry_run": dry_run,
        "sells": sells,
        "buys": buys,
        "errors": errors,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# 调度入口（供 scheduled_tasks._dispatch_task 调用）
# ============================================================================


def create_portfolio_auto_trade_task(*, dry_run: bool = False, buy_candidate_limit: int = 10):
    """调度入口：去重后启动后台 worker。

    Args:
        dry_run: True 只生成计划不下单（用于巡检/审计）；False 实际下单（默认，调度场景）
        buy_candidate_limit: 每个组合买入侧最多评估的候选数量

    Returns:
        AsyncTaskRead：任务快照（供调用方立即响应前端）
    """
    existing = list_async_tasks(task_type=TASK_TYPE, limit=1)
    if existing and existing[0].status in {"queued", "running"}:
        return existing[0]
    task = create_async_task(
        TASK_TYPE,
        {"dry_run": dry_run, "buy_candidate_limit": buy_candidate_limit},
    )
    _start_worker(
        task.id,
        lambda task_id: _run_auto_trade_task(
            task_id,
            dry_run=dry_run,
            buy_candidate_limit=buy_candidate_limit,
        ),
    )
    return task


def _run_auto_trade_task(
    task_id: str,
    *,
    dry_run: bool = False,
    buy_candidate_limit: int = 10,
) -> None:
    """worker：遍历所有已开启 auto_trade_enabled 的模拟组合执行自动交易。

    进度策略（与 portfolio_equity_snapshot 保持一致）：
    - queued=0% → fetch=10%（拉取组合列表）→ loop 30%→90%（按组合数线性）→ done=100%

    单组合失败隔离：try/except 包住每个组合，错误记录到 errors_json 与 result.errors[]

    终态保护：每次写进度前检查 task.status == "cancelled"，已取消则立即返回
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        _set_task(
            db,
            task_id,
            status="running",
            stage="fetch",
            percent=10,
            message="Fetching auto-trade-enabled portfolios",
            started_at=_now(),
        )

        portfolios = db.execute(
            select(Portfolio).where(
                Portfolio.account_type == "simulated",
                Portfolio.auto_trade_enabled == 1,
            )
        ).scalars().all()
        total = len(portfolios)

        if total == 0:
            _set_task(
                db,
                task_id,
                status="done",
                stage="done",
                percent=100,
                total=0,
                processed=0,
                ok_count=0,
                failed_count=0,
                message="No auto-trade-enabled portfolios to process",
                result_json=json.dumps(
                    {
                        "dry_run": dry_run,
                        "total": 0,
                        "executed": 0,
                        "skipped": 0,
                        "portfolio_results": [],
                    },
                    ensure_ascii=False,
                ),
                finished_at=_now(),
            )
            return

        executed = 0
        skipped = 0
        portfolio_results: list[dict[str, Any]] = []
        # 进度区间 30% → 90%，按组合数线性
        for idx, portfolio in enumerate(portfolios):
            # 每次循环前重新检查取消信号
            task = db.get(AsyncTaskRecord, task_id)
            if task is None or task.status == "cancelled":
                return
            try:
                result = run_auto_trade(
                    db=db,
                    portfolio_id=portfolio.id,
                    dry_run=dry_run,
                    buy_candidate_limit=buy_candidate_limit,
                )
                portfolio_results.append({
                    "portfolio_id": portfolio.id,
                    "portfolio_name": portfolio.name,
                    "sells_count": len(result.get("sells", [])),
                    "buys_count": len(result.get("buys", [])),
                    "errors": result.get("errors", []),
                })
                executed += 1
            except Exception as exc:
                db.rollback()
                logger.exception(
                    "Auto-trade failed for portfolio %s", portfolio.id
                )
                _append_error(task, {
                    "scope": "portfolio_auto_trade",
                    "portfolio_id": portfolio.id,
                    "error": str(exc),
                })
                portfolio_results.append({
                    "portfolio_id": portfolio.id,
                    "portfolio_name": portfolio.name,
                    "error": str(exc),
                })
                skipped += 1
                # rollback 后 portfolio 对象可能 detached，重新获取避免后续循环引用问题
                continue

            # 进度平滑：避免单组合大幅跳跃
            percent = 30 + int((idx + 1) / total * 60)
            _set_task(
                db,
                task_id,
                stage="auto_trade",
                percent=min(percent, 90),
                total=total,
                processed=idx + 1,
                ok_count=executed,
                failed_count=skipped,
                message=f"Auto-traded {idx + 1}/{total} portfolios",
            )

        # 终态检查（防止取消信号被覆盖）
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.status == "cancelled":
            return
        _set_task(
            db,
            task_id,
            status="done",
            stage="done",
            percent=100,
            total=total,
            processed=total,
            ok_count=executed,
            failed_count=skipped,
            message="Portfolio auto-trade completed",
            result_json=json.dumps(
                {
                    "dry_run": dry_run,
                    "total": total,
                    "executed": executed,
                    "skipped": skipped,
                    "portfolio_results": portfolio_results[:50],
                },
                ensure_ascii=False,
                default=str,
            ),
            finished_at=_now(),
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Portfolio auto-trade task failed: %s", task_id)
        task = db.get(AsyncTaskRecord, task_id)
        if task is not None:
            _append_error(task, {"scope": "portfolio_auto_trade", "error": str(exc)})
            task.status = "failed"
            task.stage = "failed"
            task.message = str(exc)
            task.finished_at = _now()
            task.updated_at = _now()
            db.commit()
    finally:
        db.close()


__all__ = [
    "TASK_TYPE",
    "create_portfolio_auto_trade_task",
    "run_auto_trade",
]
