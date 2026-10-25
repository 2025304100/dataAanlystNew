"""Explicit-target MySQL deployment guard. Never falls back to app config."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class GuardFailure(Exception):
    def __init__(self, code, **details):
        self.code, self.details = code, details


def _target_url(raw, expected_database):
    if not re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", expected_database):
        raise GuardFailure("ISOLATED_DATABASE_REQUIRED")
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or url.database != expected_database:
        raise GuardFailure("TARGET_DATABASE_MISMATCH")
    return url


def read_fingerprint(connection):
    identity = connection.execute(text(
        "SELECT DATABASE() AS db_name, @@hostname AS server_host, @@port AS server_port, "
        "@@server_uuid AS server_uuid, VERSION() AS server_version, @@session.tx_isolation AS isolation_level"
    )).mappings().one()
    tables = dict(connection.execute(text(
        "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'"
    )).all())
    revisions = list(connection.scalars(text("SELECT version_num FROM alembic_version ORDER BY version_num"))) if "alembic_version" in tables else []
    return {"database": identity["db_name"], "server_host": identity["server_host"],
            "server_port": identity["server_port"], "server_uuid": identity["server_uuid"],
            "server_version": identity["server_version"], "isolation_level": identity["isolation_level"],
            "revisions": revisions or ["base"], "table_engines": tables}


def _verify_pair(app, migration, args):
    for fingerprint in (app, migration):
        if fingerprint["database"] != args.expected_database:
            raise GuardFailure("TARGET_DATABASE_MISMATCH")
        if fingerprint["server_version"] != args.expected_server_version:
            raise GuardFailure("SERVER_VERSION_MISMATCH", actual=fingerprint["server_version"])
        if fingerprint["revisions"] != [args.expected_revision]:
            raise GuardFailure("REVISION_MISMATCH", actual=fingerprint["revisions"])
        unsafe = sorted(name for name, engine in fingerprint["table_engines"].items() if str(engine).lower() != "innodb")
        if unsafe:
            raise GuardFailure("NON_TRANSACTIONAL_TABLES", tables=unsafe)
    identity = ("database", "server_host", "server_port", "server_uuid")
    if not app["server_uuid"] or any(app[key] != migration[key] for key in identity):
        raise GuardFailure("SERVER_IDENTITY_MISMATCH")


def diagnose_members(connection):
    """Read every historical row without repairing or inventing event times."""
    schema = inspect(connection)
    if not schema.has_table("portfolio_members"):
        return {"checked_rows": 0, "blockers": []}
    required = {"id", "portfolio_id", "symbol_id", "status", "execution_mode", "effective_from", "effective_to"}
    missing = sorted(required - {column["name"] for column in schema.get_columns("portfolio_members")})
    if missing:
        return {"checked_rows": 0, "blockers": [{"code": "MEMBER_DIAGNOSTIC_COLUMNS_MISSING", "columns": missing}]}
    blockers, current, checked = [], {}, 0
    rows = connection.execute(text(
        "SELECT id, portfolio_id, symbol_id, status, execution_mode, effective_from, effective_to "
        "FROM portfolio_members ORDER BY id"
    )).mappings()
    for row in rows:
        checked += 1
        identity = {"member_id": row["id"], "portfolio_id": row["portfolio_id"], "symbol_id": row["symbol_id"]}
        mode = row["execution_mode"]
        canonical = mode.strip().lower() if isinstance(mode, str) else ""
        if canonical not in {"manual", "confirm", "auto"}:
            blockers.append({**identity, "code": "EXECUTION_MODE_INVALID", "value": mode,
                             "recommendation": "Recover the intended mode from an approved business record; do not default silently."})
        elif canonical != mode:
            blockers.append({**identity, "code": "EXECUTION_MODE_NONCANONICAL", "value": mode,
                             "suggested_value": canonical, "recommendation": "Back up this row and explicitly approve trim/lower normalization."})
        if row["status"] not in {"active", "paused", "archived"}:
            blockers.append({**identity, "code": "MEMBER_STATUS_INVALID", "value": row["status"]})
        if row["status"] == "archived" and row["effective_to"] is None:
            blockers.append({**identity, "code": "ARCHIVE_TIME_UNPROVEN",
                             "recommendation": "Recover the archive timestamp from audit evidence. Never substitute now or effective_from."})
        elif row["status"] in {"active", "paused"} and row["effective_to"] is not None:
            blockers.append({**identity, "code": "CLOSED_MEMBER_STATUS_CONFLICT",
                             "recommendation": "Resolve status against the recorded lifecycle before changing historical boundaries."})
        if row["effective_from"] is None or (row["effective_to"] is not None and row["effective_to"] < row["effective_from"]):
            blockers.append({**identity, "code": "MEMBER_INTERVAL_INVALID"})
        if row["effective_to"] is None:
            pair = (row["portfolio_id"], row["symbol_id"])
            if pair in current:
                blockers.append({**identity, "code": "DUPLICATE_CURRENT_MEMBER", "other_member_id": current[pair]})
            current[pair] = row["id"]
    return {"checked_rows": checked, "blockers": blockers}


def _require_clean_members(connection, report):
    diagnostics = diagnose_members(connection)
    report["member_diagnostics"] = diagnostics
    if diagnostics["blockers"]:
        raise GuardFailure("HISTORICAL_MEMBER_BLOCKERS", **report)


def upgrade_verified(connection, target_revision):
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    if not target_revision or target_revision in {"head", "heads"} or "+" in target_revision:
        raise GuardFailure("EXACT_TARGET_REVISION_REQUIRED")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    if target_revision != "base":
        resolved = ScriptDirectory.from_config(config).get_revision(target_revision)
        if resolved is None or resolved.revision != target_revision:
            raise GuardFailure("EXACT_TARGET_REVISION_REQUIRED")
    config.attributes["connection"] = connection
    # Historical migrations print progress. Keep stdout a single JSON report
    # so a successful migration cannot become an undecodable CI response.
    with redirect_stdout(sys.stderr):
        command.upgrade(config, target_revision)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "upgrade"))
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-server-version", required=True)
    parser.add_argument("--target-revision")
    args = parser.parse_args(argv)
    if not os.environ.get("APP_DATABASE_URL") or not os.environ.get("ALEMBIC_DATABASE_URL"):
        print(json.dumps({"passed": False, "error_code": "EXPLICIT_URL_REQUIRED"}))
        return 2
    engines = []
    try:
        # Validate both declarations before any connection: a typo must never
        # cause even a read against gpfx or another unapproved schema.
        app_url = _target_url(os.environ["APP_DATABASE_URL"], args.expected_database)
        migration_url = _target_url(os.environ["ALEMBIC_DATABASE_URL"], args.expected_database)
        engines = [create_engine(url, hide_parameters=True) for url in (app_url, migration_url)]
        with engines[0].connect() as app_connection, engines[1].connect() as migration_connection:
            app, migration = read_fingerprint(app_connection), read_fingerprint(migration_connection)
            _verify_pair(app, migration, args)
            report = {"passed": True, "mode": args.mode, "app": app, "alembic": migration}
            _require_clean_members(migration_connection, report)
            if args.mode == "upgrade":
                # Release metadata read locks held by the comparison session.
                app_connection.rollback()
                migration_connection.rollback()
                lock_name = "combination-migration-" + hashlib.sha256(
                    (migration["server_uuid"] + migration["database"]).encode()).hexdigest()[:32]
                locked = migration_connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name})
                if locked != 1:
                    raise GuardFailure("MIGRATION_BUSY")
                try:
                    migration_connection.rollback()
                    _verify_pair(app, read_fingerprint(migration_connection), args)
                    _require_clean_members(migration_connection, report)
                    upgrade_verified(migration_connection, args.target_revision)
                    after = read_fingerprint(migration_connection)
                    if after["revisions"] != [args.target_revision] or after["server_uuid"] != app["server_uuid"] or after["database"] != args.expected_database:
                        raise GuardFailure("POST_UPGRADE_IDENTITY_MISMATCH", after=after)
                    report["after"] = after
                finally:
                    migration_connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
            print(json.dumps(report))
            return 0
    except GuardFailure as exc:
        print(json.dumps({**exc.details, "passed": False, "error_code": exc.code}))
        return 2
    except Exception as exc:
        # DBAPI exception text can contain connection credentials. Return a
        # stable diagnostic without printing the URLs or raw exceptions.
        print(json.dumps({"passed": False, "error_code": "DATABASE_CHECK_FAILED", "exception_type": type(exc).__name__}))
        return 2
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
