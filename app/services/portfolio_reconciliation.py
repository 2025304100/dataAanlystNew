"""WP0-2 / G3-3 组合对账服务：T+1 日对 T 日决策执行结果做一致性核对。

对账链路（沿 tasks.md §WP0-2 「last_reconciled_trade_date」口径）：
  Inputs（任一缺失仍要尽力对，结果含 NOT_CHECKED）：
    (A) 决策侧证据：DecisionRun(run_type=auto_simulation, trade_date=T) +
                    DecisionEvidence(symbol, action, target_quantity)
    (B) 订单侧计划：OrderPlan / SimulationOrder（本项目用 BacktestTrade 或 外部订单系统）
    (C) 撮合结果：simulation_matching_engine.MatchResult（FILLED / 部分成交 / 拒单）
    (D) 最终持仓：Positions(T+1 收盘后快照)

  核对 4 类差异：
    R1. 有决策 BUY/SELL 但无订单计划 → MISSING_ORDER_PLAN
    R2. 有订单计划但无撮合结果（T+1 仍停留 PENDING）→ UNFILLED_PLAN
    R3. 撮合后最终持仓与预期不一致（数量/方向/成本）→ POSITION_MISMATCH
    R4. 现金 + Σ(持仓×收盘价) ≠ 期初资产（守恒破坏）→ NAV_BROKEN

  输出：
    - ReconciliationReport（差异明细 + 汇总）
    - 若无差异：Portfolio.last_reconciled_trade_date = T（只推进，不跳日，不等号安全）
    - 若有实质差异：写入 RECONCILIATION_RESULT 审计事件并进入
      RECONCILIATION_BLOCKED，留待人工修正 + 确认
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from sqlalchemy import and_, inspect, select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestTrade  # 作为 legacy fallback 订单/撮合明细
from app.models.decision_engine import DecisionRun as DecisionRunORM, DecisionEvidence
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade

from app.services.data_governance_audit import (
    AuditWriteResult,
    write_audit_event,
)


DiffKind = Literal[
    "MISSING_ORDER_PLAN",
    "UNFILLED_PLAN",
    "POSITION_MISMATCH",
    "NAV_BROKEN",
    "DECISION_RUN_NOT_FOUND",
    "NOT_CHECKED",
]


@dataclass
class ReconciliationDiff:
    symbol_id: int | None
    kind: DiffKind
    expected: Any = None
    actual: Any = None
    detail: str | None = None


@dataclass
class ReconciliationReport:
    portfolio_id: int
    trade_date: date
    decision_run_id: str | None
    status: Literal["PASSED", "BLOCKED"]
    expected_position_count: int = 0
    actual_position_count: int = 0
    expected_cash: float | None = None
    actual_cash: float | None = None
    differences: list[ReconciliationDiff] = field(default_factory=list)
    audit: AuditWriteResult | None = None

    @property
    def has_any_diff(self) -> bool:
        return any(True for d in self.differences if d.kind != "NOT_CHECKED")


@dataclass(frozen=True)
class _ExecutionFill:
    """Read-only normalized fill used by the reconciliation seam."""

    symbol_id: int
    quantity: float
    side: str


def _load_auto_simulation_execution(
    db: Session,
    *,
    portfolio_id: int,
    decision_run_id: str,
) -> tuple[set[str], list[_ExecutionFill]]:
    """Load the exact simulated execution chain for one DecisionRun.

    ``SimOrder.decision_evidence_id`` is the durable ownership boundary.  A
    historical order for the same symbol or date must never be used merely
    because it happens to look similar.  ``SimTrade`` is authoritative for
    ledger fills; ``SimOrder.filled_quantity`` is retained as a compatibility
    fallback for orders created before per-fill rows were available.
    """
    # Select only the stable attribution columns.  Older Alembic-created
    # ``sim_orders`` tables can legitimately lack later ORM-only fields such
    # as ``limit_price``; selecting the entity would make a read-only
    # reconciliation fail before it can inspect its actual evidence link.
    order_columns = {
        column["name"]
        for column in inspect(db.get_bind()).get_columns("sim_orders")
    }
    required_order_columns = {
        "id", "portfolio_id", "symbol_id", "side", "filled_quantity",
        "decision_evidence_id",
    }
    if not required_order_columns.issubset(order_columns):
        return set(), []

    orders = list(db.execute(
        select(
            SimOrder.id,
            SimOrder.symbol_id,
            SimOrder.side,
            SimOrder.filled_quantity,
            SimOrder.decision_evidence_id,
        )
        .join(DecisionEvidence, DecisionEvidence.id == SimOrder.decision_evidence_id)
        .where(
            SimOrder.portfolio_id == portfolio_id,
            DecisionEvidence.decision_run_id == str(decision_run_id),
        )
        .order_by(SimOrder.id)
    ).all())
    if not orders:
        return set(), []

    order_ids = [int(order.id) for order in orders]
    trade_columns = {
        column["name"]
        for column in inspect(db.get_bind()).get_columns("sim_trades")
    }
    required_trade_columns = {"order_id", "symbol_id", "side", "quantity"}
    fills_by_order: dict[int, list[tuple[int, str, float]]] = {}
    if required_trade_columns.issubset(trade_columns):
        for fill in db.execute(
            select(
                SimTrade.order_id,
                SimTrade.symbol_id,
                SimTrade.side,
                SimTrade.quantity,
            )
            .where(SimTrade.order_id.in_(order_ids))
            .order_by(SimTrade.order_id, SimTrade.id)
        ).all():
            fills_by_order.setdefault(int(fill.order_id), []).append(
                (int(fill.symbol_id), str(fill.side or ""), float(fill.quantity or 0.0))
            )

    order_evidence_ids: set[str] = set()
    normalized: list[_ExecutionFill] = []
    for order in orders:
        if order.decision_evidence_id:
            order_evidence_ids.add(str(order.decision_evidence_id))
        fills = fills_by_order.get(int(order.id), [])
        if fills:
            normalized.extend(
                _ExecutionFill(
                    symbol_id=symbol_id,
                    quantity=quantity,
                    side=side or str(order.side or ""),
                )
                for symbol_id, side, quantity in fills
                if quantity > 0
            )
            continue

        # Transitional/repair compatibility: a persisted filled quantity is
        # still evidence of matching even when old data has no SimTrade row.
        filled_quantity = float(order.filled_quantity or 0.0)
        if filled_quantity > 0:
            normalized.append(_ExecutionFill(
                symbol_id=int(order.symbol_id),
                quantity=filled_quantity,
                side=str(order.side or ""),
            ))
    return order_evidence_ids, normalized


# ──────────────────────────────────────────────────────────── 主函数
def reconcile_trade_date(
    db: Session,
    portfolio_id: int,
    trade_date: date,
    *,
    operator_id: str = "cron_worker",
    correlation_id: str | None = None,
    # 注入点：仿真交易撮合订单表；本项目若未建专用 order_plan，使用 BacktestTrade 作为
    # 撮合明细（回测路径兼容），真实自动仿真需接 SimulationOrder。
    trades_provider=None,  # Callable[[Session, int, date], list[BacktestTradeLike]]
    positions_provider=None,  # Callable[[Session, int, date | None], dict[int, int]]
) -> ReconciliationReport:
    """对 T 日决策 → T+1 最终持仓做对账。返回差异明细 + 推进 last_reconciled_trade_date。"""
    p = db.get(Portfolio, portfolio_id)
    if p is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    # 1. 找 DecisionRun(auto_simulation, trade_date=T)
    dr = db.execute(
        select(DecisionRunORM).where(
            DecisionRunORM.portfolio_id == portfolio_id,
            DecisionRunORM.run_type.in_(["auto_simulation"]),  # DB value
            DecisionRunORM.trade_date == trade_date,
            DecisionRunORM.status == "SUCCEEDED",
        ).order_by(DecisionRunORM.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    report = ReconciliationReport(
        portfolio_id=portfolio_id,
        trade_date=trade_date,
        decision_run_id=None if dr is None else str(dr.id),
        status="PASSED",
    )

    if dr is None:
        report.differences.append(ReconciliationDiff(
            symbol_id=None,
            kind="DECISION_RUN_NOT_FOUND",
            detail=(
                f"portfolio_id={portfolio_id} trade_date={trade_date.isoformat()} "
                "无 SUCCEEDED auto_simulation DecisionRun（未接管/当日仍未执行）"
            ),
        ))
        _finalize_report(db, p, report, operator_id, correlation_id)
        return report

    # 2. 决策侧预期（只关心 BUY/SELL/HOLD）
    evidences = db.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.decision_run_id == str(dr.id),
        )
    ).scalars().all()
    expected_qty_delta: dict[int, int] = {}  # symbol_id -> target_qty_delta
    expected_target_quantity: dict[int, int] = {}
    evidence_id_by_symbol: dict[int, str] = {}
    for ev in evidences:
        if ev.symbol_id is None:
            continue
        delta = int(getattr(ev, "target_qty_delta", 0) or 0)
        target = int(getattr(ev, "target_quantity", 0) or 0)
        action = str(getattr(ev, "action", "") or "")
        if action in {"BUY", "SELL"}:
            expected_qty_delta[ev.symbol_id] = delta
            expected_target_quantity[ev.symbol_id] = target
            evidence_id_by_symbol[ev.symbol_id] = str(ev.id)
        elif action in {"HOLD", "NO_ACTION", "DATA_BLOCKED", "REJECTED"}:
            # 仍在范围内，记录为 0 变化（便于 R3 比对最终持仓与目标）
            expected_qty_delta.setdefault(ev.symbol_id, 0)
            if target:
                expected_target_quantity.setdefault(ev.symbol_id, target)

    # 3. 订单 / 撮合侧实际
    trades: list = []
    execution_source_connected = False
    order_evidence_ids: set[str] = set()
    if trades_provider is not None:
        trades = list(trades_provider(db, portfolio_id, trade_date) or [])
        execution_source_connected = bool(trades)
    else:
        # The auto-simulation path owns a stable Evidence -> SimOrder foreign
        # key.  Prefer it to any symbol/date heuristic or historical backtest
        # row, and keep the source scoped to this exact DecisionRun.
        order_evidence_ids, trades = _load_auto_simulation_execution(
            db,
            portfolio_id=portfolio_id,
            decision_run_id=str(dr.id),
        )
        execution_source_connected = bool(order_evidence_ids)

        # Legacy test/import data predating SimOrder attribution may still be
        # uncheckable.  Preserve the prior fail-soft behaviour only for that
        # genuinely unlinked case; it must not hide a partial linked order.
        if not execution_source_connected:
            try:
                bt_rows = db.execute(
                    select(BacktestTrade).where(
                        getattr(BacktestTrade, "portfolio_id", None) == portfolio_id,
                        BacktestTrade.trade_date == trade_date,
                    )
                ).scalars().all()
                trades = list(bt_rows)
                execution_source_connected = bool(trades)
            except Exception:
                trades = []

    if trades_provider is None and not execution_source_connected:
        # 缺订单表 → NOT_CHECKED，不视为真实差异
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail=(
                "未接入统一 order_plan / SimulationOrder 数据源；"
                "仅对 决策侧预期 vs 最终持仓 做 R3 对比。"
            ),
        ))

    actual_filled_delta: dict[int, int] = {}
    for t in trades:
        sid = int(getattr(t, "symbol_id", 0))
        qty = int(getattr(t, "quantity", 0) or 0)
        side = str(getattr(t, "side", "") or "").upper()
        sign = 1 if side in {"BUY", "OPEN", "LONG"} else (-1 if side in {"SELL", "CLOSE", "SHORT"} else 0)
        if sign != 0:
            actual_filled_delta[sid] = actual_filled_delta.get(sid, 0) + sign * qty

    # R4 cash seam: when the simulation has a CashLedger, reconcile the exact
    # cash effects of the evidence-linked SimTrade rows, including fees. Older
    # imported portfolios may have no ledger yet; retain NOT_CHECKED semantics
    # for those rows instead of inventing a balance.
    table_names = set(inspect(db.get_bind()).get_table_names())
    cash_balance_row = None
    if "cash_ledger" in table_names:
        cash_balance_row = db.execute(
            select(CashLedger.balance_after)
            .where(CashLedger.portfolio_id == portfolio_id)
            .order_by(CashLedger.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    if cash_balance_row is not None and order_evidence_ids:
        linked_cash_rows = db.execute(
            select(SimTrade.side, SimTrade.amount, SimTrade.fee)
            .join(SimOrder, SimOrder.id == SimTrade.order_id)
            .where(
                SimTrade.portfolio_id == portfolio_id,
                SimOrder.decision_evidence_id.in_(order_evidence_ids),
            )
            .order_by(SimTrade.id)
        ).all()
        expected_cash = float(getattr(p, "total_capital", 0.0) or 0.0)
        for side, amount, fee in linked_cash_rows:
            gross = abs(float(amount or 0.0))
            total_fee = abs(float(fee or 0.0))
            if str(side or "").lower() == "buy":
                expected_cash -= gross + total_fee
            elif str(side or "").lower() == "sell":
                expected_cash += gross - total_fee
        report.expected_cash = round(expected_cash, 8)
        report.actual_cash = round(float(cash_balance_row), 8)
        cash_tolerance = max(0.01, abs(expected_cash) * 1e-6)
        if abs(expected_cash - float(cash_balance_row)) > cash_tolerance:
            report.differences.append(ReconciliationDiff(
                symbol_id=None,
                kind="NAV_BROKEN",
                expected={"cash": report.expected_cash},
                actual={"cash": report.actual_cash},
                detail=(
                    "CashLedger balance does not equal initial capital plus "
                    "evidence-linked fills and fees."
                ),
            ))

    # R1: 有 BUY/SELL 决策但无对应撮合变化且无订单计划（简化：缺订单记录即 MISSING）
    for sid, delta in expected_qty_delta.items():
        if delta == 0:
            continue
        filled = actual_filled_delta.get(sid, 0)
        if filled == 0 and not execution_source_connected:
            # 订单表空 → 已标记 NOT_CHECKED，不再重复记 MISSING
            pass
        elif filled == 0:
            evidence_id = evidence_id_by_symbol.get(sid)
            if order_evidence_ids and evidence_id in order_evidence_ids:
                report.differences.append(ReconciliationDiff(
                    symbol_id=sid,
                    kind="UNFILLED_PLAN",
                    expected={"target_qty_delta": delta},
                    actual={"filled_delta": 0},
                    detail=(
                        "已创建与 DecisionEvidence 精确关联的模拟订单，"
                        "但尚未产生可对账成交。"
                    ),
                ))
            else:
                report.differences.append(ReconciliationDiff(
                    symbol_id=sid,
                    kind="MISSING_ORDER_PLAN",
                    expected={"target_qty_delta": delta},
                    actual={"filled_delta": 0},
                    detail=(
                        f"DecisionEvidence 动作={delta}，但该 symbol 在 trade_date={trade_date} "
                        "无任何订单/撮合记录。"
                    ),
                ))
        elif abs(filled - delta) > 0:
            report.differences.append(ReconciliationDiff(
                symbol_id=sid,
                kind="UNFILLED_PLAN",
                expected={"target_qty_delta": delta},
                actual={"filled_delta": filled},
            ))

    # 4. R3：最终持仓与目标持仓一致
    post_positions: dict[int, int]
    if positions_provider is not None:
        post_positions = dict(positions_provider(db, portfolio_id, None) or {})
    else:
        post_positions = {
            int(pos.symbol_id): int(pos.quantity or 0)
            for pos in db.execute(
                select(Position).where(Position.portfolio_id == portfolio_id)
            ).scalars().all()
        }
    report.actual_position_count = sum(1 for q in post_positions.values() if q > 0)
    report.expected_position_count = sum(
        1 for sid, q in expected_target_quantity.items() if q > 0
    )

    for sid, target_qty in expected_target_quantity.items():
        actual = int(post_positions.get(sid, 0))
        if target_qty != actual and sid in expected_qty_delta and expected_qty_delta[sid] != 0:
            report.differences.append(ReconciliationDiff(
                symbol_id=sid,
                kind="POSITION_MISMATCH",
                expected={"target_quantity": target_qty},
                actual={"final_position_quantity": actual},
                detail=(
                    f"{sid}: 目标持仓 {target_qty} ≠ 实际 {actual}"
                ),
            ))

    _finalize_report(db, p, report, operator_id, correlation_id)
    return report


# ──────────────────────────────────────────────────────────── 推进 last_reconciled_trade_date
def _finalize_report(
    db: Session,
    p: Portfolio,
    report: ReconciliationReport,
    operator_id: str,
    correlation_id: str | None,
) -> None:
    diffs_public = [
        {
            "symbol_id": d.symbol_id,
            "kind": d.kind,
            "expected": d.expected,
            "actual": d.actual,
            "detail": d.detail,
        }
        for d in report.differences
    ]
    if report.has_any_diff:
        report.status = "BLOCKED"
        # The audit result and the order-entry stop must be one transaction.
        # ``NOT_CHECKED`` stays compatibility-only through ``has_any_diff``;
        # it must never pause a legacy portfolio merely because its old
        # execution ledger cannot be reconstructed.
        from app.services.portfolio_state_machine import _get_status, _set_status

        current_state = _get_status(db, p)
        if current_state != "ADMIN_PAUSED":
            _set_status(db, p, "RECONCILIATION_BLOCKED")
        attributes = {
            "trade_date": report.trade_date.isoformat(),
            "decision_run_id": report.decision_run_id,
            "expected_position_count": report.expected_position_count,
            "actual_position_count": report.actual_position_count,
            "expected_cash": report.expected_cash,
            "actual_cash": report.actual_cash,
            "diffs_count_non_checked": sum(
                1 for d in report.differences if d.kind != "NOT_CHECKED"
            ),
            "portfolio_state_before": current_state,
            "portfolio_state_after": (
                "ADMIN_PAUSED" if current_state == "ADMIN_PAUSED"
                else "RECONCILIATION_BLOCKED"
            ),
            "diffs": diffs_public,
        }
        # 不推进 last_reconciled_trade_date；留待人工 confirm_reconciliation_fixed
        report.audit = write_audit_event(
            db, "RECONCILIATION_RESULT",
            portfolio_id=p.id,
            business_key=report.decision_run_id or f"portfolio::{p.id}::{report.trade_date.isoformat()}",
            attributes=attributes,
            note="对账存在差异，等待人工确认修复。",
            occurred_at=None,
            operator_id=operator_id,
            correlation_id=correlation_id,
        )
    else:
        report.status = "PASSED"
        # 单调推进 last_reconciled_trade_date（只前进，不允许回退，跨日安全）
        if p.last_reconciled_trade_date is None or p.last_reconciled_trade_date < report.trade_date:
            p.last_reconciled_trade_date = report.trade_date
        attributes = {
            "trade_date": report.trade_date.isoformat(),
            "decision_run_id": report.decision_run_id,
            "promoted_last_reconciled_trade_date": (
                p.last_reconciled_trade_date.isoformat()
                if p.last_reconciled_trade_date else None
            ),
        }
        report.audit = write_audit_event(
            db, "RECONCILIATION_RESULT",
            portfolio_id=p.id,
            business_key=report.decision_run_id or f"portfolio::{p.id}::{report.trade_date.isoformat()}",
            attributes=attributes,
            note="对账差异清零，last_reconciled_trade_date 单调推进。",
            occurred_at=None,
            operator_id=operator_id,
            correlation_id=correlation_id,
        )
