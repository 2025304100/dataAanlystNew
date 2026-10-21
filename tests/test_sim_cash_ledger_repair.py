"""Public maintenance plan/apply behavior, using only an isolated SQLite DB."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Float, MetaData, Numeric, Table, create_engine, event
from sqlalchemy.dialects.mysql import DOUBLE
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.portfolio import Portfolio, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from scripts.repair_sim_cash_ledger import RepairRefused, apply_repair, plan_repair

pytestmark = pytest.mark.whitebox


@pytest.fixture
def repair_db(request):
    engine = create_engine("sqlite:///:memory:")
    if getattr(request, "param", None) == "decimal-legacy":
        schema = MetaData()
        for table in Base.metadata.tables.values():
            table.to_metadata(schema)
        for name, fields in {"cash_ledger": ("amount", "balance_after"),
                "sim_orders": ("filled_amount",)}.items():
            for field in fields:
                schema.tables[name].c[field].type = Numeric(30, 10)
        schema.create_all(engine)
    else:
        Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(Portfolio(id=1, name="Repair isolated evidence", account_type="simulated",
            total_capital=500000, investable_ratio=.9, cash_reserve_ratio=.1))
        for sid in (1, 2, 3, 3276):
            db.add(Symbol(id=sid, symbol=f"FIX-{sid}", name=f"Fixture {sid}", market="sh", asset_type="stock"))
        db.flush()
        # Independent worked example transcribed from the supplied evidence.
        fills = [
            (1, 3, "buy", 10, 310.5, 3105, 0),
            (2, 3, "sell", 5, 311.2, 1556, 0),
            (3, 2, "buy", 10000, 4.79, 47900, 0),
            (4, 1, "buy", 1300, 10.95, 14235, 0),
            (8, 3276, "buy", 100, 1306.31, 130631, 169.82),
            (9, 3276, "buy", 100, 1306.31, 130631, 169.82),
            (10, 3276, "sell", 200, 1306.77, 261354, 601.11),
            (13, 3, "sell", 5, 309.07, 1545.35, 8.09),
            (14, 1, "buy", 100, 11.2, 1120, 6.12),
            (15, 1, "buy", 100, 11.2, 1120, 6.12),
        ]
        timestamp = datetime(2026, 10, 1, 9, 30)
        for oid, sid, side, qty, price, amount, fee in fills:
            db.add(SimOrder(id=oid, portfolio_id=1, symbol_id=sid, side=side, order_type="market",
                quantity=qty, submitted_price=price, status="filled", filled_quantity=qty,
                filled_price=price, filled_amount=1545.3499755859375 if oid == 13 else amount,
                fee=fee, created_at=timestamp, filled_at=timestamp))
            db.add(SimTrade(id=oid, portfolio_id=1, order_id=oid, symbol_id=sid, side=side,
                quantity=qty, price=price, amount=amount, fee=fee, created_at=timestamp))
        cash = [
            (1, "deposit", 500000, 500000, "portfolio", 1),
            (2, "buy", -3105, 496895, "sim_order", 1),
            (3, "sell", 1556, 498451, "sim_order", 2),
            (4, "buy", -47900, 450551, "sim_order", 3),
            (5, "buy", -14235, 436316, "sim_order", 4),
            (10, "buy", -130800.8203125, 305515.1875, "sim_order", 8),
            (13, "buy", -130800.8203125, 174714.1875, "sim_order", 9),
            (17, "sell", 260752.890625, 435466.875, "sim_order", 10),
            (20, "sell", 1537.260009765625, 437004.25, "sim_order", 13),
            (21, "buy", -1126.12, 435878.13, "sim_order", 14),
            (22, "buy", -1126.12, 434752.01, "sim_order", 15),
        ]
        for lid, kind, amount, balance, ref_type, ref_id in cash:
            db.add(CashLedger(id=lid, portfolio_id=1, entry_type=kind, amount=amount,
                balance_after=balance, ref_type=ref_type, ref_id=ref_id, created_at=timestamp,
                note="Initial simulated funding" if lid == 1 else "Historical fixture"))
        db.add_all([Position(id=2, portfolio_id=1, symbol_id=2, quantity=10000, asset_type="stock"),
            Position(id=3, portfolio_id=1, symbol_id=1, quantity=1500, asset_type="stock")])
        db.commit()
        yield db
    engine.dispose()


def test_preview_proposes_only_evidenced_cash_changes_without_dml(repair_db):
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(repair_db.get_bind(), "before_cursor_execute", capture)
    try:
        plan = plan_repair(repair_db, 1)
        second = plan_repair(repair_db, 1)
    finally:
        event.remove(repair_db.get_bind(), "before_cursor_execute", capture)
    assert plan["eligible"] is True and plan["reasons"] == []
    assert plan["cash_before"] == "434752.01"
    assert plan["cash_after"] == "434752.27"
    assert plan["cash_delta"] == "0.26"
    assert [r["ledger_id"] for r in plan["changes"] if "amount" in r["after"]] == [10, 13, 17, 20]
    assert [r["ledger_id"] for r in plan["changes"] if "balance_after" in r["after"]] == [10, 13, 17, 20, 21, 22]
    assert plan["diagnosis_before"]["status"] == "BROKEN"
    assert plan["diagnosis_projected"]["status"] == "CONSISTENT"
    assert second["plan_hash"] == plan["plan_hash"]
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"}


def test_apply_backs_up_evidence_changes_only_cash_and_becomes_consistent(repair_db, tmp_path):
    before = plan_repair(repair_db, 1)
    repair_db.rollback()  # apply owns a new, clean transaction
    backup = tmp_path / "original-evidence.json"
    applied = apply_repair(repair_db, 1, expected_plan_hash=before["plan_hash"],
        evidence_path=backup, operator_id="isolated-test")
    after = plan_repair(repair_db, 1)
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert applied["applied"] is True and applied["changed_rows"] == 6
    assert applied["diagnosis_after"]["status"] == "CONSISTENT"
    assert applied["audit_event_id"] > 0
    assert saved["plan"]["plan_hash"] == before["plan_hash"]
    assert saved["plan"]["evidence"] == before["evidence"]
    assert after["cash_after"] == "434752.27" and after["changes"] == []
    for key in ("orders", "trades", "positions", "portfolio", "symbols"):
        assert after["evidence"][key] == before["evidence"][key]
    repair_db.rollback()
    noop = apply_repair(repair_db, 1, expected_plan_hash=after["plan_hash"],
        evidence_path=tmp_path / "unused-noop.json", operator_id="isolated-test")
    assert noop["applied"] is False and noop["changed_rows"] == 0
    assert not (tmp_path / "unused-noop.json").exists()


@pytest.mark.parametrize("fault,reason", [
    ("missing_trade", "ONE_TRADE_AND_CASH_POSTING_PER_ORDER_REQUIRED"),
    ("duplicate_trade", "ONE_TRADE_AND_CASH_POSTING_PER_ORDER_REQUIRED"),
    ("missing_posting", "ONE_TRADE_AND_CASH_POSTING_PER_ORDER_REQUIRED"),
    ("duplicate_posting", "ONE_TRADE_AND_CASH_POSTING_PER_ORDER_REQUIRED"),
    ("missing_fee", "EXECUTION_AMOUNT_PRICE_OR_FEE_MISMATCH"),
    ("wrong_symbol", "EXECUTION_IDENTITY_MISMATCH"),
    ("wrong_side", "EXECUTION_IDENTITY_MISMATCH"),
    ("wrong_quantity", "EXECUTION_QUANTITY_MISMATCH"),
    ("missing_cent", "CASH_AMOUNT_NOT_EVIDENCED_FLOAT_ROUNDING"),
    ("unknown_cash", "UNKNOWN_CASH_SOURCE"),
    ("position_gap", "POSITION_QUANTITY_NOT_CONSERVED"),
    ("incomplete_opening", "COMPLETE_INITIAL_DEPOSIT_REQUIRED"),
    ("manual_account", "SIMULATED_ACCOUNTS_ONLY"),
])
def test_incomplete_or_ambiguous_history_refuses_apply_without_writes(repair_db, tmp_path, fault, reason):
    if fault == "missing_trade": repair_db.delete(repair_db.get(SimTrade, 15))
    elif fault == "duplicate_trade":
        repair_db.add(SimTrade(portfolio_id=1, order_id=15, symbol_id=1, side="buy",
            quantity=100, price=11.2, amount=1120, fee=6.12))
    elif fault == "missing_posting": repair_db.delete(repair_db.get(CashLedger, 22))
    elif fault == "duplicate_posting":
        repair_db.add(CashLedger(portfolio_id=1, entry_type="buy", amount=-1126.12,
            balance_after=433625.89, ref_type="sim_order", ref_id=15))
    elif fault == "missing_fee": repair_db.get(SimTrade, 15).fee = 0
    elif fault == "wrong_symbol": repair_db.get(SimTrade, 15).symbol_id = 3
    elif fault == "wrong_side": repair_db.get(SimTrade, 15).side = "sell"
    elif fault == "wrong_quantity": repair_db.get(SimTrade, 15).quantity = 99
    elif fault == "missing_cent": repair_db.get(CashLedger, 22).amount = -1126.11
    elif fault == "unknown_cash": repair_db.get(CashLedger, 22).ref_type = "manual"
    elif fault == "position_gap": repair_db.get(Position, 3).quantity = 1499
    elif fault == "incomplete_opening": repair_db.get(CashLedger, 1).balance_after = 499999
    elif fault == "manual_account": repair_db.get(Portfolio, 1).account_type = "manual"
    repair_db.commit()
    plan = plan_repair(repair_db, 1)
    assert plan["eligible"] is False and reason in plan["reasons"]
    repair_db.rollback()
    backup = tmp_path / "must-not-exist.json"
    with pytest.raises(RepairRefused, match=reason):
        apply_repair(repair_db, 1, expected_plan_hash=plan["plan_hash"],
            evidence_path=backup, operator_id="isolated-test")
    assert not backup.exists()
    assert plan_repair(repair_db, 1)["evidence"] == plan["evidence"]


def test_changed_source_hash_cannot_reuse_preview_authorization(repair_db, tmp_path):
    approved = plan_repair(repair_db, 1)
    repair_db.get(SimOrder, 15).note = "Changed after preview"
    repair_db.commit()
    with pytest.raises(RepairRefused, match="PREVIEW_HASH_CHANGED"):
        apply_repair(repair_db, 1, expected_plan_hash=approved["plan_hash"],
            evidence_path=tmp_path / "stale.json", operator_id="isolated-test")
    assert not (tmp_path / "stale.json").exists()
    assert plan_repair(repair_db, 1)["cash_before"] == "434752.01"


def test_unwritable_evidence_prevents_all_cash_updates(repair_db, tmp_path):
    before = plan_repair(repair_db, 1)
    repair_db.rollback()
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("Original unrelated file", encoding="utf-8")
    with pytest.raises(OSError):
        apply_repair(repair_db, 1, expected_plan_hash=before["plan_hash"],
            evidence_path=blocker / "backup.json", operator_id="isolated-test")
    assert plan_repair(repair_db, 1)["evidence"] == before["evidence"]


def test_audit_storage_failure_rolls_back_cash_updates(repair_db, tmp_path):
    before = plan_repair(repair_db, 1)
    repair_db.rollback()
    def reject_audit(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO DATA_GOVERNANCE_AUDIT_EVENTS"):
            raise RuntimeError("Isolated database audit-write fault")
    event.listen(repair_db.get_bind(), "before_cursor_execute", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="audit-write fault"):
            apply_repair(repair_db, 1, expected_plan_hash=before["plan_hash"],
                evidence_path=tmp_path / "rollback-evidence.json", operator_id="isolated-test")
    finally:
        event.remove(repair_db.get_bind(), "before_cursor_execute", reject_audit)
    assert (tmp_path / "rollback-evidence.json").is_file()
    assert plan_repair(repair_db, 1)["evidence"] == before["evidence"]


def test_cli_requires_apply_hash_and_backup_before_resolving_any_database():
    script = Path(__file__).resolve().parents[1] / "scripts" / "repair_sim_cash_ledger.py"
    env = {**os.environ, "DATABASE_URL": "invalid-private-url-must-not-be-printed"}
    result = subprocess.run([sys.executable, str(script), "--portfolio-id", "1", "--apply"],
        capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert "--plan-hash" in result.stderr and "--evidence-file" in result.stderr
    assert "invalid-private-url" not in result.stdout + result.stderr


def test_reflection_preserves_legacy_columns_and_does_not_require_new_optional_orm_columns(repair_db, tmp_path):
    repair_db.connection().exec_driver_sql("ALTER TABLE sim_orders DROP COLUMN limit_price")
    repair_db.connection().exec_driver_sql("ALTER TABLE positions ADD COLUMN legacy_target_weight FLOAT")
    repair_db.connection().exec_driver_sql("UPDATE positions SET legacy_target_weight = 0.25")
    repair_db.commit()
    plan = plan_repair(repair_db, 1)
    assert plan["eligible"] and plan["cash_delta"] == "0.26"
    assert "limit_price" not in plan["evidence"]["orders"][0]
    assert all(row["legacy_target_weight"] == .25 for row in plan["evidence"]["positions"])
    repair_db.rollback()
    backup = tmp_path / "legacy-evidence.json"
    applied = apply_repair(repair_db, 1, expected_plan_hash=plan["plan_hash"],
        evidence_path=backup, operator_id="isolated-test")
    assert applied["diagnosis_after"]["status"] == "CONSISTENT"
    assert json.loads(backup.read_text())["plan"]["evidence"]["positions"] == plan["evidence"]["positions"]


def test_missing_required_legacy_column_is_a_safe_refusal(repair_db):
    repair_db.connection().exec_driver_sql("ALTER TABLE sim_orders DROP COLUMN filled_price")
    repair_db.commit()
    plan = plan_repair(repair_db, 1)
    assert plan["eligible"] is False
    assert plan["reasons"] == ["REQUIRED_EVIDENCE_COLUMN_MISSING:orders"]


def test_cli_default_preview_honors_explicit_isolated_url_and_leaves_database_bytes_unchanged(repair_db, tmp_path):
    database = tmp_path / "cli-isolated.sqlite"
    with sqlite3.connect(database) as target:
        source = repair_db.get_bind().raw_connection()
        try:
            source.driver_connection.backup(target)
        finally:
            source.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    script = Path(__file__).resolve().parents[1] / "scripts" / "repair_sim_cash_ledger.py"
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + database.as_posix()}
    result = subprocess.run([sys.executable, str(script), "--portfolio-id", "1"],
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["eligible"] and plan["cash_after"] == "434752.27"
    assert "evidence" not in plan and plan["evidence_counts"]["orders"] == 10
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert database.as_posix() not in result.stdout + result.stderr


@pytest.mark.parametrize("repair_db", ["decimal-legacy"], indirect=True)
def test_known_float_tails_rounded_to_real_decimal_storage_scale_remain_evidenced(repair_db, tmp_path):
    plan = plan_repair(repair_db, 1)
    assert plan["eligible"] is True, plan["reasons"]
    assert plan["cash_after"] == "434752.27" and plan["cash_delta"] == "0.26"
    assert plan["evidence"]["cash"][8]["amount"] == "1537.2600097656"
    assert plan["evidence"]["orders"][7]["filled_amount"] == "1545.3499755859"
    repair_db.rollback()
    result = apply_repair(repair_db, 1, expected_plan_hash=plan["plan_hash"],
        evidence_path=tmp_path / "decimal-original.json", operator_id="isolated-test")
    assert result["diagnosis_after"]["status"] == "CONSISTENT"


@pytest.mark.parametrize("case_variant", [False, True])
def test_cli_refuses_result_path_aliasing_evidence_before_any_database_access(tmp_path, case_variant):
    if case_variant and os.name != "nt": pytest.skip("Windows case-insensitive path contract")
    script = Path(__file__).resolve().parents[1] / "scripts" / "repair_sim_cash_ledger.py"
    evidence = tmp_path / "ProtectedEvidence.json"
    output = str(evidence).swapcase() if case_variant else str(tmp_path / "unused" / ".." / evidence.name)
    env = {**os.environ, "DATABASE_URL": "invalid-private-url-must-not-be-printed"}
    result = subprocess.run([sys.executable, str(script), "--portfolio-id", "1", "--apply",
        "--plan-hash", "0" * 64, "--operator", "isolated-test", "--evidence-file", str(evidence),
        "--output", output], capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert "OUTPUT_EVIDENCE_PATH_COLLISION" in result.stderr
    assert "DATABASE_OR_EVIDENCE_OPERATION_FAILED" not in result.stderr
    assert "invalid-private-url" not in result.stdout + result.stderr
    assert not evidence.exists()


def test_cli_existing_output_is_preserved_and_apply_never_starts(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "repair_sim_cash_ledger.py"
    output = tmp_path / "existing-result.json"
    output.write_text("Protected previous evidence", encoding="utf-8")
    evidence = tmp_path / "not-created.json"
    env = {**os.environ, "DATABASE_URL": "invalid-private-url-must-not-be-printed"}
    result = subprocess.run([sys.executable, str(script), "--portfolio-id", "1", "--apply",
        "--plan-hash", "0" * 64, "--operator", "isolated-test", "--evidence-file", str(evidence),
        "--output", str(output)], capture_output=True, text=True, env=env)
    assert result.returncode == 2
    assert "OUTPUT_FILE_ALREADY_EXISTS" in result.stderr
    assert "DATABASE_OR_EVIDENCE_OPERATION_FAILED" not in result.stderr
    assert output.read_text(encoding="utf-8") == "Protected previous evidence"
    assert not evidence.exists()


def test_mysql_float_reflection_retains_driver_precision_without_decimal_truncation(repair_db):
    # Schema/driver boundary: reproduce MySQL reflection's DOUBLE(asdecimal=True)
    # on the isolated SQLite data, including its 10-place result conversion.
    def mysql_reflection(inspector, table, column_info):
        if isinstance(column_info["type"], Float):
            column_info["type"] = DOUBLE(asdecimal=True)
    event.listen(Table, "column_reflect", mysql_reflection)
    try:
        plan = plan_repair(repair_db, 1)
    finally:
        event.remove(Table, "column_reflect", mysql_reflection)
    assert plan["eligible"] is True, plan["reasons"]
    assert plan["evidence"]["cash"][8]["amount"] == 1537.260009765625
    assert plan["evidence"]["orders"][7]["filled_amount"] == 1545.3499755859375
    assert plan["cash_delta"] == "0.26"
