"""G0 spec T2: Database Restore Utility (SQLite + MySQL dual support)

Reads the manifest file produced by db_backup.py, validates MD5 + SHA256
hashes, then restores the DB. For SQLite the current DB is moved into a
rollback copy BEFORE the restored file is put in place; for MySQL the dump
is piped through `mysql` client.

Usage (SQLite):
    python scripts/db_restore.py --manifest ./backups/backup_sqlite_*.manifest.json \\
        --db-url sqlite:///.../app.db [--dry-run]

Usage (MySQL):
    python scripts/db_restore.py --manifest ./backups/backup_mysql_*.manifest.json \\
        --db-url mysql+pymysql://user:password@host/new_empty_schema \\
        [--mysql /usr/bin/mysql] [--dry-run]

An explicit --db-url or DB_RESTORE_TARGET_URL is mandatory. Actual SHA256
verification cannot be bypassed. MySQL restoration refuses all nonempty target
schemas, including when the legacy SQLite force option is supplied.

Exit codes:
    0 ok, 1 args/manifest error, 2 hash mismatch, 3 DB access error,
    4 unsafe/nonempty target, or SQLite irreversible revision without explicit force.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IRREVERSIBLE_REVISION = "wps_0023_026_drop_legacy_last_successful_trade_date"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_file(path: Path, algo: str, chunk: int = 1 << 20) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _sqlite_path_from_url(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    p = Path(raw)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _mysql_parts_from_url(url: str) -> dict | None:
    # One URL contract for SQLAlchemy's identity check and both native clients.
    from scripts.db_backup import _mysql_parts_from_url as parse_client_target
    return parse_client_target(url)


def _current_alembic_head(db_url: str) -> str | None:
    try:
        from sqlalchemy import create_engine, text
    except Exception:
        return None
    try:
        engine = create_engine(db_url)
        with engine.connect() as c:
            rows = c.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1")).fetchall()
        engine.dispose()
        return rows[0][0] if rows else None
    except Exception:
        return None


def _validate_hashes(manifest: dict, backup_path: Path, no_hash_check: bool) -> tuple[bool, str]:
    if no_hash_check:
        return False, "--no-hash-check is no longer permitted; restore requires SHA256 verification"
    errors: list[str] = []
    expect_md5 = manifest.get("md5")
    expect_sha = manifest.get("sha256")
    if not isinstance(expect_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expect_sha):
        return False, "manifest must contain a valid 64-digit SHA256"
    if expect_md5 is not None and (not isinstance(expect_md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", expect_md5)):
        return False, "manifest MD5 is malformed"
    if expect_md5:
        actual = _hash_file(backup_path, "md5")
        if actual.lower() != expect_md5.lower():
            errors.append(f"md5 mismatch: expect={expect_md5} actual={actual}")
    if expect_sha:
        actual = _hash_file(backup_path, "sha256")
        if actual.lower() != expect_sha.lower():
            errors.append(f"sha256 mismatch: expect={expect_sha} actual={actual}")
    if errors:
        return False, "; ".join(errors)
    check = [s for s in ("md5", "sha256") if manifest.get(s)]
    return True, f"OK ({', '.join(check)})"


# ---------------------------------------------------------------------------
# Restore backends
# ---------------------------------------------------------------------------

def restore_sqlite(
    manifest: dict, backup_path: Path, db_url: str,
    dry_run: bool, no_hash_check: bool,
    force_rewrite_over_0026: bool,
) -> int:
    db_path = _sqlite_path_from_url(db_url)
    assert db_path is not None
    # 1) hash validation
    ok, msg = _validate_hashes(manifest, backup_path, no_hash_check)
    if not ok:
        sys.stderr.write(f"[restore] HASH CHECK FAILED: {msg}\n")
        return 2
    print(f"[restore] hash validation : {msg}")

    # 2) irreversible revision guard: if LIVE DB is already at 0026 but the
    # backup does not have that column yet, restoring would regress the
    # irreversible DROP without explicit consent.
    live_rev = _current_alembic_head(db_url)
    backup_rev = manifest.get("alembic_version")
    if live_rev == IRREVERSIBLE_REVISION and backup_rev != IRREVERSIBLE_REVISION:
        if not force_rewrite_over_0026:
            sys.stderr.write(
                f"[restore] REFUSE: live DB is at irreversible revision "
                f"{IRREVERSIBLE_REVISION} but backup is at {backup_rev!r}.\n"
                f"          Pass --force-rewrite-over-0026 to explicitly permit this regressive restore.\n"
            )
            return 4
        print(f"[restore] overwriting live 0026 DB (--force-rewrite-over-0026)")

    if dry_run:
        print(f"[restore] DRY-RUN: would copy {backup_path} -> {db_path}")
        print(f"[restore] DRY-RUN: live revision = {live_rev!r}, backup revision = {backup_rev!r}")
        return 0

    # 3) move current DB into rollback copy (keep newest 3)
    if db_path.exists():
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        rollback = db_path.with_name(db_path.name + f".pre-restore.{ts}.bak")
        shutil.copy2(db_path, rollback)
        # trim old rollbacks to prevent disk bloat
        rolls = sorted(db_path.parent.glob(db_path.name + ".pre-restore.*.bak"))
        for old in rolls[:-3]:
            try:
                old.unlink()
            except OSError:
                pass
        print(f"[restore] rollback snapshot : {rollback}")

    shutil.copy2(backup_path, db_path)
    print(f"[restore] restored SQLite DB to: {db_path}")
    return 0


def restore_mysql(
    manifest: dict, backup_path: Path, db_url: str,
    mysql_bin: str, dry_run: bool, no_hash_check: bool,
    force_rewrite_over_0026: bool,
) -> int:
    try:
        parts = _mysql_parts_from_url(db_url)
    except Exception:
        sys.stderr.write("[restore] Invalid/unsupported explicit MySQL URL; no import executed.\n")
        return 1
    if parts is None:
        sys.stderr.write("[restore] Explicit target must use a MySQL URL.\n")
        return 1

    ok, msg = _validate_hashes(manifest, backup_path, no_hash_check)
    if not ok:
        sys.stderr.write(f"[restore] HASH CHECK FAILED: {msg}\n")
        return 2
    print(f"[restore] hash validation : {msg}")

    # A mysqldump contains DROP TABLE statements. Its target must be a newly
    # provisioned empty schema, never a live DB selected by implicit config.
    # The legacy force option cannot bypass this rule. Writers must remain
    # stopped for the entire restore; MySQL DDL cannot roll back an import.
    if not parts["database"]:
        sys.stderr.write("[restore] MySQL target must explicitly name a database.\n")
        return 1
    from sqlalchemy import create_engine, text
    engine = create_engine(db_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            actual = connection.exec_driver_sql("SELECT DATABASE()").scalar_one()
            if actual != parts["database"]:
                sys.stderr.write("[restore] REFUSE: actual database differs from the explicit target.\n")
                return 4
            existing = connection.execute(text("SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=:database LIMIT 1"), {"database": actual}).first()
            if existing:
                sys.stderr.write("[restore] REFUSE: MySQL target contains tables/views; restore requires a fresh empty schema.\n")
                return 4
    except Exception as exc:
        sys.stderr.write(f"[restore] Target identity/emptiness check failed ({type(exc).__name__}); no import executed.\n")
        return 3
    finally:
        engine.dispose()

    if dry_run:
        print(f"[restore] DRY-RUN: would pipe {backup_path} through {mysql_bin} "
              f"into {parts['user']}@{parts['host']}:{parts['port']}/{parts['database']}")
        return 0

    mysql_exe = shutil.which(mysql_bin) or mysql_bin
    cmd = [mysql_exe, *parts["client_transport_args"], "--default-character-set=utf8mb4"]
    cmd += ["--host", parts["host"], "--port", str(parts["port"])]
    if parts["user"]:
        cmd += ["-u", parts["user"]]
    # --database
    cmd += ["--database", parts["database"]]

    env = os.environ.copy()
    env.pop("MYSQL_PWD", None)
    if parts["password"]:
        env["MYSQL_PWD"] = parts["password"]

    with backup_path.open("rb") as f:
        proc = subprocess.run(cmd, stdin=f, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        sys.stderr.write(
            f"[restore] mysql import failed ({proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')}\n"
        )
        return 3
    print(f"[restore] restored MySQL dump to {parts['database']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="G0 DB restore with hash + irreversible guard")
    p.add_argument("--manifest", required=True, help="Path to *.manifest.json produced by db_backup.py")
    p.add_argument("--db-url", default=None, help="Explicit restore target; alternatively set DB_RESTORE_TARGET_URL. No application URL fallback.")
    p.add_argument("--mysql", default="mysql", help="Path to mysql client executable (MySQL only).")
    p.add_argument("--no-hash-check", action="store_true", help="Retired option: always rejected; SHA256 is mandatory.")
    p.add_argument("--dry-run", action="store_true", help="Do validation steps, but do not actually restore.")
    p.add_argument("--force-rewrite-over-0026", action="store_true",
                   help="SQLite legacy revision override only; never permits MySQL nonempty target overwrite.")
    args = p.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        sys.stderr.write(f"Manifest not found: {manifest_path}\n")
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"Manifest parse error: {type(e).__name__}: {e}\n")
        return 1

    if manifest.get("schema") != "quant-workbench/g0-backup/v1":
        sys.stderr.write(f"Manifest schema unknown: {manifest.get('schema')}\n")
        return 1

    # Resolve backup file (manifest JSON stores it relative to manifest dir)
    backup_file = manifest.get("backup_file")
    if not backup_file:
        sys.stderr.write("Manifest missing 'backup_file' key\n")
        return 1
    backup_path = (manifest_path.parent / backup_file).resolve()
    if not backup_path.exists():
        sys.stderr.write(f"Backup file referenced by manifest not found: {backup_path}\n")
        return 1

    # Restore never inherits a live application's implicit database target.
    db_url = args.db_url or os.getenv("DB_RESTORE_TARGET_URL")
    if not db_url:
        sys.stderr.write("[restore] Explicit --db-url or DB_RESTORE_TARGET_URL is required; application database defaults are forbidden.\n")
        return 1

    dialect = manifest.get("dialect", "sqlite")
    print(f"[restore] manifest   : {manifest_path}")
    print(f"[restore] backup     : {backup_path} ({manifest.get('bytes')} bytes)")
    print(f"[restore] dialect    : {dialect}")
    print(f"[restore] backup_rev : {manifest.get('alembic_version')}")
    print(f"[restore] created_at : {manifest.get('created_at')}")

    if dialect == "sqlite":
        return restore_sqlite(manifest, backup_path, db_url,
                              args.dry_run, args.no_hash_check,
                              args.force_rewrite_over_0026)
    elif dialect == "mysql":
        return restore_mysql(manifest, backup_path, db_url,
                             args.mysql, args.dry_run, args.no_hash_check,
                             args.force_rewrite_over_0026)
    else:
        sys.stderr.write(f"Unsupported manifest dialect: {dialect}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
