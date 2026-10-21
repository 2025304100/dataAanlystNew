"""Conservative, preview-first repair of an evidenced simulated cash book.

Only a complete initial deposit followed by one filled order / one trade / one
cash posting per execution is supported. Multi-fill, external cash movements,
unattributed history and quantity discrepancies require manual investigation.
This script never initializes schemas, starts workers or changes account state.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import struct
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import Float, MetaData, Table, create_engine, select, update
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.orm import Session, sessionmaker

from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.services.cash_ledger_diagnosis import diagnose_cash_ledger
from app.services.data_governance_audit import write_audit_event

CENT = Decimal("0.01")
VERSION = "sim-cash-ledger-repair-v1"


class RepairRefused(ValueError):
    """A safe reason code, without database URLs or raw configuration values."""


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RepairRefused("NON_FINITE_OR_MISSING_NUMBER") from None
    if not result.is_finite():
        raise RepairRefused("NON_FINITE_OR_MISSING_NUMBER")
    return result


def _text(value: Any) -> str:
    return format(_decimal(value).normalize(), "f")


def _cent(value: Any) -> Decimal:
    return _decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _float32(value: Decimal) -> Decimal:
    try:
        return _decimal(struct.unpack("!f", struct.pack("!f", float(value)))[0])
    except (OverflowError, struct.error):
        raise RepairRefused("UNSUPPORTED_FLOAT_RANGE") from None


def _matches_float(value: Any, expected: Decimal, storage_scale: int | None = None) -> bool:
    raw = _decimal(value)
    # This is an exact historical FLOAT representation check, not a monetary
    # epsilon: a missing cent/fee/posting cannot be waived by account size.
    binary32 = _float32(expected)
    if raw == expected or raw == binary32:
        return True
    # Some historical FLOAT values were subsequently stored in DECIMAL(p,10).
    # Accept only the exact FLOAT32 value rounded to the reflected DB scale.
    # Never infer a scale from the number or introduce a cash/relative epsilon.
    return (isinstance(storage_scale, int) and 8 <= storage_scale <= 30
        and raw == binary32.quantize(Decimal(1).scaleb(-storage_scale), rounding=ROUND_HALF_UP))


def _money(value: Any, storage_scale: int | None = None) -> Decimal:
    rounded = _cent(value)
    if not _matches_float(value, rounded, storage_scale):
        raise RepairRefused("UNRECOGNIZED_SUBCENT_AMOUNT")
    return rounded


def _same_quantity(left: Any, right: Any) -> bool:
    a, b = _decimal(left), _decimal(right)
    return a == b or a == _float32(b) or b == _float32(a)


def _price(value: Any, storage_scale: int | None = None) -> Decimal:
    for unit in (CENT, Decimal("0.001"), Decimal("0.0001")):
        rounded = _decimal(value).quantize(unit, rounding=ROUND_HALF_UP)
        if _matches_float(value, rounded, storage_scale):
            return rounded
    raise RepairRefused("UNSUPPORTED_PRICE_PRECISION")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(_jsonable(value), sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")


def _snapshot(db: Session, portfolio_id: int, *, lock: bool = False) -> dict:
    evidence = dict(portfolio=None, cash=[], orders=[], trades=[], positions=[], symbols=[], numeric_scales={})
    required = {
        "portfolio": {"id", "account_type", "total_capital"},
        "cash": {"id", "portfolio_id", "entry_type", "amount", "balance_after", "ref_type", "ref_id", "note"},
        "orders": {"id", "portfolio_id", "symbol_id", "side", "status", "quantity", "filled_quantity", "filled_price", "filled_amount", "fee"},
        "trades": {"id", "portfolio_id", "symbol_id", "order_id", "side", "quantity", "price", "amount", "fee"},
        "positions": {"id", "portfolio_id", "symbol_id", "quantity"},
        "symbols": {"id", "symbol"},
    }
    metadata = MetaData()
    with db.no_autoflush:
        tables = {}
        for name, model in (("portfolio", Portfolio), ("cash", CashLedger), ("orders", SimOrder),
                ("trades", SimTrade), ("positions", Position), ("symbols", Symbol)):
            try:
                table = Table(model.__tablename__, metadata, autoload_with=db.connection(), resolve_fks=False)
            except NoSuchTableError:
                evidence["schema_refusal"] = f"REQUIRED_EVIDENCE_TABLE_MISSING:{name}"
                return evidence
            if required[name] - set(table.c.keys()):
                evidence["schema_refusal"] = f"REQUIRED_EVIDENCE_COLUMN_MISSING:{name}"
                return evidence
            for column in table.c:
                if isinstance(column.type, Float):
                    # MySQL reflection defaults DOUBLE to asdecimal=True, which
                    # silently rounds DBAPI doubles to ten decimal places. Keep
                    # the driver's complete binary float evidence. This changes
                    # only the local read type, never the physical schema; real
                    # NUMERIC/DECIMAL columns retain their declared scale.
                    column.type.asdecimal = False
            tables[name] = table
            evidence["numeric_scales"][name] = {column.name: column.type.scale for column in table.c
                if isinstance(getattr(column.type, "scale", None), int)}
        for name in ("portfolio", "cash", "orders", "trades", "positions"):
            table = tables[name]
            column = table.c.id if name == "portfolio" else table.c.portfolio_id
            statement = select(table).where(column == portfolio_id).order_by(table.c.id)
            if lock:
                statement = statement.with_for_update()
            rows = [_jsonable(dict(row)) for row in db.execute(statement).mappings()]
            evidence[name] = (rows[0] if rows else None) if name == "portfolio" else rows
        symbol_ids = sorted({row["symbol_id"] for name in ("orders", "trades", "positions") for row in evidence[name]})
        table = tables["symbols"]
        statement = select(table).where(table.c.id.in_(symbol_ids)).order_by(table.c.id)
        if lock:
            statement = statement.with_for_update()
        evidence["symbols"] = [_jsonable(dict(row)) for row in db.execute(statement).mappings()]
    return evidence


def _validated_rows(evidence: dict) -> list[dict]:
    if evidence.get("schema_refusal"):
        raise RepairRefused(evidence["schema_refusal"])
    def scale(table: str, field: str) -> int | None:
        return evidence["numeric_scales"].get(table, {}).get(field)
    portfolio, cash = evidence["portfolio"], evidence["cash"]
    if not portfolio:
        raise RepairRefused("PORTFOLIO_NOT_FOUND")
    if portfolio["account_type"] != "simulated":
        raise RepairRefused("SIMULATED_ACCOUNTS_ONLY")
    if not cash:
        raise RepairRefused("COMPLETE_INITIAL_DEPOSIT_REQUIRED")
    first = cash[0]
    opening = _money(first["amount"], scale("cash", "amount"))
    if (first["entry_type"] != "deposit" or first["ref_type"] != "portfolio"
            or first["ref_id"] != portfolio["id"] or first["note"] != "Initial simulated funding"
            or opening <= 0 or _decimal(first["balance_after"]) != _decimal(first["amount"])
            or opening != _money(portfolio["total_capital"], scale("portfolio", "total_capital"))):
        raise RepairRefused("COMPLETE_INITIAL_DEPOSIT_REQUIRED")
    orders = {row["id"]: row for row in evidence["orders"]}
    symbols = {row["id"] for row in evidence["symbols"]}
    trades_by_order: dict[int, list] = {}
    postings_by_order: dict[int, list] = {}
    for trade in evidence["trades"]:
        if trade["order_id"] not in orders:
            raise RepairRefused("UNKNOWN_TRADE_ORDER")
        trades_by_order.setdefault(trade["order_id"], []).append(trade)
    for row in cash[1:]:
        if row["entry_type"] not in ("buy", "sell") or row["ref_type"] != "sim_order" or row["ref_id"] not in orders:
            raise RepairRefused("UNKNOWN_CASH_SOURCE")
        postings_by_order.setdefault(row["ref_id"], []).append(row)
    wanted = {}
    for oid, order in orders.items():
        if order["status"] != "filled" or order["side"] not in ("buy", "sell"):
            raise RepairRefused("ONLY_COMPLETE_FILLED_ORDERS_SUPPORTED")
        trades, postings = trades_by_order.get(oid, []), postings_by_order.get(oid, [])
        if len(trades) != 1 or len(postings) != 1:
            raise RepairRefused("ONE_TRADE_AND_CASH_POSTING_PER_ORDER_REQUIRED")
        trade, posting = trades[0], postings[0]
        if (order["symbol_id"] not in symbols or order["symbol_id"] != trade["symbol_id"]
                or order["side"] != trade["side"] or order["side"] != posting["entry_type"]):
            raise RepairRefused("EXECUTION_IDENTITY_MISMATCH")
        qty = _decimal(order["filled_quantity"])
        if qty <= 0 or not _same_quantity(order["quantity"], qty) or not _same_quantity(trade["quantity"], qty):
            raise RepairRefused("EXECUTION_QUANTITY_MISMATCH")
        gross, fee = _money(order["filled_amount"], scale("orders", "filled_amount")), _money(order["fee"], scale("orders", "fee"))
        if (gross <= 0 or fee < 0 or _money(trade["amount"], scale("trades", "amount")) != gross
                or _money(trade["fee"], scale("trades", "fee")) != fee
                or _price(order["filled_price"], scale("orders", "filled_price")) != _price(trade["price"], scale("trades", "price"))
                or _cent(_price(trade["price"], scale("trades", "price")) * qty) != gross):
            raise RepairRefused("EXECUTION_AMOUNT_PRICE_OR_FEE_MISMATCH")
        change = -(gross + fee) if order["side"] == "buy" else gross - fee
        if (order["side"] == "sell" and change < 0) or not _matches_float(posting["amount"], change, scale("cash", "amount")):
            raise RepairRefused("CASH_AMOUNT_NOT_EVIDENCED_FLOAT_ROUNDING")
        wanted[posting["id"]] = change
    quantities: dict[int, Decimal] = {}
    projected = []
    balance = Decimal(0)
    for row in cash:
        _money(row["balance_after"], scale("cash", "balance_after"))  # reject unexplained sub-cent representations
        amount = opening if row["id"] == first["id"] else wanted[row["id"]]
        balance += amount
        if balance < 0:
            raise RepairRefused("RECONSTRUCTED_CASH_WOULD_BE_NEGATIVE")
        projected.append({**row, "amount": _text(amount), "balance_after": _text(balance)})
        if row["id"] != first["id"]:
            order = orders[row["ref_id"]]
            sid = order["symbol_id"]
            qty = _decimal(order["filled_quantity"])
            quantities[sid] = quantities.get(sid, Decimal(0)) + (qty if order["side"] == "buy" else -qty)
            if quantities[sid] < 0:
                raise RepairRefused("EXECUTIONS_REQUIRE_UNEVIDENCED_OPENING_POSITION")
    positions = {}
    for position in evidence["positions"]:
        sid = position["symbol_id"]
        if sid in positions or sid not in quantities:
            raise RepairRefused("UNKNOWN_OR_DUPLICATE_POSITION")
        positions[sid] = _decimal(position["quantity"])
    if any(not _same_quantity(positions.get(sid, 0), qty) for sid, qty in quantities.items()):
        raise RepairRefused("POSITION_QUANTITY_NOT_CONSERVED")
    return projected


def _make_plan(evidence: dict, portfolio_id: int) -> dict:
    result = dict(version=VERSION, portfolio_id=portfolio_id, eligible=False, reasons=[],
        changes=[], cash_before=None, cash_after=None, cash_delta=None,
        diagnosis_before=None, diagnosis_projected=None, evidence=evidence)
    try:
        projected = _validated_rows(evidence)
        result["diagnosis_before"] = diagnose_cash_ledger(evidence["cash"], evidence["trades"])
        result["diagnosis_projected"] = diagnose_cash_ledger(projected, evidence["trades"])
        if result["diagnosis_projected"]["status"] != "CONSISTENT":
            raise RepairRefused("PROJECTED_READONLY_DIAGNOSIS_NOT_CONSISTENT")
        for before, after in zip(evidence["cash"], projected, strict=True):
            fields = [field for field in ("amount", "balance_after") if _decimal(before[field]) != _decimal(after[field])]
            if fields:
                result["changes"].append(dict(ledger_id=before["id"],
                    before={field: _text(before[field]) for field in fields},
                    after={field: after[field] for field in fields}))
        result.update(eligible=True, cash_before=_text(evidence["cash"][-1]["balance_after"]),
            cash_after=projected[-1]["balance_after"],
            cash_delta=_text(_decimal(projected[-1]["balance_after"]) - _decimal(evidence["cash"][-1]["balance_after"])))
    except RepairRefused as error:
        result["reasons"].append(str(error))
        result["changes"] = []
    result["plan_hash"] = hashlib.sha256(_canonical(result)).hexdigest()
    return result


def plan_repair(db: Session, portfolio_id: int) -> dict:
    """SELECT-only preview, including raw evidence and exact changed fields."""
    if db.new or db.dirty or db.deleted:
        raise RepairRefused("CLEAN_SESSION_REQUIRED")
    return _make_plan(_snapshot(db, portfolio_id), portfolio_id)


def apply_repair(db: Session, portfolio_id: int, *, expected_plan_hash: str,
        evidence_path: str | Path, operator_id: str) -> dict:
    """Own one transaction; lock, revalidate, back up, update cash, audit, verify.

    Use a fresh Session (or end the preview's read transaction first). A stale
    authorization is rejected even if a new plan would otherwise be eligible.
    Re-previewing a repaired book yields an independently confirmed no-op.
    """
    if db.in_transaction() or db.new or db.dirty or db.deleted:
        raise RepairRefused("FRESH_CLEAN_SESSION_REQUIRED")
    if (not isinstance(expected_plan_hash, str) or len(expected_plan_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_plan_hash)):
        raise RepairRefused("EXPLICIT_PREVIEW_HASH_REQUIRED")
    if not str(operator_id).strip():
        raise RepairRefused("EXPLICIT_OPERATOR_REQUIRED")
    with db.begin():
        if db.get_bind().dialect.name == "sqlite":
            # SQLite ignores FOR UPDATE; acquire its write reservation instead.
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        plan = _make_plan(_snapshot(db, portfolio_id, lock=True), portfolio_id)
        if not hmac.compare_digest(plan["plan_hash"], expected_plan_hash):
            raise RepairRefused("PREVIEW_HASH_CHANGED")
        if not plan["eligible"]:
            raise RepairRefused("REPAIR_REFUSED:" + ",".join(plan["reasons"]))
        if not plan["changes"]:
            return dict(applied=False, changed_rows=0, plan_hash=plan["plan_hash"],
                diagnosis_after=plan["diagnosis_before"], audit_event_id=None)
        path = Path(evidence_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _canonical(dict(recorded_at=datetime.now(timezone.utc).isoformat(), plan=plan))
        try:
            # Exclusive create prevents overwriting previous evidence. fsync must
            # succeed before the first UPDATE or audit INSERT is attempted.
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise RepairRefused("EVIDENCE_FILE_ALREADY_EXISTS") from None
        for change in plan["changes"]:
            db.execute(update(CashLedger).where(CashLedger.portfolio_id == portfolio_id,
                CashLedger.id == change["ledger_id"]).values(
                    **{field: float(Decimal(value)) for field, value in change["after"].items()}))
        db.flush()
        after = _make_plan(_snapshot(db, portfolio_id, lock=True), portfolio_id)
        diagnosis = after["diagnosis_before"]
        if not after["eligible"] or after["changes"] or not diagnosis or diagnosis["status"] != "CONSISTENT":
            raise RepairRefused("POST_APPLY_READONLY_DIAGNOSIS_NOT_CONSISTENT")
        audit = write_audit_event(db, "RECONCILIATION_RESULT", portfolio_id=portfolio_id,
            operator_id=operator_id, business_key=f"{VERSION}:{portfolio_id}",
            correlation_id=plan["plan_hash"],
            before={"cash": plan["cash_before"], "rows": [{"ledger_id": r["ledger_id"], **r["before"]} for r in plan["changes"]]},
            after={"cash": plan["cash_after"], "rows": [{"ledger_id": r["ledger_id"], **r["after"]} for r in plan["changes"]]},
            attributes={"maintenance_tool": VERSION, "approved_plan_hash": plan["plan_hash"],
                "evidence_path": str(path), "evidence_sha256": hashlib.sha256(payload).hexdigest(),
                "diagnosis_status": diagnosis["status"], "orders_trades_positions_unchanged": True},
            note="Rebuilt cent-level simulated cash chain from complete execution evidence. Recognized FLOAT amount tails; historical balance-break cause undetermined. Account state unchanged")
        return dict(applied=True, changed_rows=len(plan["changes"]), plan_hash=plan["plan_hash"],
            after_plan_hash=after["plan_hash"], cash_before=plan["cash_before"],
            cash_after=plan["cash_after"], cash_delta=plan["cash_delta"],
            evidence_path=str(path), evidence_sha256=hashlib.sha256(payload).hexdigest(),
            audit_event_id=audit.event_id, diagnosis_after=diagnosis)


def _configured_session_factory():
    # Reuse the application's active runtime binding when invoked in-process.
    from app.db.session import get_session_local
    try:
        return get_session_local()
    except RuntimeError:
        pass
    # Standalone: match initialize_runtime_database's precedence exactly, without
    # importing app.main, running init_db or disclosing the connection URL.
    from app.core.config import build_mysql_url, load_db_config, settings
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        url = explicit
    else:
        config = load_db_config()
        url = build_mysql_url(config) if config.get("use_mysql") and config.get("mysql", {}).get("host") else settings.database_url
    return sessionmaker(bind=create_engine(url, pool_pre_ping=True, echo=False),
        autoflush=False, expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portfolio-id", type=int, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Read-only preview (default)")
    mode.add_argument("--apply", action="store_true", help="Apply an explicitly approved preview hash")
    parser.add_argument("--plan-hash", help="Exact hash from a fresh preview")
    parser.add_argument("--evidence-file", help="New exclusive-create evidence JSON path, required for apply")
    parser.add_argument("--operator", help="Explicit audit operator, required for apply")
    parser.add_argument("--output", help="Optional NEW exclusive-create plan/result JSON file; raw source rows and connection settings are omitted")
    args = parser.parse_args(argv)
    if args.portfolio_id <= 0:
        parser.error("--portfolio-id must be positive")
    if args.apply and not (args.plan_hash and args.evidence_file and args.operator):
        parser.error("--apply requires --plan-hash, --evidence-file and --operator")
    if not args.apply and (args.plan_hash or args.evidence_file or args.operator):
        parser.error("--plan-hash, --evidence-file and --operator are apply-only options")
    output_stream = None
    committed_result = None
    try:
        if args.output and args.evidence_file:
            output_path = os.path.normcase(str(Path(args.output).expanduser().resolve()))
            evidence_path = os.path.normcase(str(Path(args.evidence_file).expanduser().resolve()))
            if output_path == evidence_path:
                raise RepairRefused("OUTPUT_EVIDENCE_PATH_COLLISION")
        if args.output:
            path = Path(args.output).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Reserve before any DB access. Existing results/evidence cannot
                # be overwritten, and a predictable output-path error cannot
                # occur after an otherwise successful apply transaction.
                output_stream = path.open("x", encoding="utf-8", newline="\n")
            except FileExistsError:
                raise RepairRefused("OUTPUT_FILE_ALREADY_EXISTS") from None
        factory = _configured_session_factory()
        with factory() as db:
            if args.apply:
                result = apply_repair(db, args.portfolio_id, expected_plan_hash=args.plan_hash,
                    evidence_path=args.evidence_file, operator_id=args.operator)
                if result["applied"]:
                    committed_result = result
            else:
                plan = plan_repair(db, args.portfolio_id)
                result = {key: value for key, value in plan.items() if key != "evidence"}
                result["evidence_counts"] = {key: len(value) for key, value in plan["evidence"].items() if isinstance(value, list)}
        encoded = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False)
        if output_stream:
            output_stream.write(encoded + "\n")
            output_stream.flush()
            os.fsync(output_stream.fileno())
        print(encoded)
        return 0 if result.get("eligible", True) else 2
    except RepairRefused as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
    except Exception as error:
        # Drivers commonly embed host/user/URL/SQL parameters in exception text.
        if committed_result is not None:
            print(json.dumps({"error": "REPAIR_COMMITTED_RESULT_OUTPUT_FAILED", "committed": True,
                "audit_event_id": committed_result["audit_event_id"],
                "evidence_path": committed_result["evidence_path"]}), file=sys.stderr)
            return 3
        code = "EVIDENCE_OR_OUTPUT_IO_FAILED" if isinstance(error, OSError) else "DATABASE_OR_EVIDENCE_OPERATION_FAILED"
        print(json.dumps({"error": code}), file=sys.stderr)
    finally:
        if output_stream:
            try:
                output_stream.close()
            except OSError:
                pass
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
