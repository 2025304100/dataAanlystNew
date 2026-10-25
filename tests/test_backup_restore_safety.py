"""Public backup/restore boundary: explicit targets and verified artifacts."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import db_backup, db_restore

pytestmark = pytest.mark.whitebox


@pytest.fixture
def sqlite_artifact(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, note TEXT NOT NULL)")
        conn.execute("INSERT INTO evidence VALUES (1, '原始证据，完整保留')")
    directory = tmp_path / "backups"
    directory.mkdir()
    artifact = db_backup.backup_sqlite(f"sqlite:///{source.as_posix()}", directory)
    db_backup.write_manifest(artifact)
    return Path(artifact.manifest_file)


def test_restore_cli_requires_explicit_target_without_database_url_fallback(sqlite_artifact, tmp_path, monkeypatch):
    fallback = tmp_path / "must-not-be-opened.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{fallback.as_posix()}")
    monkeypatch.delenv("DB_RESTORE_TARGET_URL", raising=False)
    assert db_restore.main(["--manifest", str(sqlite_artifact), "--dry-run"]) == 1
    assert not fallback.exists()


@pytest.mark.parametrize("sha", [None, "", "bad", "z" * 64])
def test_restore_cli_rejects_missing_or_invalid_sha256(sqlite_artifact, tmp_path, sha):
    payload = json.loads(sqlite_artifact.read_text(encoding="utf-8"))
    if sha is None:
        payload.pop("sha256")
    else:
        payload["sha256"] = sha
    sqlite_artifact.write_text(json.dumps(payload), encoding="utf-8")
    target = tmp_path / "target.db"
    assert db_restore.main(["--manifest", str(sqlite_artifact), "--db-url", f"sqlite:///{target.as_posix()}"]) == 2
    assert not target.exists()


def test_restore_cli_cannot_bypass_hash_for_real_restore(sqlite_artifact, tmp_path):
    target = tmp_path / "target.db"
    assert db_restore.main(["--manifest", str(sqlite_artifact), "--db-url", f"sqlite:///{target.as_posix()}", "--no-hash-check"]) == 2
    assert not target.exists()


class DatabaseBoundary:
    """External database adapter, so unsafe cases never touch a real server."""
    def __init__(self, database, tables=()):
        self.database, self.tables = database, tables
    def connect(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def dispose(self):
        pass
    def exec_driver_sql(self, sql):
        assert sql == "SELECT DATABASE()"
        return SimpleNamespace(scalar_one=lambda: self.database)
    def execute(self, sql, params=None):
        statement = str(sql)
        if "information_schema.TABLES" in statement:
            return SimpleNamespace(first=lambda: (self.tables[0],) if self.tables else None)
        if "alembic_version" in statement:
            return SimpleNamespace(fetchall=lambda: [])
        raise AssertionError(f"Unexpected database operation: {statement}")


@pytest.mark.parametrize("force", [False, True])
def test_mysql_restore_refuses_existing_tables_even_with_legacy_force_option(tmp_path, monkeypatch, force):
    import sqlalchemy
    dump = tmp_path / "fixture.sql"
    dump.write_bytes(b"DROP TABLE evidence;\n")
    manifest = {"sha256": hashlib.sha256(dump.read_bytes()).hexdigest()}
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: DatabaseBoundary("codex_combination_restore", ["evidence"]))
    clients = []
    monkeypatch.setattr(db_restore.subprocess, "run", lambda *a, **k: clients.append(a) or SimpleNamespace(returncode=0))
    result = db_restore.restore_mysql(manifest, dump,
        "mysql+pymysql://user:pw@127.0.0.1/codex_combination_restore", "mysql", False, False, force)
    assert result != 0
    assert clients == [], "Existing tables must be rejected before mysql imports any bytes"


def test_mysql_backup_and_restore_decode_url_credentials_at_client_boundary(tmp_path, monkeypatch):
    import sqlalchemy
    url = "mysql+pymysql://test%40user:p%40ss%25word@127.0.0.1/codex_combination_restore"
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: DatabaseBoundary("codex_combination_restore"))
    calls = []
    def client(command, **kwargs):
        if "--version" in command or "-V" in command:
            return SimpleNamespace(returncode=0, stdout="mysql  Ver 14.14 Distrib 5.7.26", stderr="")
        calls.append((command, kwargs.get("env", {})))
        if "stdout" in kwargs:
            kwargs["stdout"].write("SELECT '完整证据';\n".encode("utf-8"))
        return SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(db_backup.subprocess, "run", client)
    artifact = db_backup.backup_mysql(url, tmp_path, "mysqldump")
    db_backup.write_manifest(artifact)
    manifest = json.loads(Path(artifact.manifest_file).read_text(encoding="utf-8"))
    assert db_restore.restore_mysql(manifest, tmp_path / artifact.backup_file, url,
        "mysql", False, False, False) == 0
    assert len(calls) == 2
    for command, environment in calls:
        assert command[command.index("-u") + 1] == "test@user"
        assert environment["MYSQL_PWD"] == "p@ss%word"
        assert all("p@ss%word" not in argument for argument in command)


@pytest.mark.parametrize("via_env", [False, True])
def test_sqlite_restore_explicit_target_preserves_old_rollback_and_unicode(sqlite_artifact, tmp_path, monkeypatch, via_env):
    target = tmp_path / "explicit.db"
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO evidence VALUES (9, '保留恢复前值')")
    ignored = tmp_path / "ignored-default.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{ignored.as_posix()}")
    url = f"sqlite:///{target.as_posix()}"
    argv = ["--manifest", str(sqlite_artifact)]
    if via_env:
        monkeypatch.setenv("DB_RESTORE_TARGET_URL", url)
    else:
        monkeypatch.delenv("DB_RESTORE_TARGET_URL", raising=False)
        argv += ["--db-url", url]
    assert db_restore.main(argv) == 0
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT * FROM evidence").fetchall() == [(1, "原始证据，完整保留")]
    rollbacks = list(tmp_path.glob("explicit.db.pre-restore.*.bak"))
    assert len(rollbacks) == 1
    with sqlite3.connect(rollbacks[0]) as conn:
        assert conn.execute("SELECT * FROM evidence").fetchall() == [(9, "保留恢复前值")]
    assert not ignored.exists()


def test_mysql_restore_refuses_mismatched_actual_database(tmp_path, monkeypatch):
    import sqlalchemy
    dump = tmp_path / "fixture.sql"
    dump.write_bytes(b"SELECT 1;\n")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: DatabaseBoundary("unexpected_database"))
    monkeypatch.setattr(db_restore.subprocess, "run", lambda *a, **k: pytest.fail("Import executed before identity check"))
    assert db_restore.restore_mysql({"sha256": hashlib.sha256(dump.read_bytes()).hexdigest()}, dump,
        "mysql+pymysql://user:pw@127.0.0.1/codex_combination_restore", "mysql", False, False, False) != 0


def test_mysql_restore_rejects_query_routing_that_client_does_not_support(tmp_path, monkeypatch):
    import sqlalchemy
    dump = tmp_path / "fixture.sql"
    dump.write_bytes(b"SELECT 1;\n")
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: DatabaseBoundary("codex_combination_restore"))
    clients = []
    monkeypatch.setattr(db_restore.subprocess, "run", lambda *a, **k: clients.append(a) or SimpleNamespace(returncode=0))
    assert db_restore.restore_mysql({"sha256": hashlib.sha256(dump.read_bytes()).hexdigest()}, dump,
        "mysql+pymysql://user:pw@127.0.0.1/codex_combination_restore?unix_socket=/different/mysql.sock",
        "mysql", False, False, False) != 0
    assert not clients
