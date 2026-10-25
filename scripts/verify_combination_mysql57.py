"""Required real-MySQL combination acceptance, always using fresh isolated schemas."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SUITES = {
    "migrations": "tests/test_combination_migration_mysql57.py",
    "orders": "tests/test_combination_orders_mysql57.py",
    "orders_e2e": "tests/test_combination_orders_e2e_mysql57.py",
    "guard": "tests/test_guarded_database_migration.py",
    "worker": "tests/test_factor_pipeline_worker_mysql57.py",
    "history": "tests/test_security_status_recovery_mysql57.py",
    "data": "tests/test_combination_data_mysql57.py",
    "backup": "tests/test_combination_restore_mysql57.py",
    "dates": "tests/test_combination_legacy_dates_mysql57.py",
    "auto_http": "tests/test_combination_auto_http_mysql57.py",
    "g4": "tests/test_g4_api_governance.py",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, help="Fresh codex_combination_* prefix; never reused")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--suite", choices=list(SUITES), action="append")
    args = parser.parse_args()
    if not re.fullmatch(r"codex_combination_[a-z0-9][a-z0-9_]{0,23}", args.prefix):
        parser.error("prefix must be codex_combination_ followed by 1..24 lowercase alphanumeric/underscore characters")
    raw = os.environ.get("COMBINATION_MYSQL57_URL")
    if not raw:
        parser.error("COMBINATION_MYSQL57_URL must be explicit; no application config fallback")
    url = make_url(raw)
    if url.get_backend_name() != "mysql":
        parser.error("Only MySQL is supported")
    if url.database and not url.database.startswith("codex_combination_"):
        parser.error("URL must have no database or an explicit codex_combination_* database")
    suites = args.suite or list(SUITES)
    names = {name: f"{args.prefix}_{name}" for name in suites}
    if "history" in suites:
        names["nontrans"] = f"{args.prefix}_nontrans"
    if "backup" in suites:
        names["restore"] = f"{args.prefix}_restore"
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    server = create_engine(url._replace(database=None), hide_parameters=True)
    try:
        with server.connect() as conn:
            identity = dict(conn.execute(text("SELECT VERSION() AS version, @@server_uuid AS server_uuid, "
                "@@hostname AS hostname, @@port AS port, @@sql_mode AS sql_mode, "
                "@@tx_isolation AS isolation_level, @@default_storage_engine AS default_engine, @@basedir AS basedir")).mappings().one())
            if not identity["version"].startswith("5.7."):
                raise RuntimeError("Acceptance specifically requires MySQL 5.7")
            existing = set(conn.execute(text("SELECT schema_name FROM information_schema.schemata")).scalars())
            if existing.intersection(names.values()):
                raise RuntimeError("Dedicated schema already exists; use a fresh prefix. Existing evidence will not be cleared.")
            for database in names.values():
                conn.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        (args.evidence_dir/"database-identity.json").write_text(json.dumps({**identity, "schemas": names}, indent=2), encoding="utf-8")
        outcomes = {}
        for suite in suites:
            log_path = args.evidence_dir/f"{suite}.log"
            junit = args.evidence_dir/f"{suite}.xml"
            env = dict(os.environ, MYSQL57_TEST_URL=url.set(database=names[suite]).render_as_string(hide_password=False),
                       COMBINATION_EVIDENCE_DIR=str(args.evidence_dir.resolve()),
                       DATABASE_URL="sqlite:///:memory:",
                       AUTO_UNIVERSE_SYNC_ON_STARTUP="0", AUTO_TRADE_CALENDAR_REFRESH="0")
            if suite == "history":
                env["MYSQL57_NONTRANSACTIONAL_TEST_URL"] = url.set(database=names["nontrans"]).render_as_string(hide_password=False)
            if suite == "worker":
                env["MYSQL57_FACTOR_TEST_URL"] = env["MYSQL57_TEST_URL"]
            if suite == "auto_http":
                env["COMBINATION_AUTO_E2E_URL"] = env["MYSQL57_TEST_URL"]
                env["COMBINATION_AUTO_E2E_EVIDENCE"] = str((args.evidence_dir/"auto-http").resolve())
            if suite == "g4":
                env["G4_MYSQL57_URL"] = env["MYSQL57_TEST_URL"]
            if suite == "backup":
                env["MYSQL57_RESTORE_TEST_URL"] = url.set(database=names["restore"]).render_as_string(hide_password=False)
                for variable, binary in (("MYSQLDUMP_BIN", "mysqldump"), ("MYSQL_BIN", "mysql")):
                    candidate = Path(identity["basedir"])/"bin"/(binary + (".exe" if os.name == "nt" else ""))
                    if variable not in env:
                        found = shutil.which(binary) or (str(candidate) if candidate.is_file() else None)
                        if not found:
                            raise RuntimeError(f"Set {variable} to the real MySQL client binary")
                        env[variable] = found
            with log_path.open("w", encoding="utf-8") as stream:
                run = subprocess.run([sys.executable, "-m", "pytest", SUITES[suite], "-q", "--tb=short",
                    "--junitxml", str(junit.resolve())], cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
            counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            if junit.is_file():
                for item in ET.parse(junit).iter("testsuite"):
                    for key in counts:
                        counts[key] += int(item.attrib.get(key, 0))
            passed = run.returncode == 0 and counts["tests"] > 0 and not any(counts[key] for key in ("failures", "errors", "skipped"))
            if suite == "g4":
                from scripts.verify_g4_contracts import validate_junit
                contract_coverage = validate_junit(junit)
                passed = passed and contract_coverage["passed"]
                (args.evidence_dir/"g4-contract-coverage.json").write_text(json.dumps(contract_coverage,indent=2),encoding="utf8")
            outcomes[suite] = {"passed": passed, "exit_code": run.returncode, **counts, "log": str(log_path)}
            print(json.dumps({"suite": suite, **outcomes[suite]}, ensure_ascii=False), flush=True)
        report = {"passed": all(item["passed"] for item in outcomes.values()), "outcomes": outcomes, "schemas": names}
        (args.evidence_dir/"acceptance-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if report["passed"] else 1
    finally:
        server.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
