"""G0 spec T2: Database Backup Utility (SQLite + MySQL dual support)

Creates a timestamped backup of the app database, computes MD5+SHA256 hashes,
and emits a machine-readable manifest alongside the artifact. The restore
counterpart (db_restore.py) MUST validate hashes from this manifest before
allowing a restore.

Usage (SQLite, uses app settings DATABASE_URL by default):
    python scripts/db_backup.py [--output-dir ./backups] [--db-url URL]

Usage (MySQL):
    python scripts/db_backup.py --db-url "mysql+pymysql://u:p@host:3306/db" \\
        [--mysqldump /usr/bin/mysqldump] [--output-dir ./backups]

Exit codes: 0 ok, 1 validation/setup error, 2 irreversible migration detected
without explicit --allow-unsafe-0026, 3 DB access error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IRREVERSIBLE_REVISION = "wps_0023_026_drop_legacy_last_successful_trade_date"
IRREVERSIBLE_WARNING = (
    "⚠️ 将要备份的数据库已应用 / 即将应用不可逆 revision "
    + IRREVERSIBLE_REVISION
    + "（DROP portfolios.last_successful_trade_date）。请确认此备份将用于"
    + " 潜在的降级/灾难恢复。传 --allow-unsafe-0026 跳过此确认。"
)


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


def _md5_file(p: Path) -> str:
    return _hash_file(p, "md5")


def _sha256_file(p: Path) -> str:
    return _hash_file(p, "sha256")


def _sqlite_path_from_url(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    p = Path(raw)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _mysql_parts_from_url(url: str) -> dict[str, Any] | None:
    u = urlparse(url)
    if u.scheme not in ("mysql", "mysql+pymysql", "mysql+mysqlconnector"):
        return None
    return {
        "host": u.hostname or "127.0.0.1",
        "port": int(u.port or 3306),
        "user": u.username or "",
        "password": u.password or "",
        "database": u.path.lstrip("/") or "",
    }


def _current_alembic_head(db_url: str) -> str | None:
    """Return current alembic_version.version_num for the DB (if any)."""
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


def _count_sqlite_tables(db_path: Path) -> int:
    try:
        from sqlalchemy import create_engine, inspect
    except Exception:
        return -1
    try:
        eng = create_engine(f"sqlite:///{db_path.as_posix()}")
        n = len(inspect(eng).get_table_names())
        eng.dispose()
        return n
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

@dataclass
class BackupArtifact:
    dialect: str
    db_url_scheme: str
    backup_file: str  # relative to output_dir
    manifest_file: str  # absolute
    bytes: int
    md5: str
    sha256: str
    created_at: str
    alembic_version: str | None
    sqlite_table_count: int | None
    mysqldump_version: str | None = None
    notes: list[str] = field(default_factory=list)


def backup_sqlite(db_url: str, out_dir: Path) -> BackupArtifact:
    src = _sqlite_path_from_url(db_url)
    assert src is not None
    if not src.exists():
        raise FileNotFoundError(f"SQLite DB not found: {src}")
    # For SQLite, enforce a checkpoint FIRST so the backup file is complete
    # (not just .db + WAL side files).
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(f"sqlite:///{src.as_posix()}")
        with eng.connect() as c:
            c.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            c.execute(text("PRAGMA journal_mode=DELETE"))
        eng.dispose()
    except Exception:
        pass

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_file = f"backup_sqlite_{ts}.db"
    dst = out_dir / dst_file
    shutil.copy2(src, dst)

    md5 = _md5_file(dst)
    sha256 = _sha256_file(dst)
    alembic_v = _current_alembic_head(db_url)
    tbl_count = _count_sqlite_tables(dst)
    size = dst.stat().st_size

    art = BackupArtifact(
        dialect="sqlite",
        db_url_scheme="sqlite",
        backup_file=dst_file,
        manifest_file=str(out_dir / f"backup_sqlite_{ts}.manifest.json"),
        bytes=size,
        md5=md5,
        sha256=sha256,
        created_at=_dt.datetime.now().isoformat(timespec="seconds"),
        alembic_version=alembic_v,
        sqlite_table_count=tbl_count,
    )
    return art


def backup_mysql(db_url: str, out_dir: Path, mysqldump: str) -> BackupArtifact:
    parts = _mysql_parts_from_url(db_url)
    assert parts is not None
    mysqldump_bin = shutil.which(mysqldump) or mysqldump
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_file = f"backup_mysql_{parts['database']}_{ts}.sql"
    dst = out_dir / dst_file

    cmd = [mysqldump_bin]
    cmd += ["--host", parts["host"], "--port", str(parts["port"])]
    if parts["user"]:
        cmd += ["-u", parts["user"]]
    if parts["password"]:
        # NOTE: passing pw on cmd line leaks it via /proc; acceptable for
        # controlled backup scripts per G0 spec T2. Prefer MYSQL_PWD env.
        cmd += [f"--password={parts['password']}"]
    cmd += ["--single-transaction", "--routines", "--triggers", parts["database"]]

    try:
        mv = subprocess.run(
            [mysqldump_bin, "--version"],
            capture_output=True, text=True, check=False,
        ).stderr or subprocess.run(
            [mysqldump_bin, "-V"],
            capture_output=True, text=True, check=False,
        ).stdout or ""
    except Exception:
        mv = ""

    env = os.environ.copy()
    if parts["password"]:
        env["MYSQL_PWD"] = parts["password"]
    with dst.open("wb") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"mysqldump failed ({proc.returncode}): {proc.stderr.decode(errors='replace')}"
        )

    md5 = _md5_file(dst)
    sha256 = _sha256_file(dst)
    alembic_v = _current_alembic_head(db_url)
    size = dst.stat().st_size

    art = BackupArtifact(
        dialect="mysql",
        db_url_scheme="mysql",
        backup_file=dst_file,
        manifest_file=str(out_dir / f"backup_mysql_{parts['database']}_{ts}.manifest.json"),
        bytes=size,
        md5=md5,
        sha256=sha256,
        created_at=_dt.datetime.now().isoformat(timespec="seconds"),
        alembic_version=alembic_v,
        sqlite_table_count=None,
        mysqldump_version=mv.strip(),
    )
    return art


def write_manifest(art: BackupArtifact) -> None:
    payload: dict[str, Any] = asdict(art)
    # manifest_file is the output path of the manifest itself; strip from
    # payload to avoid self-reference or write it manually.
    out_path = Path(payload.pop("manifest_file"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload["manifest_version"] = "1.0"
    payload["schema"] = "quant-workbench/g0-backup/v1"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # Overwrite art.manifest_file with the absolute path we actually wrote to
    # for the final stdout summary.
    art.manifest_file = str(out_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="G0 DB backup + hash + manifest writer")
    p.add_argument("--db-url", default=None, help="Override DATABASE_URL.")
    p.add_argument("--output-dir", default=str(ROOT / "backups"),
                   help="Where to place backup file + manifest. Default: ./backups")
    p.add_argument("--mysqldump", default="mysqldump",
                   help="Path to mysqldump executable (MySQL only).")
    p.add_argument("--allow-unsafe-0026", action="store_true",
                   help="Skip the interactive confirmation for irreversible 0026 migration.")
    p.add_argument("--label", default="", help="Optional label written into manifest notes.")
    args = p.parse_args(argv)

    # Resolve DB URL (prefer explicit CLI > env > settings)
    from app.core.config import settings
    db_url: str = args.db_url or os.getenv("DATABASE_URL") or settings.database_url
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- irreversible migration safety check ---
    rev = _current_alembic_head(db_url)
    if rev == IRREVERSIBLE_REVISION and not args.allow_unsafe_0026:
        sys.stderr.write(IRREVERSIBLE_WARNING + "\n")
        resp = input("Continue anyway? [y/N] ").strip().lower()
        if resp not in {"y", "yes"}:
            return 2

    print(f"[backup] dialect   = {db_url.split('://')[0]}")
    print(f"[backup] out_dir   = {out_dir}")
    print(f"[backup] alembic_v = {rev!r}")

    try:
        if db_url.startswith("sqlite:///"):
            art = backup_sqlite(db_url, out_dir)
        elif urlparse(db_url).scheme in ("mysql", "mysql+pymysql", "mysql+mysqlconnector"):
            art = backup_mysql(db_url, out_dir, args.mysqldump)
        else:
            sys.stderr.write(f"Unsupported DB URL scheme: {db_url}\n")
            return 1
    except Exception as e:
        sys.stderr.write(f"[backup] FAILED: {type(e).__name__}: {e}\n")
        return 3

    if args.label:
        art.notes.append(f"label={args.label}")
    write_manifest(art)

    # --- Summary
    print(f"[backup] OK")
    print(f"  file       : {art.backup_file} ({art.bytes} bytes)")
    print(f"  md5        : {art.md5}")
    print(f"  sha256     : {art.sha256}")
    print(f"  alembic    : {art.alembic_version}")
    print(f"  sqlite_tbl : {art.sqlite_table_count}")
    print(f"  manifest   : {art.manifest_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
