"""Serialize collection writes across processes without changing legacy rows.

MySQL named locks are scoped to the database and held on a dedicated connection.
SQLite is a local-file deployment: OS locks on a sibling file also release on
process exit. Lock acquisition is nonblocking; overlapping callers must retry.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text


class MarketEventCollectionBusy(RuntimeError):
    pass


@contextmanager
def market_event_write_lock(db):
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    dialect = engine.dialect.name
    if dialect == "mysql":
        name = "market-events:" + hashlib.sha256((engine.url.database or "").encode()).hexdigest()[:48]
        # Keep ownership separate from Session.commit(), which can return its
        # own connection to the pool before the lock is released.
        with engine.connect() as connection:
            acquired = connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": name})
            if acquired != 1:
                raise MarketEventCollectionBusy("行情消息正在采集，请稍后重试")
            try:
                yield
            finally:
                try:
                    released = connection.scalar(text("SELECT RELEASE_LOCK(:name)"), {"name": name})
                except Exception:
                    connection.invalidate()
                    raise
                if released != 1:
                    connection.invalidate()
        return
    if dialect != "sqlite":
        raise RuntimeError(f"Unsupported market event collection dialect: {dialect}")

    database = engine.url.database
    if database and database != ":memory:":
        lock_path = Path(str(Path(database).resolve()) + ".market-events.lock")
    else:
        # Independent in-memory engines cannot share data across processes.
        lock_path = Path(tempfile.gettempdir()) / f"market-events-{os.getpid()}-{id(engine)}.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise MarketEventCollectionBusy("行情消息正在采集，请稍后重试") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
