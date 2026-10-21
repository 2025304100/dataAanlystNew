"""WP0-2 / G3-3 组合对账服务：T+1 日对 T 日决策执行结果做一致性核对。

对账链路（沿 tasks.md §WP0-2 「last_reconciled_trade_date」口径）：
  Inputs（任一缺失仍要尽力对，结果含 NOT_CHECKED）：
    (A) 决策侧证据：DecisionRun(run_type=auto_simulation, trade_date=T) +
                    DecisionEvidence(symbol, action, target_quantity)
    (B) 订单侧计划：OrderPlan / SimulationOrder（本项目用 BacktestTrade 或 外部订单系统）
    (C) 撮合结果：simulation_matching_engine.MatchResult（FILLED / 部分成交 / 拒单）
    (D) 最终持仓：执行日快照，或完整模拟成交账本按执行日重建

  核对 4 类差异：
    R1. 有决策 BUY/SELL 但无订单计划 → MISSING_ORDER_PLAN
    R2. 有订单计划但无撮合结果（T+1 仍停留 PENDING）→ UNFILLED_PLAN
    R3. 撮合后最终持仓与预期不一致（数量/方向/成本）→ POSITION_MISMATCH
    R4. 现金 + Σ(持仓×收盘价) ≠ 期初资产（守恒破坏）→ NAV_BROKEN

  输出：
    - ReconciliationReport（差异明细 + 汇总）
    - 若无差异：Portfolio.last_reconciled_trade_date = T（只推进，不跳日，不等号安全）
    - 若有差异或关键证据未核验：写入 RECONCILIATION_RESULT 审计事件并进入
      RECONCILIATION_BLOCKED，留待人工修正 + 确认
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models.backtest import BacktestRun, BacktestTrade  # legacy/import evidence
from app.models.decision_engine import DecisionRun as DecisionRunORM, DecisionEvidence
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.services.cash_ledger_diagnosis import diagnose_cash_ledger

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
    cash_audit: dict[str, Any] | None = None

    @property
    def has_any_diff(self) -> bool:
        # Missing evidence is an incomplete check, never a clean account.
        return bool(self.differences)


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


def _positions_at_execution_date(
    db: Session, portfolio: Portfolio, execution_date: date,
    cash_audit: dict[str, Any] | None,
) -> dict[int, float] | None:
    """Rebuild only a complete, attributable simulated account from its fills.

    Position has no historical date. It is used only to validate completeness
    of the lifetime fill ledger, never as the requested historical snapshot.
    Unexplained manual positions and legacy missing fills remain unverified.
    """
    if not cash_audit or cash_audit.get("status") != "CONSISTENT":
        return None
    opening = db.execute(select(CashLedger.entry_type, CashLedger.ref_type,
        CashLedger.ref_id, CashLedger.note, CashLedger.amount, CashLedger.balance_after)
        .where(CashLedger.portfolio_id == portfolio.id)
        .order_by(CashLedger.id).limit(1)).first()
    if opening is None or opening.entry_type != "deposit":
        return None
    standard_funding = (portfolio.account_type == "simulated"
        and opening.ref_type == "portfolio" and opening.ref_id == portfolio.id
        and opening.note == "Initial simulated funding")
    replay_funding = False
    if opening.ref_type == "backtest_run" and opening.ref_id is not None:
        source = db.execute(select(BacktestRun.status, BacktestRun.initial_capital)
            .where(BacktestRun.id == opening.ref_id)).first()
        # Applying a run to a different simulated portfolio is supported.
        # Its source portfolio therefore cannot be used as the ownership test.
        replay_funding = (portfolio.account_type == "simulated" and source is not None
            and source.status == "success" and float(source.initial_capital or 0) > 0
            and round(float(opening.amount), 2) == round(float(source.initial_capital), 2)
            and round(float(opening.balance_after), 2) == round(float(source.initial_capital), 2)
            and opening.note in {
                f"Apply backtest run #{opening.ref_id} initial capital",
                f"Apply backtest run #{opening.ref_id} initial capital (no trades)",
            })
    if not standard_funding and not replay_funding:
        return None

    tables = inspect(db.get_bind()).get_table_names()
    if "sim_orders" not in tables or "sim_trades" not in tables:
        return None
    orders = {int(row.id): row for row in db.execute(select(SimOrder.id,
        SimOrder.symbol_id, SimOrder.side, SimOrder.filled_quantity)
        .where(SimOrder.portfolio_id == portfolio.id)).all()}
    fills = db.execute(select(SimTrade.id, SimTrade.order_id, SimTrade.symbol_id,
        SimTrade.side, SimTrade.quantity, SimTrade.created_at)
        .where(SimTrade.portfolio_id == portfolio.id)
        .order_by(SimTrade.created_at, SimTrade.id)).all()
    cash_refs = db.execute(select(CashLedger.ref_type, CashLedger.ref_id)
        .where(CashLedger.portfolio_id == portfolio.id,
            CashLedger.entry_type.in_(["buy", "sell"]))).all()
    if any(row.ref_type != "sim_order" or row.ref_id is None for row in cash_refs):
        return None
    filled_by_order: dict[int, float] = {}
    total: dict[int, float] = {}
    historical: dict[int, float] = {}
    for fill in fills:
        order = orders.get(int(fill.order_id))
        side = str(fill.side or "").lower()
        quantity = float(fill.quantity or 0)
        if (order is None or order.symbol_id != fill.symbol_id
            or str(order.side or "").lower() != side
            or side not in {"buy", "sell"} or quantity <= 0 or fill.created_at is None):
            return None
        filled_by_order[int(fill.order_id)] = filled_by_order.get(int(fill.order_id), 0) + quantity
        sid = int(fill.symbol_id)
        delta = quantity if side == "buy" else -quantity
        total[sid] = total.get(sid, 0) + delta
        if total[sid] < -1e-6:  # A sale requiring an unknown opening position.
            return None
        timestamp = fill.created_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date() <= execution_date:
            historical[sid] = historical.get(sid, 0) + delta
    if any(abs(float(order.filled_quantity or 0) - filled_by_order.get(oid, 0)) > 1e-6
        for oid, order in orders.items()):
        return None
    current = {int(sid): float(qty or 0) for sid, qty in db.execute(
        select(Position.symbol_id, Position.quantity).where(Position.portfolio_id == portfolio.id))}
    if any(abs(current.get(sid, 0) - total.get(sid, 0)) > 1e-6
        for sid in set(current) | set(total)):
        return None
    return historical


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
    positions_provider=None,  # Callable[[Session, int, date], dict[int, int] | None]
    persist: bool = True,
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

    def finish() -> None:
        if persist:
            _finalize_report(db, p, report, operator_id, correlation_id)
        else:
            report.status = "BLOCKED" if report.has_any_diff else "PASSED"

    # Cash arithmetic is meaningful even when today's automatic execution
    # evidence is missing. Its absence must not hide an existing book gap.
    if "cash_ledger" in inspect(db.get_bind()).get_table_names():
        rows = db.execute(select(CashLedger.id, CashLedger.entry_type,
            CashLedger.amount, CashLedger.balance_after, CashLedger.ref_type, CashLedger.ref_id)
            .where(CashLedger.portfolio_id == portfolio_id)
            .order_by(CashLedger.id)).mappings().all()
        fills = None
        if 'sim_trades' in inspect(db.get_bind()).get_table_names():
            fills = [dict(row) for row in db.execute(select(SimTrade.order_id,
                SimTrade.side, SimTrade.amount, SimTrade.fee)
                .where(SimTrade.portfolio_id == portfolio_id)).mappings()]
        report.cash_audit = diagnose_cash_ledger([dict(row) for row in rows], fills)
        if report.cash_audit.get('expected_cash') is not None:
            report.expected_cash = report.cash_audit['expected_cash']
            report.actual_cash = report.cash_audit['actual_cash']
        if report.cash_audit['status'] == 'BROKEN':
            report.differences.append(ReconciliationDiff(
                symbol_id=None, kind='NAV_BROKEN',
                expected={"cash": report.expected_cash}, actual={"cash": report.actual_cash},
                detail='完整现金账本的收支累计与记录余额不一致；需核对历史流水，未经确认不调整账本。',
            ))
    if not report.cash_audit or report.cash_audit.get("status") in {"NOT_CHECKED", "PARTIAL"}:
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail="现金流水缺失或缺少可核验的期初资金，现金核对尚未完成。",
        ))

    if dr is None:
        report.differences.append(ReconciliationDiff(
            symbol_id=None,
            kind="DECISION_RUN_NOT_FOUND",
            detail=(
                f"portfolio_id={portfolio_id} trade_date={trade_date.isoformat()} "
                "无 SUCCEEDED auto_simulation DecisionRun（未接管/当日仍未执行）"
            ),
        ))
        finish()
        return report

    # 2. 决策侧预期（只关心 BUY/SELL/HOLD）
    evidences = db.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.decision_run_id == str(dr.id),
        )
    ).scalars().all()
    if not evidences and (dr.universe_count or dr.member_count):
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail="决策运行已完成，但缺少对应的决策明细，尚不能完成对账。",
        ))
    expected_qty_delta: dict[int, int] = {}  # symbol_id -> target_qty_delta
    expected_target_quantity: dict[int, int] = {}
    unknown_target_symbols: set[int] = set()
    evidence_id_by_symbol: dict[int, str] = {}
    for ev in evidences:
        if ev.symbol_id is None:
            continue
        delta = int(getattr(ev, "target_qty_delta", 0) or 0)
        target = int(getattr(ev, "target_quantity", 0) or 0)
        action = str(getattr(ev, "action", "") or "")
        if ev.target_quantity is None:
            unknown_target_symbols.add(int(ev.symbol_id))
            report.differences.append(ReconciliationDiff(
                symbol_id=int(ev.symbol_id), kind="NOT_CHECKED",
                detail="决策记录缺少目标持仓数量，不能按零持仓核对。",
            ))
        if action in {"BUY", "SELL"}:
            expected_qty_delta[ev.symbol_id] = delta
            expected_target_quantity[ev.symbol_id] = target
            evidence_id_by_symbol[ev.symbol_id] = str(ev.id)
        elif action in {"HOLD", "NO_ACTION", "DATA_BLOCKED", "REJECTED"}:
            # 仍在范围内，记录为 0 变化（便于 R3 比对最终持仓与目标）
            expected_qty_delta.setdefault(ev.symbol_id, 0)
            if ev.target_quantity is not None:
                expected_target_quantity.setdefault(ev.symbol_id, target)

    # 3. 订单 / 撮合侧实际
    trades: list = []
    execution_source_connected = False
    order_evidence_ids: set[str] = set()
    if trades_provider is not None:
        trades = list(trades_provider(db, portfolio_id, trade_date) or [])
        execution_source_connected = True
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

        # Legacy/import data predating SimOrder attribution may still be
        # uncheckable. Never let that absence hide a partial linked order.
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

    if trades_provider is None and not execution_source_connected and any(expected_qty_delta.values()):
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
    execution_date = dr.execution_at.date()
    post_positions: dict[int, int] | None
    if positions_provider is not None:
        # A provider supplies a dated closing snapshot, not today's portfolio.
        supplied = positions_provider(db, portfolio_id, execution_date)
        post_positions = None if supplied is None else dict(supplied)
    else:
        post_positions = _positions_at_execution_date(db, p, execution_date, report.cash_audit)
    if post_positions is None:
        report.differences.append(ReconciliationDiff(
            symbol_id=None, kind="NOT_CHECKED",
            detail=f"缺少 {execution_date.isoformat()} 的可核验持仓记录，不能用当前持仓代替历史持仓。",
        ))
        finish()
        return report
    report.actual_position_count = sum(1 for q in post_positions.values() if q > 0)
    report.expected_position_count = sum(
        1 for sid, q in expected_target_quantity.items() if q > 0
    )

    for sid in sorted(set(expected_target_quantity) | set(post_positions)):
        if sid in unknown_target_symbols:
            continue
        target_qty = expected_target_quantity.get(sid, 0)
        actual = int(post_positions.get(sid, 0))
        if target_qty != actual:
            report.differences.append(ReconciliationDiff(
                symbol_id=sid,
                kind="POSITION_MISMATCH",
                expected={"target_quantity": target_qty},
                actual={"final_position_quantity": actual},
                detail=(
                    f"{sid}: 目标持仓 {target_qty} ≠ 实际 {actual}"
                ),
            ))

    finish()
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
        # Incomplete historical evidence also blocks advancement; callers
        # must never advertise an unverified account as reconciled.
        from app.services.portfolio_state_machine import _get_status, _set_status

        current_state = _get_status(db, p)
        if current_state != "ADMIN_PAUSED":
            _set_status(db, p, "RECONCILIATION_BLOCKED")
        attributes = {
            "status": report.status,
            "trade_date": report.trade_date.isoformat(),
            "decision_run_id": report.decision_run_id,
            "expected_position_count": report.expected_position_count,
            "actual_position_count": report.actual_position_count,
            "expected_cash": report.expected_cash,
            "actual_cash": report.actual_cash,
            "diffs_count_non_checked": sum(
                1 for d in report.differences if d.kind != "NOT_CHECKED"
            ),
            "incomplete_checks_count": sum(
                1 for d in report.differences if d.kind == "NOT_CHECKED"
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
