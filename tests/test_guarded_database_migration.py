"""H7 public CLI boundary: explicit targets, no fallback to the app config."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.init_db import _refresh_model_metadata
from app.models.portfolio import Portfolio
from app.models.portfolio_member import PortfolioMember
from app.models.symbol import Symbol

pytestmark = pytest.mark.whitebox

ROOT = Path(__file__).resolve().parents[1]


def cli_environment(**changes):
    return dict(os.environ, PYTHONIOENCODING="utf-8", **changes)


@pytest.fixture(scope="module")
def mysql_engine():
    raw = os.environ.get("MYSQL57_TEST_URL")
    if not raw:
        pytest.skip("MYSQL57_TEST_URL is required for the isolated MySQL guard lane")
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or not re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", url.database or ""):
        pytest.fail("MYSQL57_TEST_URL must target a dedicated codex_combination_* MySQL database")
    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as conn:
        assert not inspect(conn).get_table_names(), "Supply a fresh empty guard database, separate from order/migration lanes"
    _refresh_model_metadata()
    metadata = MetaData()
    for name in ("portfolios", "symbols", "portfolio_members"):
        table = Base.metadata.tables[name].to_metadata(metadata)
        table.dialect_options["mysql"]["engine"] = "InnoDB"
        table.dialect_options["mysql"]["charset"] = "utf8mb4"
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def seed_member_identity(engine):
    with Session(engine) as db:
        token = uuid.uuid4().hex[:10]
        portfolio = Portfolio(name=f"H7-{token}", account_type="manual", total_capital=0,
                              investable_ratio=1, cash_reserve_ratio=.05)
        symbol = Symbol(symbol=f"H{token}", name="H7 diagnostic", asset_type="stock", market="cn")
        db.add_all([portfolio, symbol]); db.commit()
        return portfolio.id, symbol.id


def test_missing_explicit_targets_fails_closed_without_url_fallback():
    environment = cli_environment()
    environment.pop("APP_DATABASE_URL", None)
    environment.pop("ALEMBIC_DATABASE_URL", None)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/guarded_database_migration.py"),
                             "check", "--expected-database", "codex_combination_missing",
                             "--expected-revision", "base", "--expected-server-version", "5.7.26"],
                            env=environment, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_code"] == "EXPLICIT_URL_REQUIRED"


def test_h7_actual_identity_check_and_mismatched_target_are_fail_closed(mysql_engine):
    url = mysql_engine.url
    environment = cli_environment(APP_DATABASE_URL=url.render_as_string(hide_password=False),
                       ALEMBIC_DATABASE_URL=url.render_as_string(hide_password=False))
    command = [sys.executable, str(ROOT / "scripts/guarded_database_migration.py"), "check",
               "--expected-database", url.database, "--expected-revision", "base",
               "--expected-server-version", "5.7.26"]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["app"]["database"] == report["alembic"]["database"] == url.database
    assert report["app"]["server_uuid"] == report["alembic"]["server_uuid"]
    for flag, invalid, code in (("--expected-server-version", "0.0", "SERVER_VERSION_MISMATCH"),
                                ("--expected-revision", "wrong_revision", "REVISION_MISMATCH"),
                                ("--expected-database", "gpfx", "ISOLATED_DATABASE_REQUIRED")):
        invalid_command = list(command)
        invalid_command[invalid_command.index(flag) + 1] = invalid
        rejected = subprocess.run(invalid_command, env=environment, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        assert rejected.returncode == 2
        assert json.loads(rejected.stdout)["error_code"] == code
    environment["ALEMBIC_DATABASE_URL"] = url.set(database="gpfx").render_as_string(hide_password=False)
    result = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_code"] == "TARGET_DATABASE_MISMATCH"
    assert (url.password or "not-a-secret") not in result.stdout + result.stderr


def test_h7_explicit_noop_upgrade_uses_verified_connection(mysql_engine):
    url = mysql_engine.url
    environment = cli_environment(APP_DATABASE_URL=url.render_as_string(hide_password=False),
                       ALEMBIC_DATABASE_URL=url.render_as_string(hide_password=False))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/guarded_database_migration.py"),
                             "upgrade", "--expected-database", url.database,
                             "--expected-revision", "base", "--expected-server-version", "5.7.26",
                             "--target-revision", "base"], env=environment,
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["after"]["database"] == url.database
    assert report["after"]["server_uuid"] == report["app"]["server_uuid"]
    assert report["after"]["revisions"] == ["base"]


@pytest.mark.parametrize("mode, mode_code", [(" AUTO ", "EXECUTION_MODE_NONCANONICAL"), (" ", "EXECUTION_MODE_INVALID")])
def test_historical_dirty_members_are_reported_without_inventing_archive_times(mysql_engine, mode, mode_code):
    portfolio_id, symbol_id = seed_member_identity(mysql_engine)
    with Session(mysql_engine) as db:
        row = PortfolioMember(portfolio_id=portfolio_id, symbol_id=symbol_id, status="archived", execution_mode=mode)
        db.add(row); db.commit()
        member_id = row.id
        before = (row.status, row.execution_mode, row.effective_from, row.effective_to)
    url = mysql_engine.url
    environment = cli_environment(APP_DATABASE_URL=url.render_as_string(hide_password=False),
                       ALEMBIC_DATABASE_URL=url.render_as_string(hide_password=False))
    try:
        result = subprocess.run([sys.executable, str(ROOT / "scripts/guarded_database_migration.py"),
                                 "check", "--expected-database", url.database,
                                 "--expected-revision", "base", "--expected-server-version", "5.7.26"],
                                env=environment, capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        assert result.returncode == 2, result.stdout
        report = json.loads(result.stdout)
        issues = [issue for issue in report["member_diagnostics"]["blockers"] if issue.get("member_id") == member_id]
        assert {issue["code"] for issue in issues} >= {mode_code, "ARCHIVE_TIME_UNPROVEN"}
        mode_issue = next(issue for issue in issues if issue["code"] == mode_code)
        if mode_code == "EXECUTION_MODE_NONCANONICAL":
            assert mode_issue["suggested_value"] == "auto"
        else:
            assert "suggested_value" not in mode_issue
        assert "suggested_value" not in next(issue for issue in issues if issue["code"] == "ARCHIVE_TIME_UNPROVEN")
        with Session(mysql_engine) as db:
            row = db.get(PortfolioMember, member_id)
            assert (row.status, row.execution_mode, row.effective_from, row.effective_to) == before
    finally:
        # Restore only the known synthetic fixture; the diagnostic itself is read-only.
        with Session(mysql_engine) as db:
            row = db.get(PortfolioMember, member_id)
            row.status, row.execution_mode = "active", "manual"
            db.commit()


def test_h7_fresh_sibling_upgrades_base_to_exact_0083(mysql_engine):
    base_url = mysql_engine.url
    name = base_url.database[:44] + "_h7_" + uuid.uuid4().hex[:8]
    assert re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", name) and len(name) <= 64
    admin = create_engine(base_url.set(database=""), hide_parameters=True)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    admin.dispose()
    url = base_url.set(database=name)
    environment = cli_environment(APP_DATABASE_URL=url.render_as_string(hide_password=False),
                       ALEMBIC_DATABASE_URL=url.render_as_string(hide_password=False))
    target = "wps_0024_083_combination_contracts"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/guarded_database_migration.py"),
                             "upgrade", "--expected-database", name, "--expected-revision", "base",
                             "--expected-server-version", "5.7.26", "--target-revision", target],
                            env=environment, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["after"]["database"] == name
    assert report["after"]["server_uuid"] == report["app"]["server_uuid"]
    assert report["after"]["revisions"] == [target]
    assert all(engine == "InnoDB" for engine in report["after"]["table_engines"].values())
    verified = create_engine(url, hide_parameters=True)
    try:
        with verified.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == target
            index = next(index for index in inspect(conn).get_indexes("portfolio_members") if index["name"] == "idx_portfolio_members_active")
            assert index["unique"] and index["column_names"] == ["portfolio_id", "symbol_id", "active_member_slot"]
        with mysql_engine.connect() as conn:
            assert list(conn.scalars(text("SELECT version_num FROM alembic_version"))) == []
    finally:
        verified.dispose()
    print("retained_guard_upgrade_database", name)
