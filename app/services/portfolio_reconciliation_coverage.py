"""Verified current-account baselines without rewriting unknown history.

Only the existing simulated-account book is an implemented source adapter.
Cash is checked against funding movements and attributed fills; holdings and
costs are rebuilt independently from the complete fill journal. A source gap,
unexplained difference, unresolved obligation, changed fingerprint, or active
account task prevents acceptance. External account statements are not guessed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any
from uuid import uuid4

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.orm import Session

from app.models.portfolio import Portfolio
from app.models.portfolio_reconciliation_coverage import (
    PortfolioAccountBaseline, PortfolioReconciliationCursor, PortfolioReconciliationDay,
    PortfolioReconciliationReport,
)
from app.schemas.portfolio_reconciliation_coverage import (
    CoverageAccepted, CoverageAcceptRequest, CoverageBlocker, CoveragePreview,
    CoveragePreviewRequest,
)
from app.services.data_governance_audit import DataGovernanceAuditEvent, write_audit_event


POLICY_VERSION = "sim-account-baseline-v1"
SHANGHAI = timezone(timedelta(hours=8))
MONEY_TOLERANCE = Decimal("0.01")
QUANTITY_TOLERANCE = Decimal("0.000001")
SOURCE_COLUMNS = {
    "cash_ledger": {"id", "portfolio_id", "entry_type", "amount", "balance_after", "ref_type", "ref_id", "created_at"},
    "positions": {"id", "portfolio_id", "symbol_id", "quantity", "avg_cost", "updated_at"},
    "sim_orders": {"id", "portfolio_id", "symbol_id", "side", "quantity", "status", "filled_quantity", "filled_amount", "fee", "decision_evidence_id", "review_status", "created_at"},
    "sim_trades": {"id", "portfolio_id", "symbol_id", "order_id", "side", "quantity", "price", "amount", "fee", "created_at"},
    "decision_runs": {"id", "portfolio_id", "run_type", "status", "created_at"},
    "decision_evidence": {"id", "portfolio_id", "decision_run_id", "symbol_id", "action", "target_quantity"},
    "decision_order_plans": {"order_plan_id", "portfolio_id", "decision_run_id", "evidence_id", "symbol_id", "action", "target_quantity"},
}
TERMINAL_ORDERS = {"filled", "rejected", "cancelled", "expired", "timed_out_cancelled"}


class CoverageError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None, status_code: int = 409):
        super().__init__(message)
        self.code, self.details, self.status_code = code, details, status_code


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except (InvalidOperation, TypeError, ValueError):
        pass
    raise CoverageError("INVALID_ACCOUNT_AMOUNT", "账户来源包含非有限金额或缺少数量，不能建立基线")


def _str(value: Decimal) -> str:
    return format(value, "f")


def _block(code: str, message: str, details: Any = None) -> CoverageBlocker:
    return CoverageBlocker(code=code, message=message, details=details)


def _cursor(db: Session, portfolio_id: int, *, lock: bool = False) -> PortfolioReconciliationCursor | None:
    statement = select(PortfolioReconciliationCursor).where(
        PortfolioReconciliationCursor.portfolio_id == portfolio_id,
        PortfolioReconciliationCursor.scope_key == "account",
    ).execution_options(populate_existing=True)
    return db.scalar(statement.with_for_update() if lock else statement)


def _cursor_snapshot(cursor: PortfolioReconciliationCursor | None) -> dict[str, Any] | None:
    if cursor is None:
        return None
    values = {key: getattr(cursor, key) for key in (
        "id", "portfolio_id", "scope_key", "generation", "baseline_id", "coverage_start_trade_date",
        "verified_through_trade_date", "calendar_version", "policy_version", "row_version",
    )}
    # Governance audit canonical JSON does not serialize Python dates. Freeze
    # explicit ISO values instead of letting its legacy _raw_repr fallback erase
    # the structured before/after cursor proof.
    return {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in values.items()}


def _read_sources(db: Session, portfolio_id: int, *, lock: bool) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[CoverageBlocker]]:
    inspector = inspect(db.connection())
    tables = set(inspector.get_table_names())
    sources: dict[str, list[dict[str, Any]]] = {}
    references: list[dict[str, Any]] = []
    blockers: list[CoverageBlocker] = []
    metadata = MetaData()
    for name, required in SOURCE_COLUMNS.items():
        columns = {col["name"] for col in inspector.get_columns(name)} if name in tables else set()
        missing = sorted(required - columns)
        if missing:
            blockers.append(_block("ACCOUNT_SOURCE_MISSING", f"缺少当前账户来源：{name}", {"source": name, "missing_columns": missing}))
            sources[name] = []
            references.append({"source": name, "available": False, "missing_columns": missing})
            continue
        table = Table(name, metadata, autoload_with=db.connection())
        key = "id" if "id" in table.c else "order_plan_id"
        statement = select(table).where(table.c.portfolio_id == portfolio_id).order_by(table.c[key])
        if lock:
            statement = statement.with_for_update()
        rows = [dict(row) for row in db.execute(statement).mappings()]
        sources[name] = rows
        references.append({"source": name, "available": True, "row_ids": [str(row[key]) for row in rows],
                           "row_count": len(rows), "content_hash": _hash(rows)})
    return sources, references, blockers


def _verify_account(sources: dict[str, list[dict[str, Any]]], portfolio_id: int) -> tuple[dict[str, Any], list[CoverageBlocker]]:
    blockers: list[CoverageBlocker] = []
    cash, positions, orders, trades = (sources[name] for name in ("cash_ledger", "positions", "sim_orders", "sim_trades"))
    order_map = {row["id"]: row for row in orders}
    trade_by_order: dict[int, dict[str, Decimal]] = {}
    quantities: dict[int, Decimal] = {}
    costs: dict[int, Decimal] = {}
    trade_cash = Decimal(0)
    for trade in sorted(trades, key=lambda row: (row["created_at"], row["id"])):
        sid, side, oid = trade["symbol_id"], str(trade["side"]).lower(), trade["order_id"]
        quantity, price, amount, fee = (_decimal(trade[key]) for key in ("quantity", "price", "amount", "fee"))
        if side not in {"buy", "sell"} or quantity <= 0 or price <= 0 or amount < 0 or fee < 0:
            blockers.append(_block("INVALID_TRADE_FACT", "存在方向、数量或金额不明确的成交", {"trade_id": trade["id"]}))
            continue
        if abs(quantity * price - amount) > MONEY_TOLERANCE:
            blockers.append(_block("TRADE_AMOUNT_MISMATCH", "成交数量乘价格与成交金额不符", {"trade_id": trade["id"]}))
        order = order_map.get(oid)
        if not order or order["symbol_id"] != sid or str(order["side"]).lower() != side:
            blockers.append(_block("UNEXPLAINED_TRADE", "成交缺少同组合、同标的、同方向订单来源", {"trade_id": trade["id"], "order_id": oid}))
        totals = trade_by_order.setdefault(oid, {"quantity": Decimal(0), "amount": Decimal(0), "fee": Decimal(0), "cash": Decimal(0)})
        delta = -(amount + fee) if side == "buy" else amount - fee
        for key, value in (("quantity", quantity), ("amount", amount), ("fee", fee), ("cash", delta)):
            totals[key] += value
        trade_cash += delta
        previous, average = quantities.get(sid, Decimal(0)), costs.get(sid, Decimal(0))
        if side == "buy":
            quantities[sid] = previous + quantity
            costs[sid] = ((previous * average + quantity * price) / quantities[sid]).quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
        else:
            if quantity > previous + QUANTITY_TOLERANCE:
                blockers.append(_block("POSITION_ORIGIN_UNKNOWN", "成交历史包含没有可信期初持仓支持的卖出", {"trade_id": trade["id"]}))
            quantities[sid] = previous - quantity
            costs[sid] = average if quantities[sid] > 0 else Decimal(0)
    for order in orders:
        status = str(order["status"]).lower()
        if status not in TERMINAL_ORDERS or str(order.get("review_status") or "").upper() == "PENDING_REVIEW":
            blockers.append(_block("OUTSTANDING_ORDER", "存在未完成订单、待审核订单或未知订单状态", {"order_id": order["id"], "status": order["status"]}))
        totals = trade_by_order.get(order["id"], {"quantity": Decimal(0), "amount": Decimal(0), "fee": Decimal(0)})
        for field, key, tolerance in (("filled_quantity", "quantity", QUANTITY_TOLERANCE), ("filled_amount", "amount", MONEY_TOLERANCE), ("fee", "fee", MONEY_TOLERANCE)):
            if abs(_decimal(order[field]) - totals[key]) > tolerance:
                blockers.append(_block("ORDER_FILL_MISMATCH", "订单成交汇总与独立成交明细不符", {"order_id": order["id"], "field": field}))
        if status == "filled" and abs(_decimal(order["quantity"]) - totals["quantity"]) > QUANTITY_TOLERANCE:
            blockers.append(_block("UNFINISHED_FILLED_ORDER", "订单声称完成但成交数量不足", {"order_id": order["id"]}))
        if _decimal(order["quantity"]) <= 0 or totals["quantity"] > _decimal(order["quantity"]) + QUANTITY_TOLERANCE:
            blockers.append(_block("ORDER_OVERFILL", "订单请求数量异常或实际成交超出委托范围，需先核查执行差异", {"order_id": order["id"]}))
    actual_positions = {row["symbol_id"]: row for row in positions}
    if len(actual_positions) != len(positions):
        blockers.append(_block("DUPLICATE_POSITION", "同标的当前持仓重复，必须先修复账户"))
    frozen_positions: list[dict[str, Any]] = []
    for sid in sorted(set(actual_positions) | set(quantities)):
        actual = actual_positions.get(sid)
        recorded_qty = _decimal(actual["quantity"]) if actual else Decimal(0)
        recorded_cost = _decimal(actual["avg_cost"]) if actual else Decimal(0)
        expected_qty, expected_cost = quantities.get(sid, Decimal(0)), costs.get(sid, Decimal(0))
        if recorded_qty < 0 or abs(recorded_qty - expected_qty) > QUANTITY_TOLERANCE or (recorded_qty > 0 and abs(recorded_cost - expected_cost) > MONEY_TOLERANCE):
            blockers.append(_block("POSITION_MISMATCH", "当前逐标的持仓或成本与独立成交重建不符", {
                "symbol_id": sid, "actual_quantity": _str(recorded_qty), "expected_quantity": _str(expected_qty),
                "actual_avg_cost": _str(recorded_cost), "expected_avg_cost": _str(expected_cost),
            }))
        if actual and recorded_qty != 0:
            frozen_positions.append({"symbol_id": sid, "quantity": _str(recorded_qty), "avg_cost": _str(recorded_cost), "source_ref": f"positions:{actual['id']}"})
    actual_cash: Decimal | None = None
    funding = Decimal(0)
    ledger_trading: dict[int, Decimal] = {}
    previous = Decimal(0)
    if not cash:
        blockers.append(_block("CASH_LEDGER_MISSING", "缺少可信期初入金和完整现金流水，不能用总资金或净值汇总代替"))
    for index, row in enumerate(cash):
        kind, amount, balance = str(row["entry_type"]).lower(), _decimal(row["amount"]), _decimal(row["balance_after"])
        if index == 0 and (kind != "deposit" or amount <= 0 or abs(balance - amount) > MONEY_TOLERANCE):
            blockers.append(_block("CASH_OPENING_UNKNOWN", "现金流水没有可核验的期初入金，历史初值未知"))
        if abs(previous + amount - balance) > MONEY_TOLERANCE:
            blockers.append(_block("CASH_LEDGER_MISMATCH", "现金流水相邻余额不守恒", {"ledger_id": row["id"]}))
        previous = actual_cash = balance
        if kind in {"buy", "sell"}:
            if row["ref_type"] != "sim_order" or row["ref_id"] not in order_map:
                blockers.append(_block("UNATTRIBUTED_CASH", "交易现金流水缺少同组合真实订单引用", {"ledger_id": row["id"]}))
            else:
                oid = row["ref_id"]
                ledger_trading[oid] = ledger_trading.get(oid, Decimal(0)) + amount
        elif kind in {"deposit", "withdraw"} and row["ref_type"] == "portfolio" and row["ref_id"] == portfolio_id:
            funding += amount
            if (kind == "deposit" and amount <= 0) or (kind == "withdraw" and amount >= 0):
                blockers.append(_block("INVALID_FUNDING_MOVEMENT", "入金或出金的方向与金额符号不符", {"ledger_id": row["id"]}))
        else:
            blockers.append(_block("UNVERIFIED_FUNDING_SOURCE", "存在未接入独立核验的现金调整或导入来源", {"ledger_id": row["id"], "ref_type": row["ref_type"]}))
    for oid in sorted(set(ledger_trading) | set(trade_by_order)):
        wanted = trade_by_order.get(oid, {}).get("cash", Decimal(0))
        if oid not in ledger_trading or oid not in trade_by_order or abs(ledger_trading.get(oid, Decimal(0)) - wanted) > MONEY_TOLERANCE:
            blockers.append(_block("CASH_TRADE_MISMATCH", "逐单现金流水与成交及费用独立核算不符", {"order_id": oid}))
    expected_cash = funding + trade_cash if cash else None
    if actual_cash is not None and (actual_cash < 0 or abs(actual_cash - expected_cash) > MONEY_TOLERANCE):
        blockers.append(_block("CASH_ACCOUNT_MISMATCH", "当前现金与入出金、成交、费用独立核算不符"))
    runs = {row["id"]: row for row in sources["decision_runs"]}
    auto_runs = {key: row for key, row in runs.items() if row["run_type"] == "auto_simulation"}
    for run in auto_runs.values():
        if str(run["status"]).upper() in {"PENDING", "RUNNING"}:
            blockers.append(_block("ACCOUNT_EXECUTION_ACTIVE", "当前组合仍有执行中的正式任务", {"decision_run_id": run["id"]}))
    order_evidence = {row["decision_evidence_id"] for row in orders if row["decision_evidence_id"] and str(row["status"]).lower() in TERMINAL_ORDERS}
    for table, evidence_key in (("decision_order_plans", "evidence_id"), ("decision_evidence", "id")):
        for item in sources[table]:
            if item["decision_run_id"] not in runs:
                blockers.append(_block("ORPHAN_EXECUTION_INTENT", "执行意图缺少可核验的同组合运行来源", {"source": table, "ref": item.get("id") or item.get("order_plan_id")}))
            elif item["decision_run_id"] in auto_runs and str(item["action"]).upper() in {"BUY", "SELL"} and _decimal(item["target_quantity"] or 0) > 0 and item[evidence_key] not in order_evidence:
                blockers.append(_block("OUTSTANDING_EXECUTION_INTENT", "正式执行意图尚未找到已终结订单，义务未核清", {"source": table, "ref": item.get("id") or item.get("order_plan_id")}))
    return {"cash": _str(actual_cash) if actual_cash is not None else None,
            "independent_expected_cash": _str(expected_cash) if expected_cash is not None else None,
            "positions": frozen_positions, "unresolved_obligation_count": sum(1 for item in blockers if item.code in {"OUTSTANDING_ORDER", "ACCOUNT_EXECUTION_ACTIVE", "OUTSTANDING_EXECUTION_INTENT", "ORPHAN_EXECUTION_INTENT"}),
            "verification_method": "cash_book_vs_attributed_fills_and_positions_vs_complete_fill_journal"}, blockers


def _trusted_calendar(db: Session, at: datetime, *, lock: bool) -> dict[str, Any]:
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    result = resolve_inspection_calendar(db, market="CN", at=_utc(at).replace(tzinfo=timezone.utc), current_read=lock)
    if not result.get("version"):
        raise CoverageError("CALENDAR_READ_FAILED" if result.get("status") == "READ_ERROR" else "CALENDAR_PROVENANCE_REQUIRED",
                            result.get("reason") or "日历缺少可核验的交易所同步来源")
    return result


def _calendar(db: Session, today: date, requested: date | None, *, lock: bool, at: datetime | None = None) -> tuple[date | None, str | None, list[CoverageBlocker]]:
    try:
        trusted = _trusted_calendar(db, at or _utc(), lock=lock)
    except CoverageError as exc:
        return requested, None, [_block(exc.code, str(exc))]
    if requested and requested > today + timedelta(days=31):
        return requested, None, [_block("COVERAGE_START_TOO_DISTANT", "新覆盖起点应是当前快照之后的下一交易日，不能选择远期日期")]
    inspector = inspect(db.connection())
    if "trade_calendar" not in inspector.get_table_names():
        return requested, None, [_block("CALENDAR_SOURCE_MISSING", "缺少真实交易日历，不能按工作日猜测新覆盖起点")]
    table = Table("trade_calendar", MetaData(), autoload_with=db.connection())
    if not {"date", "is_trading_day"}.issubset(table.c.keys()):
        return requested, None, [_block("CALENDAR_SOURCE_MISSING", "交易日历字段不完整")]
    end = requested if requested and requested > today else today + timedelta(days=31)
    statement = select(table).where(table.c.date > today, table.c.date <= end).order_by(table.c.date)
    rows = list(db.execute(statement.with_for_update() if lock else statement).mappings())
    if any(row["is_trading_day"] not in {0, 1} for row in rows):
        return requested, None, [_block("CALENDAR_SOURCE_INVALID", "真实交易日历包含未识别的开休市标记，不能接受覆盖起点")]
    next_open = next((row["date"] for row in rows if row["is_trading_day"] == 1), None)
    target = requested or next_open
    if target is None or target <= today:
        return target, None, [_block("COVERAGE_START_INVALID", "新覆盖起点必须是快照之后的真实交易日，不能回填当天或历史期初")]
    indexed = {row["date"]: row for row in rows}
    missing = [today + timedelta(days=offset) for offset in range(1, (target - today).days + 1) if today + timedelta(days=offset) not in indexed]
    if missing or target != next_open:
        return target, None, [_block("CALENDAR_COVERAGE_INCOMPLETE", "新覆盖起点必须是已登记完整日历中的下一交易日，不能跳过日期或缺失行", {"missing_dates": missing, "next_open": next_open})]
    frozen = [{"date": row["date"], "is_trading_day": row["is_trading_day"]} for row in rows if row["date"] <= target]
    return target, _hash({"source_version": trusted["version"], "rows": frozen}), []


def preview_reconciliation_coverage(
    db: Session, portfolio_id: int, request: CoveragePreviewRequest, *,
    now: datetime | None = None, current_read: bool = False,
) -> CoveragePreview:
    with db.no_autoflush:
        statement = select(Portfolio).where(Portfolio.id == portfolio_id).execution_options(populate_existing=True)
        portfolio = db.scalar(statement.with_for_update() if current_read else statement)
        if portfolio is None:
            raise CoverageError("PORTFOLIO_NOT_FOUND", "组合不存在", status_code=404)
        cursor = _cursor(db, portfolio_id, lock=current_read)
        sources, source_refs, blockers = _read_sources(db, portfolio_id, lock=current_read)
        as_of = _utc(now)
        for source, rows in sources.items():
            for row in rows:
                for field in ("created_at", "updated_at", "filled_at"):
                    stamp = row.get(field)
                    if isinstance(stamp, datetime) and _utc(stamp) > as_of:
                        blockers.append(_block("ACCOUNT_SOURCE_AFTER_SNAPSHOT", "账户来源时间晚于本次快照时点，不能当作已发生事实接受", {
                            "source": source, "record_id": row.get("id") or row.get("order_plan_id"), "field": field,
                        }))
        if portfolio.account_type != "simulated":
            blockers.append(_block("EXTERNAL_ACCOUNT_EVIDENCE_UNAVAILABLE", "当前账户类型缺少独立账户对账单与未决义务来源，暂不能建立可信基线"))
        if portfolio.currency != "CNY":
            blockers.append(_block("CURRENCY_SOURCE_UNSUPPORTED", "当前来源只核验人民币模拟账本，其他币种尚缺独立资金与汇率来源"))
        if portfolio.lease_expire_at and _utc(portfolio.lease_expire_at) > as_of:
            blockers.append(_block("ACCOUNT_LEASE_ACTIVE", "账户执行租约尚未释放，必须等待当前执行结束"))
        if cursor and cursor.lease_expires_at and _utc(cursor.lease_expires_at) > as_of:
            blockers.append(_block("COVERAGE_LEASE_ACTIVE", "当前核对队列正在处理，不能同时切换覆盖代次"))
        snapshot: dict[str, Any] = {"currency": portfolio.currency, "cash": None, "positions": [], "unresolved_obligation_count": None}
        if not any(item.code == "ACCOUNT_SOURCE_MISSING" for item in blockers):
            try:
                verified, account_blockers = _verify_account(sources, portfolio_id)
                snapshot.update(verified)
                blockers.extend(account_blockers)
            except CoverageError as exc:
                blockers.append(_block(exc.code, str(exc)))
        # Identity is resolved from the actual current symbol catalogue, never
        # inferred from a bare code, name, or market value.
        inspector = inspect(db.connection())
        if "symbols" in inspector.get_table_names():
            table = Table("symbols", MetaData(), autoload_with=db.connection())
            ids = [item["symbol_id"] for item in snapshot["positions"]]
            identity = {row["id"]: row for row in db.execute(select(table).where(table.c.id.in_(ids))).mappings()} if ids else {}
            for item in snapshot["positions"]:
                symbol = identity.get(item["symbol_id"])
                if symbol and symbol["market"] and symbol["symbol"]:
                    from app.services.portfolio_inspection_diagnostics import CN_MARKETS
                    item.update(security_id=f"{symbol['market'].upper()}:{symbol['symbol']}", name=symbol["name"])
                    if symbol["market"].upper() not in CN_MARKETS:
                        blockers.append(_block("MARKET_CALENDAR_UNSUPPORTED", "跨市场账户缺少完整市场日历接续适配，不能使用单市场日期覆盖"))
                else:
                    blockers.append(_block("SECURITY_IDENTITY_UNKNOWN", "持仓证券市场身份缺失", {"symbol_id": item["symbol_id"]}))
        elif snapshot["positions"]:
            blockers.append(_block("SECURITY_SOURCE_MISSING", "持仓证券身份来源不可用"))
        local_day = as_of.replace(tzinfo=timezone.utc).astimezone(SHANGHAI).date()
        target, calendar_version, calendar_blockers = _calendar(db, local_day, request.coverage_start_trade_date, lock=current_read, at=as_of)
        blockers.extend(calendar_blockers)
        if not request.history_gaps:
            blockers.append(_block("HISTORY_GAPS_REQUIRED", "需登记不可恢复历史区间、已尝试来源与保留引用；备注不能替代账户核验"))
        if target and any(gap.trade_date_to >= target for gap in request.history_gaps):
            blockers.append(_block("GAP_OVERLAPS_NEW_COVERAGE", "历史未知区间不能覆盖新起点或之后日期"))
        if cursor and target and target <= cursor.coverage_start_trade_date:
            blockers.append(_block("COVERAGE_NOT_NEWER", "新基线必须从较新的覆盖起点开始，不能重置同一范围的核验结果"))
        snapshot["source_watermarks"] = source_refs
        fingerprint = _hash({"portfolio_id": portfolio_id, "account_type": portfolio.account_type,
            "snapshot": snapshot, "source_refs": source_refs, "cursor": _cursor_snapshot(cursor),
            "coverage_start_trade_date": target, "history_gaps": [gap.model_dump(mode="json") for gap in request.history_gaps],
            "calendar_version": calendar_version, "policy_version": POLICY_VERSION})
        return CoveragePreview(portfolio_id=portfolio_id, as_of_at=as_of.replace(tzinfo=timezone.utc),
            can_accept=not blockers, coverage_start_trade_date=target, evidence_fingerprint=fingerprint,
            cursor_row_version=cursor.row_version if cursor else 0, current_generation=cursor.generation if cursor else 0,
            snapshot=snapshot, source_refs=source_refs, history_gaps=request.history_gaps,
            blockers=blockers, calendar_version=calendar_version, policy_version=POLICY_VERSION)


def _accepted(baseline: PortfolioAccountBaseline, cursor: PortfolioReconciliationCursor, *, idempotent: bool = False) -> CoverageAccepted:
    payload = json.loads(baseline.snapshot_json)
    return CoverageAccepted(portfolio_id=baseline.portfolio_id, baseline_id=baseline.id,
        cursor_id=cursor.id, generation=payload["generation"], coverage_start_trade_date=baseline.coverage_start_trade_date,
        audit_event_id=baseline.accepted_audit_id, content_hash=baseline.content_hash,
        history_gaps=json.loads(baseline.history_gaps_json), idempotent=idempotent)


def accept_reconciliation_coverage(
    db: Session, portfolio_id: int, request: CoverageAcceptRequest, *, operator_id: str,
    now: datetime | None = None,
) -> CoverageAccepted:
    if not operator_id or not operator_id.strip() or operator_id in {"system", "cron_worker"}:
        raise CoverageError("PERSONAL_REVIEW_REQUIRED", "新覆盖起点必须由本人明确审核，不能由后台任务代签", status_code=403)
    if len(request.review_note.strip()) < 10:
        raise CoverageError("REVIEW_NOTE_REQUIRED", "本人审核理由至少需要10个有效字符", status_code=400)
    request_hash = _hash({"request": request.model_dump(mode="json"), "operator_id": operator_id})
    with db.begin_nested():
        # Lock the common account root before current reads. Existing runtime
        # guards also lock this portfolio; source reads lock current ranges.
        db.scalar(select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update())
        existing = db.scalar(select(PortfolioAccountBaseline).where(
            PortfolioAccountBaseline.portfolio_id == portfolio_id,
            PortfolioAccountBaseline.acceptance_key == request.acceptance_key,
        ))
        if existing:
            if existing.request_hash != request_hash:
                raise CoverageError("ACCEPTANCE_KEY_REUSED", "相同幂等键已用于不同审核内容")
            cursor = _cursor(db, portfolio_id, lock=True)
            if cursor is None or cursor.baseline_id != existing.id:
                raise CoverageError("COVERAGE_ACCEPTANCE_SUPERSEDED", "该审核原已保存，但当前覆盖代次已更新；原记录保留，请按当前代重新读取，不能重新启用旧起点")
            return _accepted(existing, cursor, idempotent=True)
        preview = preview_reconciliation_coverage(db, portfolio_id, request, now=now, current_read=True)
        if request.expected_cursor_row_version != preview.cursor_row_version:
            raise CoverageError("COVERAGE_VERSION_CHANGED", "覆盖代次或核验游标已变化，请重新预览并审核")
        if request.expected_evidence_fingerprint != preview.evidence_fingerprint:
            raise CoverageError("ACCOUNT_EVIDENCE_CHANGED", "账户、义务、日历或来源证据已变化，请重新预览并审核")
        if not preview.can_accept:
            raise CoverageError("BASELINE_NOT_ACCEPTABLE", "当前账户尚不能独立核验，不能仅凭备注接受新覆盖起点",
                                details=[item.model_dump(mode="json") for item in preview.blockers], status_code=428)
        cursor = _cursor(db, portfolio_id, lock=True)
        before = _cursor_snapshot(cursor)
        generation = cursor.generation + 1 if cursor else 1
        baseline_id, cursor_id = f"baseline-{uuid4().hex}", cursor.id if cursor else f"cursor-{uuid4().hex}"
        gaps = [gap.model_dump(mode="json") for gap in request.history_gaps]
        if cursor:
            previous = db.get(PortfolioAccountBaseline, cursor.baseline_id)
            if previous is None or previous.portfolio_id != portfolio_id:
                raise CoverageError("PREVIOUS_BASELINE_MISSING", "旧覆盖代次的基线引用不可核验，不能覆盖历史")
            gaps = list({_canonical(gap): gap for gap in json.loads(previous.history_gaps_json) + gaps}.values())
        frozen = {**preview.snapshot, "as_of_at": preview.as_of_at.isoformat(), "generation": generation,
            "evidence_fingerprint": preview.evidence_fingerprint, "review_note": request.review_note.strip(),
            "reviewer": operator_id, "calendar_version": preview.calendar_version, "policy_version": POLICY_VERSION}
        audit = write_audit_event(db, "DATA_BLOCK_RESOLUTION", portfolio_id=portfolio_id,
            business_key=baseline_id, operator_id=operator_id, correlation_id=request.acceptance_key, occurred_at=_utc(preview.as_of_at),
            before={"coverage": before}, after={"baseline_id": baseline_id, "generation": generation,
                "coverage_start_trade_date": request.coverage_start_trade_date.isoformat(), "verified_through_trade_date": None},
            attributes={"confirmation_type": "ACCOUNT_BASELINE_ACCEPTED", "review_decision": "approved", "execution_result": "not_executed",
                "actor_type": "human", "initiator_id": operator_id, "executor_id": operator_id,
                "source_timezone": "UTC", "bypass": False,
                "review_note": request.review_note.strip(), "ack": True, "snapshot_id": baseline_id,
                "evidence_fingerprint": preview.evidence_fingerprint, "history_gaps": gaps,
                "source_refs": preview.source_refs, "trading_resumed": False, "runtime_enabled": False},
            note="本人接受新的账户核验覆盖起点；旧期保持未知，新范围尚未核验任何交易日")
        baseline = PortfolioAccountBaseline(id=baseline_id, portfolio_id=portfolio_id, scope_key="account",
            kind="rebaseline" if cursor else "initial", as_of_at=_utc(preview.as_of_at),
            coverage_start_trade_date=request.coverage_start_trade_date, snapshot_json=_canonical(frozen),
            content_hash=_hash(frozen), source_refs_json=_canonical(preview.source_refs), history_gaps_json=_canonical(gaps),
            accepted_audit_id=audit.event_id, supersedes_id=cursor.baseline_id if cursor else None,
            acceptance_key=request.acceptance_key, request_hash=request_hash)
        db.add(baseline)
        db.flush()
        if cursor:
            cursor.generation = generation
            cursor.baseline_id = baseline_id
            cursor.coverage_start_trade_date = request.coverage_start_trade_date
            cursor.verified_through_trade_date = None
            cursor.calendar_version = preview.calendar_version
            cursor.policy_version = POLICY_VERSION
            cursor.row_version += 1
        else:
            cursor = PortfolioReconciliationCursor(id=cursor_id, portfolio_id=portfolio_id, scope_key="account",
                generation=generation, baseline_id=baseline_id, coverage_start_trade_date=request.coverage_start_trade_date,
                verified_through_trade_date=None, calendar_version=preview.calendar_version, policy_version=POLICY_VERSION, row_version=1)
            db.add(cursor)
        db.flush()
        from app.services.portfolio_reconciliation_events import acknowledge_baseline_sources
        acknowledge_baseline_sources(db, portfolio_id, baseline_id, generation)
        return _accepted(baseline, cursor)


def get_reconciliation_coverage(db: Session, portfolio_id: int) -> dict[str, Any]:
    with db.no_autoflush:
        cursor = _cursor(db, portfolio_id)
        baselines = list(db.scalars(select(PortfolioAccountBaseline).where(
            PortfolioAccountBaseline.portfolio_id == portfolio_id,
        ).order_by(PortfolioAccountBaseline.created_at.desc(), PortfolioAccountBaseline.id.desc())))
        return {"portfolio_id": portfolio_id, "scope_key": "account", "cursor": _cursor_snapshot(cursor),
            "baselines": [{"id": item.id, "kind": item.kind, "as_of_at": item.as_of_at.replace(tzinfo=timezone.utc),
                "coverage_start_trade_date": item.coverage_start_trade_date, "content_hash": item.content_hash,
                "history_gaps": json.loads(item.history_gaps_json), "accepted_audit_id": item.accepted_audit_id,
                "supersedes_id": item.supersedes_id} for item in baselines],
            "runtime_enabled": False, "trading_resumed": False,
            "historical_coverage_unknown": cursor is None or bool(baselines and json.loads(baselines[0].history_gaps_json))}


def get_active_coverage(db: Session, portfolio_id: int, *, current_read: bool = False) -> dict[str, Any] | None:
    """Return the actual accepted generation, never a legacy checkpoint guess."""
    with db.no_autoflush:
        cursor = _cursor(db, portfolio_id, lock=current_read)
        if cursor is None:
            return None
        baseline = db.get(PortfolioAccountBaseline, cursor.baseline_id, populate_existing=True)
        if baseline is None or baseline.portfolio_id != portfolio_id:
            raise CoverageError("BASELINE_SOURCE_MISSING", "当前核验代次的原始账户基线缺失，不能声明通过")
        try:
            snapshot = json.loads(baseline.snapshot_json)
        except (TypeError, ValueError) as exc:
            raise CoverageError("BASELINE_SOURCE_INVALID", "账户基线原始快照不可读取") from exc
        audit = db.get(DataGovernanceAuditEvent, baseline.accepted_audit_id)
        try:
            accepted = json.loads(audit.attributes_json or "{}") if audit else {}
        except (TypeError, ValueError):
            accepted = {}
        if (_hash(snapshot) != baseline.content_hash or snapshot.get("generation") != cursor.generation
                or not audit or audit.portfolio_id != portfolio_id
                or accepted.get("confirmation_type") != "ACCOUNT_BASELINE_ACCEPTED"
                or accepted.get("snapshot_id") != baseline.id or accepted.get("ack") is not True):
            raise CoverageError("BASELINE_SOURCE_INVALID", "账户基线指纹、代次或本人接受审计不匹配，需核查来源")
        return {"cursor_id": cursor.id, "generation": cursor.generation, "baseline_id": baseline.id,
            "coverage_start_trade_date": cursor.coverage_start_trade_date,
            "verified_through_trade_date": cursor.verified_through_trade_date,
            "row_version": cursor.row_version, "calendar_version": cursor.calendar_version,
            "policy_version": cursor.policy_version, "baseline_snapshot": snapshot,
            "baseline_as_of_at": baseline.as_of_at, "history_gaps": json.loads(baseline.history_gaps_json)}


def revalidate_active_coverage_sources(
    db: Session, portfolio_id: int, *, expected_baseline_id: str | None = None,
    current_read: bool = True,
) -> dict[str, Any]:
    """Recheck the accepted current-account snapshot before initial activation.

    Calendar/start selection is already frozen in the acceptance. This check
    can run the following day without replacing that start with today's date.
    It grants no runtime capability and writes no audit or account material.
    """
    with db.no_autoflush:
        statement = select(Portfolio).where(Portfolio.id == portfolio_id).execution_options(populate_existing=True)
        portfolio = db.scalar(statement.with_for_update() if current_read else statement)
        if not portfolio or portfolio.account_type != "simulated" or portfolio.currency != "CNY":
            raise CoverageError("ACCOUNT_SOURCE_UNSUPPORTED", "当前账户类型或币种已经不属于该已接受来源适配范围")
        active = get_active_coverage(db, portfolio_id, current_read=current_read)
        if active is None:
            raise CoverageError("ACCEPTED_COVERAGE_REQUIRED", "尚无本人接受的账户核验基线")
        if expected_baseline_id and active["baseline_id"] != expected_baseline_id:
            raise CoverageError("COVERAGE_GENERATION_CHANGED", "当前账户基线已变化，请重新审核启用范围")
        sources, references, blockers = _read_sources(db, portfolio_id, lock=current_read)
        if not blockers:
            try:
                _, blockers = _verify_account(sources, portfolio_id)
            except CoverageError as exc:
                blockers = [_block(exc.code, str(exc))]
        if blockers:
            raise CoverageError("BASELINE_NOT_ACCEPTABLE", "当前账户来源、资产或未决义务未通过重新核验",
                                details=[item.model_dump(mode="json") for item in blockers], status_code=428)
        accepted_sources = active["baseline_snapshot"].get("source_watermarks")
        if not isinstance(accepted_sources, list) or _hash(accepted_sources) != _hash(references):
            raise CoverageError("ACCOUNT_EVIDENCE_CHANGED", "本人接受后账户来源已变化，不能沿用旧快照启用新范围")
        return {"baseline_id": active["baseline_id"], "generation": active["generation"],
            "coverage_start_trade_date": active["coverage_start_trade_date"],
            "evidence_fingerprint": active["baseline_snapshot"].get("evidence_fingerprint"),
            "current_source_refs": references, "source_verified": True}


def _continuous_coverage(db: Session, cursor: PortfolioReconciliationCursor, *, through: date, lock: bool, calendar_source_version: str) -> dict[str, Any]:
    """Compute closure from current-generation reports and real calendar rows.

    Recompute from the accepted start so revised failed evidence cannot leave
    a previously certified date falsely visible as continuously verified.
    Older generations and the legacy portfolio checkpoint remain untouched.
    """
    start = cursor.coverage_start_trade_date
    if through < start:
        return {"verified_through_trade_date": None, "first_blocked_trade_date": start}
    calendar_table = Table("trade_calendar", MetaData(), autoload_with=db.connection())
    statement = select(calendar_table).where(calendar_table.c.date >= start, calendar_table.c.date <= through).order_by(calendar_table.c.date)
    calendar_rows = [dict(row) for row in db.execute(statement.with_for_update() if lock else statement).mappings()]
    calendar = {row["date"]: row["is_trading_day"] for row in calendar_rows}
    days = {row.target_trade_date: row for row in db.scalars(select(PortfolioReconciliationDay).where(
        PortfolioReconciliationDay.cursor_id == cursor.id,
        PortfolioReconciliationDay.generation == cursor.generation,
        PortfolioReconciliationDay.target_trade_date >= start,
        PortfolioReconciliationDay.target_trade_date <= through,
    ))}
    verified, blocked = None, None
    current = start
    while current <= through:
        if current not in calendar or calendar[current] not in {0, 1}:
            blocked = current
            break
        day = days.get(current)
        # A recorded cash/account exception on a closed market day remains
        # blocking; market closure never removes an observed account problem.
        if day and (day.verdict != "PASSED" or day.work_state != "done" or not day.current_report_id
                    or day.processed_evidence_version < day.requested_evidence_version):
            blocked = current
            break
        if calendar[current] == 1 and day is None:
            blocked = current
            break
        if day:
            report = db.get(PortfolioReconciliationReport, day.current_report_id)
            if report is None or report.day_id != day.id or report.baseline_id != cursor.baseline_id or report.verdict != "PASSED":
                blocked = current
                break
            expected_calendar_version = _hash({"source_version": calendar_source_version,
                "rows": [row for row in calendar_rows if row["date"] <= current]})
            if report.calendar_version != expected_calendar_version:
                # A later-date retry may discover that an earlier day's report
                # used a superseded calendar. Preserve it, but require a fresh
                # version before treating the prefix as continuously verified.
                blocked = current
                break
        if calendar[current] == 1:
            verified = current
        current += timedelta(days=1)
    return {"verified_through_trade_date": verified, "first_blocked_trade_date": blocked}


def record_coverage_reconciliation(
    db: Session, *, portfolio_id: int, trade_date: date, verdict: str,
    checks: dict[str, Any] | list[Any], findings: dict[str, Any] | list[Any],
    source_refs: list[dict[str, Any]], evidence_hash: str,
    closure_type: str | None = "RECONCILED", calendar_version: str | None = None,
    expected_generation: int | None = None, expected_cursor_row_version: int | None = None, operator_id: str = "system",
    expected_requested_evidence_version: int | None = None,
    correlation_id: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one verified worker/recheck result and its generation cursor.

    The caller supplies actual reconciliation evidence. This function owns all
    four-table writes, immutable report versions, atomic audit, and continuous
    calendar consumption. It never changes the portfolio's legacy checkpoint.
    """
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", evidence_hash):
        raise CoverageError("EVIDENCE_FINGERPRINT_REQUIRED", "核对报告必须带真实证据指纹")
    if verdict == "PASSED" and (not checks or not source_refs or findings):
        raise CoverageError("VERIFIED_REPORT_EVIDENCE_REQUIRED", "核对通过需要完整检查、真实来源且无未解决差异，不能保存空通过报告")
    if verdict == "PASSED" and any(ref.get("available") is False or ref.get("synthetic") is True for ref in source_refs):
        raise CoverageError("VERIFIED_REPORT_SOURCE_INVALID", "缺失或演示来源不能核验通过")
    def has_failed_check(value: Any) -> bool:
        if isinstance(value, dict):
            return any((key in {"passed", "verified"} and child is False) or has_failed_check(child) for key, child in value.items())
        return isinstance(value, list) and any(has_failed_check(child) for child in value)
    if verdict == "PASSED" and has_failed_check(checks):
        raise CoverageError("FAILED_CHECK_CANNOT_PASS", "检查明确未通过，不能保存为核验通过报告")
    as_of = _utc(now)
    local_today = as_of.replace(tzinfo=timezone.utc).astimezone(SHANGHAI).date()
    if verdict == "PASSED" and trade_date > local_today:
        raise CoverageError("RECONCILIATION_NOT_DUE", "未来交易日不能提前核验通过")
    with db.begin_nested():
        db.scalar(select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update())
        active = get_active_coverage(db, portfolio_id, current_read=True)
        if active is None:
            raise CoverageError("ACCEPTED_COVERAGE_REQUIRED", "尚无本人接受的账户核验基线，不能创建覆盖通过记录")
        cursor = _cursor(db, portfolio_id, lock=True)
        if expected_generation is not None and active["generation"] != expected_generation:
            raise CoverageError("COVERAGE_GENERATION_CHANGED", "核对任务所属代次已失效，请按新账户基线重新核查")
        if expected_cursor_row_version is not None and cursor.row_version != expected_cursor_row_version:
            raise CoverageError("COVERAGE_CURSOR_CHANGED", "来源核验后覆盖游标已变化，请重新核对后保存")
        if trade_date < active["coverage_start_trade_date"]:
            raise CoverageError("OUTSIDE_CURRENT_COVERAGE", "旧期间保持历史未知，不能用新基线报告覆盖旧日期")
        if cursor.lease_expires_at and _utc(cursor.lease_expires_at) > as_of and cursor.lease_owner not in {operator_id, correlation_id}:
            raise CoverageError("COVERAGE_LEASE_ACTIVE", "另一核对任务正在持有当前覆盖租约")
        if verdict == "PASSED" and closure_type not in {"RECONCILED", "NO_ACTIVITY_VERIFIED"}:
            raise CoverageError("UNSUPPORTED_CLOSURE_TYPE", "该处置类型需要独立审核策略，不能自动核验关闭")
        trusted_calendar = _trusted_calendar(db, as_of, lock=True)
        inspector = inspect(db.connection())
        if "trade_calendar" not in inspector.get_table_names():
            raise CoverageError("CALENDAR_SOURCE_MISSING", "当前核对缺少真实日历来源，不能推进覆盖")
        calendar_table = Table("trade_calendar", MetaData(), autoload_with=db.connection())
        calendar_rows = [dict(row) for row in db.execute(select(calendar_table).where(
            calendar_table.c.date >= cursor.coverage_start_trade_date, calendar_table.c.date <= trade_date,
        ).order_by(calendar_table.c.date).with_for_update()).mappings()]
        # Freeze the actual date rows used now. A caller's version label does
        # not replace the stored calendar evidence.
        observed_calendar_version = _hash({"source_version": trusted_calendar["version"], "rows": calendar_rows})
        if verdict == "PASSED" and any(row["is_trading_day"] not in {0, 1} for row in calendar_rows):
            raise CoverageError("CALENDAR_SOURCE_INVALID", "核对日历包含未知开休市状态，不能核验通过")
        if verdict == "PASSED" and len(calendar_rows) != (trade_date - cursor.coverage_start_trade_date).days + 1:
            raise CoverageError("CALENDAR_COVERAGE_INCOMPLETE", "核对区间日历存在缺失行，不能把未知日期跳过")
        day = db.scalar(select(PortfolioReconciliationDay).where(
            PortfolioReconciliationDay.cursor_id == cursor.id,
            PortfolioReconciliationDay.generation == cursor.generation,
            PortfolioReconciliationDay.target_trade_date == trade_date,
        ).with_for_update())
        before = _cursor_snapshot(cursor)
        previous_report = db.get(PortfolioReconciliationReport, day.current_report_id) if day and day.current_report_id else None
        if expected_requested_evidence_version is not None:
            if expected_requested_evidence_version < 1 or expected_requested_evidence_version > (day.requested_evidence_version if day else 1):
                raise CoverageError("COVERAGE_EVIDENCE_VERSION_INVALID", "核验需求版本不属于当前日期行，请重新派发")
            if day and expected_requested_evidence_version < day.processed_evidence_version:
                raise CoverageError("COVERAGE_EVIDENCE_VERSION_STALE", "该核验需求已被更新版本处理，不能覆盖新报告")
        has_new_demand = bool(day and day.requested_evidence_version > day.processed_evidence_version
            and (expected_requested_evidence_version is None or expected_requested_evidence_version > day.processed_evidence_version))
        if previous_report and all((
            previous_report.evidence_hash == evidence_hash, previous_report.verdict == verdict,
            previous_report.closure_type == closure_type, previous_report.checks_json == _canonical(checks),
            previous_report.findings_json == _canonical(findings), previous_report.source_refs_json == _canonical(source_refs),
            previous_report.calendar_version == observed_calendar_version,
            not has_new_demand,
        )):
            status = _continuous_coverage(db, cursor, through=max(trade_date, cursor.verified_through_trade_date or trade_date), lock=True,
                calendar_source_version=trusted_calendar["version"])
            if cursor.verified_through_trade_date != status["verified_through_trade_date"]:
                before_recalculation = _cursor_snapshot(cursor)
                cursor.verified_through_trade_date = status["verified_through_trade_date"]
                cursor.row_version += 1
                write_audit_event(db, "RECONCILIATION_RESULT", portfolio_id=portfolio_id,
                    business_key=previous_report.id, occurred_at=as_of, operator_id=operator_id,
                    correlation_id=correlation_id, before={"coverage": before_recalculation},
                    after=json.loads(_canonical({"coverage": _cursor_snapshot(cursor), **status})), attributes={
                        "actor_type": "system", "initiator_id": operator_id, "executor_id": operator_id,
                        "source_timezone": "UTC", "bypass": False,
                        "confirmation_type": "COVERAGE_CONTINUITY_RECALCULATED", "status": verdict,
                        "trade_date": trade_date.isoformat(), "generation": cursor.generation,
                        "baseline_id": cursor.baseline_id, "report_id": previous_report.id,
                        "report_version": previous_report.report_version, "evidence_hash": evidence_hash,
                        "calendar_version": observed_calendar_version,
                    }, note="保留原不可变报告，原子重算当前代连续核验游标")
                db.flush()
            return {"current_report_id": previous_report.id, "report_version": previous_report.report_version,
                "evidence_version": previous_report.evidence_version, "generation": cursor.generation,
                "requested_evidence_version": day.requested_evidence_version,
                "processed_evidence_version": day.processed_evidence_version,
                "pending_new_evidence": day.requested_evidence_version > day.processed_evidence_version,
                "baseline_id": cursor.baseline_id, "can_advance": verdict == "PASSED" and status["verified_through_trade_date"] is not None and status["verified_through_trade_date"] >= trade_date,
                **status, "cursor_row_version": cursor.row_version, "idempotent": True}
        if day is None:
            day = PortfolioReconciliationDay(id=f"recon-day-{uuid4().hex}", cursor_id=cursor.id,
                generation=cursor.generation, target_trade_date=trade_date, requested_evidence_version=1,
                processed_evidence_version=0, attempt_count=0, work_state="queued", verdict="EVIDENCE_INCOMPLETE")
            db.add(day); db.flush()
        version = previous_report.report_version + 1 if previous_report else 1
        evidence_version = (expected_requested_evidence_version if expected_requested_evidence_version is not None
                            else max(day.requested_evidence_version, day.processed_evidence_version + 1))
        report_id = f"recon-report-{uuid4().hex}"
        audit = write_audit_event(db, "RECONCILIATION_RESULT", portfolio_id=portfolio_id,
            business_key=report_id, operator_id=operator_id, correlation_id=correlation_id, occurred_at=as_of,
            before={"coverage": before}, after={"report_id": report_id, "verdict": verdict},
            attributes={"trade_date": trade_date.isoformat(), "status": verdict, "generation": cursor.generation,
                "actor_type": "system", "initiator_id": operator_id, "executor_id": operator_id,
                "source_timezone": "UTC", "bypass": False,
                "baseline_id": cursor.baseline_id, "report_id": report_id, "report_version": version,
                "evidence_version": evidence_version, "evidence_hash": evidence_hash,
                "closure_type": closure_type, "source_refs": source_refs, "findings": findings},
            note="按本人接受的账户基线核对当前代范围，旧历史未知结论保持不变")
        report = PortfolioReconciliationReport(id=report_id, day_id=day.id, report_version=version,
            evidence_version=evidence_version, baseline_id=cursor.baseline_id,
            checks_json=_canonical(checks), findings_json=_canonical(findings), source_refs_json=_canonical(source_refs),
            evidence_hash=evidence_hash, verdict=verdict, closure_type=closure_type,
            policy_version=cursor.policy_version, calendar_version=observed_calendar_version,
            audit_event_id=audit.event_id)
        db.add(report); db.flush()
        day.current_report_id = report_id
        day.requested_evidence_version = max(day.requested_evidence_version, evidence_version)
        day.processed_evidence_version = max(day.processed_evidence_version, evidence_version)
        if day.processed_evidence_version >= day.requested_evidence_version:
            day.account_source_pending = 0
        pending_new_evidence = day.requested_evidence_version > day.processed_evidence_version
        day.attempt_count += 1
        day.verdict = verdict
        day.closure_type = closure_type
        day.work_state = "queued" if pending_new_evidence else "done" if verdict == "PASSED" else "waiting_execution" if verdict in {"NOT_DUE", "EXECUTION_IN_PROGRESS"} else "needs_review"
        day.last_error_code = None if verdict == "PASSED" else verdict
        db.flush()
        max_day = max([trade_date, cursor.verified_through_trade_date or trade_date] + list(db.scalars(select(
            PortfolioReconciliationDay.target_trade_date,
        ).where(PortfolioReconciliationDay.cursor_id == cursor.id, PortfolioReconciliationDay.generation == cursor.generation,
                PortfolioReconciliationDay.target_trade_date <= local_today))))
        status = _continuous_coverage(db, cursor, through=min(max_day, local_today), lock=True,
            calendar_source_version=trusted_calendar["version"])
        cursor.verified_through_trade_date = status["verified_through_trade_date"]
        cursor.row_version += 1
        event = db.get(DataGovernanceAuditEvent, audit.event_id)
        event.after_json = _canonical({"report_id": report_id, "verdict": verdict, "coverage": _cursor_snapshot(cursor),
                                      "requested_evidence_version": day.requested_evidence_version,
                                      "processed_evidence_version": day.processed_evidence_version,
                                      "pending_new_evidence": pending_new_evidence,
                                      "first_blocked_trade_date": status["first_blocked_trade_date"]})
        db.flush()
        return {"current_report_id": report_id, "report_version": version, "evidence_version": evidence_version,
            "generation": cursor.generation, "baseline_id": cursor.baseline_id,
            "requested_evidence_version": day.requested_evidence_version, "processed_evidence_version": day.processed_evidence_version,
            "pending_new_evidence": pending_new_evidence,
            "can_advance": verdict == "PASSED" and status["verified_through_trade_date"] is not None and status["verified_through_trade_date"] >= trade_date,
            **status, "cursor_row_version": cursor.row_version, "idempotent": False}
