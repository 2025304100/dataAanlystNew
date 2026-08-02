"""Cross-process lock governance and stale task recovery for the DuckDB warehouse.

DuckDB supports many readers but only one writer process at a time. The
existing ``FactorWarehouse`` uses an in-process ``threading.RLock``
(``_PATH_LOCKS``) which serializes writes within a single process but
cannot prevent two separate processes from opening the same warehouse
file for writing simultaneously.  This module adds:

1. **Cross-process file-based mutual exclusion** — atomic
   ``O_CREAT | O_EXCL`` lockfile guarantees that only one process can
   hold the warehouse write lock at any time.
2. **Lock owner diagnostics** — returns PID, process name and hold
   duration so operators can identify who is blocking the warehouse.
3. **Stale lock cleanup** — PID liveness check (psutil when available,
   ctypes/os.kill fallback) automatically recovers locks left behind by
   crashed processes.
4. **Stale task diagnosis and recovery** — detects ``running`` tasks
   whose heartbeat or stage budget has expired and marks them ``failed``
   so they no longer block single-flight scheduling.
5. **Full diagnostics script** — ``run_warehouse_lock_diagnostics``
   produces a JSON-serialisable report for operational runbooks.

``psutil`` is an **optional** dependency: when unavailable the module
degrades gracefully — PID liveness is checked via a ctypes (Windows) or
``os.kill`` (Unix) fallback, and process-name resolution is skipped.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import psutil  # type: ignore

    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without psutil
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WarehouseLockUnavailable(RuntimeError):
    """Raised when a cross-process warehouse lock cannot be acquired.

    Carries the ``owner_pid`` of the process currently holding the lock
    (when identifiable) so callers can include it in diagnostics.
    """

    def __init__(
        self, path: str, owner_pid: int | None, reason: str
    ) -> None:
        self.path = path
        self.owner_pid = owner_pid
        self.reason = reason
        detail = f"Warehouse lock unavailable for {path}: {reason}"
        if owner_pid is not None:
            detail += f" (held by PID {owner_pid})"
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WarehouseLockInfo:
    """DuckDB warehouse lock diagnostic information."""

    path: str
    is_locked: bool
    lock_owner_pid: int | None
    lock_owner_process: str | None
    lock_acquired_at: datetime | None
    lock_age_seconds: float | None
    diagnostic_method: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WarehouseLockAcquisition:
    """Result of a cross-process lock acquisition attempt."""

    acquired: bool
    lockfile_path: str
    owner_pid: int | None
    owner_started_at: datetime | None
    reason: str | None


@dataclass(frozen=True)
class StaleTaskDiagnostic:
    """Diagnostic result for a potentially stale (zombie) task."""

    task_id: str
    status: str
    heartbeat_at: datetime | None
    last_progress_at: datetime | None
    stage_started_at: datetime | None
    stage_budget_seconds: int | None
    is_stale: bool
    stale_reason: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    """Return current UTC time as a tz-naive datetime (matches DB storage)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lockfile_path(warehouse_path: Path) -> Path:
    """Return the lockfile path for a given warehouse file.

    Uses ``<warehouse>.lock`` so the DuckDB suffix is preserved.
    """
    return Path(str(warehouse_path) + ".lock")


def _pid_exists(pid: int) -> bool:
    """Check whether a process with *pid* is currently alive.

    Uses ``psutil.pid_exists`` when available.  Falls back to a
    ctypes ``OpenProcess`` call on Windows or ``os.kill(pid, 0)``
    on Unix.  Returns ``True`` (conservative) when the check itself
    raises an unexpected error.
    """
    if pid <= 0:
        return False
    if _PSUTIL_AVAILABLE:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return True
    # Fallback without psutil
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, 0, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    # Unix fallback
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _process_name(pid: int) -> str | None:
    """Return the process name for *pid*, or ``None`` if unavailable."""
    if _PSUTIL_AVAILABLE:
        try:
            return psutil.Process(pid).name()
        except Exception:
            return None
    return None


def _read_lockfile(lockfile: Path) -> dict | None:
    """Read and parse lockfile content.

    Returns ``None`` if the file is missing, empty, or unparseable.
    """
    try:
        content = lockfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not content.strip():
        return None
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _file_mtime(lockfile: Path) -> datetime:
    return datetime.fromtimestamp(lockfile.stat().st_mtime)


# ---------------------------------------------------------------------------
# Lock diagnosis
# ---------------------------------------------------------------------------


def diagnose_warehouse_lock(warehouse_path: Path) -> WarehouseLockInfo:
    """Diagnose the DuckDB warehouse file lock state.

    Windows does not support ``fcntl``; this implementation uses a
    lockfile + PID liveness check:

    1. Check whether ``<warehouse>.lock`` exists.
    2. Read the file content (PID + ``acquired_at``).
    3. Verify the PID is still alive (``psutil.pid_exists``).
    4. If the process is dead but the lockfile remains, mark as stale.
    """
    lockfile = _lockfile_path(warehouse_path)
    path_str = str(warehouse_path)

    if not lockfile.exists():
        return WarehouseLockInfo(
            path=path_str,
            is_locked=False,
            lock_owner_pid=None,
            lock_owner_process=None,
            lock_acquired_at=None,
            lock_age_seconds=None,
            diagnostic_method="lockfile",
            notes=["no lockfile present"],
        )

    data = _read_lockfile(lockfile)

    # Empty / unparseable lockfile — owner likely crashed mid-write.
    if data is None:
        mtime = _file_mtime(lockfile)
        age = max(0.0, (_utcnow_naive() - mtime).total_seconds())
        return WarehouseLockInfo(
            path=path_str,
            is_locked=False,
            lock_owner_pid=None,
            lock_owner_process=None,
            lock_acquired_at=mtime,
            lock_age_seconds=age,
            diagnostic_method="lockfile",
            notes=[
                "lockfile exists but content is empty/unparseable; "
                "treated as stale (owner process likely crashed)"
            ],
        )

    pid = data.get("pid")
    acquired_at = _parse_datetime(data.get("acquired_at"))

    # No valid PID in lockfile — cannot verify ownership.
    if not isinstance(pid, int):
        mtime = _file_mtime(lockfile)
        age = max(0.0, (_utcnow_naive() - (acquired_at or mtime)).total_seconds())
        return WarehouseLockInfo(
            path=path_str,
            is_locked=False,
            lock_owner_pid=None,
            lock_owner_process=None,
            lock_acquired_at=acquired_at or mtime,
            lock_age_seconds=age,
            diagnostic_method="lockfile",
            notes=["lockfile has no valid PID; treated as stale"],
        )

    alive = _pid_exists(pid)
    if _PSUTIL_AVAILABLE:
        method = "psutil"
    elif sys.platform == "win32":
        method = "ctypes"
    else:
        method = "os.kill"

    if acquired_at is None:
        acquired_at = _file_mtime(lockfile)
    age = max(0.0, (_utcnow_naive() - acquired_at).total_seconds())

    if alive:
        return WarehouseLockInfo(
            path=path_str,
            is_locked=True,
            lock_owner_pid=pid,
            lock_owner_process=_process_name(pid),
            lock_acquired_at=acquired_at,
            lock_age_seconds=age,
            diagnostic_method=method,
            notes=[],
        )
    return WarehouseLockInfo(
        path=path_str,
        is_locked=False,
        lock_owner_pid=pid,
        lock_owner_process=None,
        lock_acquired_at=acquired_at,
        lock_age_seconds=age,
        diagnostic_method=method,
        notes=[f"PID {pid} is not alive; lockfile is stale"],
    )


# ---------------------------------------------------------------------------
# Lock acquire / release
# ---------------------------------------------------------------------------


def acquire_warehouse_lock(
    warehouse_path: Path,
    *,
    owner_pid: int | None = None,
    timeout_seconds: float = 0.0,
) -> WarehouseLockAcquisition:
    """Acquire a cross-process mutual-exclusion lock.

    Uses atomic file creation (``O_CREAT | O_EXCL``) to guarantee
    cross-process safety.  When *timeout_seconds* > 0 and the lock is
    held, polls until the timeout expires.  Stale lockfiles (whose owner
    PID is dead) are automatically recycled.
    """
    lockfile = _lockfile_path(warehouse_path)
    lockfile_str = str(lockfile)
    pid = owner_pid if owner_pid is not None else os.getpid()
    acquired_at = _utcnow_naive()
    deadline = (
        time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    )

    while True:
        # Attempt atomic creation.
        try:
            fd = os.open(
                lockfile_str, os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            try:
                content = json.dumps(
                    {
                        "pid": pid,
                        "acquired_at": acquired_at.isoformat(),
                        "hostname": os.environ.get("COMPUTERNAME")
                        or os.environ.get("HOSTNAME"),
                    }
                )
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            return WarehouseLockAcquisition(
                acquired=True,
                lockfile_path=lockfile_str,
                owner_pid=pid,
                owner_started_at=acquired_at,
                reason=None,
            )
        except FileExistsError:
            # Lockfile exists — check whether it is stale.
            data = _read_lockfile(lockfile)
            if data is not None:
                existing_pid = data.get("pid")
                if (
                    isinstance(existing_pid, int)
                    and not _pid_exists(existing_pid)
                ):
                    # Stale lock — recycle and retry.
                    try:
                        lockfile.unlink()
                    except OSError:
                        pass
                    continue

            # Lock is held by a live (or indeterminate) process.
            if deadline is not None and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                time.sleep(min(0.5, max(0.1, remaining / 10)))
                continue

            info = diagnose_warehouse_lock(warehouse_path)
            return WarehouseLockAcquisition(
                acquired=False,
                lockfile_path=lockfile_str,
                owner_pid=info.lock_owner_pid,
                owner_started_at=info.lock_acquired_at,
                reason="held_by_other",
            )
        except OSError as exc:
            return WarehouseLockAcquisition(
                acquired=False,
                lockfile_path=lockfile_str,
                owner_pid=None,
                owner_started_at=None,
                reason=f"io_error:{exc.__class__.__name__}",
            )


def release_warehouse_lock(
    warehouse_path: Path, *, owner_pid: int | None = None
) -> bool:
    """Release the cross-process lock.

    Only deletes the lockfile when the recorded PID matches *owner_pid*
    (defaults to the current PID), preventing accidental release of a
    lock owned by another process.
    """
    lockfile = _lockfile_path(warehouse_path)
    pid = owner_pid if owner_pid is not None else os.getpid()

    if not lockfile.exists():
        return False

    data = _read_lockfile(lockfile)
    if data is not None:
        existing_pid = data.get("pid")
        if isinstance(existing_pid, int) and existing_pid != pid:
            return False  # Refuse to release another process's lock.

    try:
        lockfile.unlink()
        return True
    except OSError:
        return False


def cleanup_stale_warehouse_lock(warehouse_path: Path) -> bool:
    """Remove a stale lockfile whose owner process is no longer alive.

    Returns ``True`` if a stale lockfile was removed, ``False`` if the
    lockfile does not exist or is still held by a live process.
    """
    lockfile = _lockfile_path(warehouse_path)
    if not lockfile.exists():
        return False

    data = _read_lockfile(lockfile)
    if data is not None:
        existing_pid = data.get("pid")
        if isinstance(existing_pid, int) and _pid_exists(existing_pid):
            return False  # Lock is held by a live process.

    try:
        lockfile.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Stale task diagnosis & recovery
# ---------------------------------------------------------------------------


def diagnose_stale_tasks(
    db_session,
    *,
    heartbeat_timeout_seconds: int = 90,
    stage_budget_grace_seconds: int = 300,
) -> list[StaleTaskDiagnostic]:
    """Diagnose all ``status='running'`` tasks whose heartbeat has expired.

    Does **not** modify any task state — callers decide whether to act
    on the diagnostics (see :func:`recover_stale_tasks`).

    A task is considered stale when any of the following holds:

    * ``heartbeat_at`` (or ``updated_at`` fallback) is older than
      *heartbeat_timeout_seconds* (default 90 s = 3× heartbeat interval).
    * ``stage_started_at + stage_budget_seconds + grace`` is in the past.
    * No heartbeat / updated_at / started_at timestamp exists at all
      (``process_lost``).
    """
    from sqlalchemy import select

    from app.models.async_task import AsyncTaskRecord

    now = _utcnow_naive()
    stmt = select(AsyncTaskRecord).where(AsyncTaskRecord.status == "running")
    rows = db_session.execute(stmt).scalars().all()

    results: list[StaleTaskDiagnostic] = []
    for task in rows:
        last_alive = (
            task.heartbeat_at
            or task.updated_at
            or task.started_at
            or task.created_at
        )
        is_stale = False
        stale_reason: str | None = None

        if last_alive is not None:
            age = (now - last_alive).total_seconds()
            if age > heartbeat_timeout_seconds:
                is_stale = True
                stale_reason = "heartbeat_timeout"
        else:
            is_stale = True
            stale_reason = "process_lost"

        # Stage-budget check (independent of heartbeat).
        if (
            task.stage_started_at is not None
            and task.stage_budget_seconds is not None
        ):
            stage_deadline = task.stage_started_at + timedelta(
                seconds=task.stage_budget_seconds + stage_budget_grace_seconds
            )
            if now > stage_deadline:
                is_stale = True
                if stale_reason is None:
                    stale_reason = "stage_budget_exceeded"

        results.append(
            StaleTaskDiagnostic(
                task_id=task.id,
                status=task.status,
                heartbeat_at=task.heartbeat_at,
                last_progress_at=task.last_progress_at,
                stage_started_at=task.stage_started_at,
                stage_budget_seconds=task.stage_budget_seconds,
                is_stale=is_stale,
                stale_reason=stale_reason,
            )
        )
    return results


def recover_stale_tasks(
    db_session,
    *,
    heartbeat_timeout_seconds: int = 90,
    stage_budget_grace_seconds: int = 300,
    task_types: tuple[str, ...] | None = None,
) -> list[dict]:
    """Recover zombie tasks by marking expired ``running`` tasks as ``failed``.

    Each returned dict contains:
    ``task_id`` / ``previous_status`` / ``new_status`` /
    ``stale_reason`` / ``finished_at``.

    When *task_types* includes ``"factor_pipeline"`` the corresponding
    warehouse lock is also released (best-effort) so the next pipeline
    run is not blocked by a dead process's lockfile.
    """
    from sqlalchemy import select

    from app.models.async_task import AsyncTaskRecord

    diagnostics = diagnose_stale_tasks(
        db_session,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        stage_budget_grace_seconds=stage_budget_grace_seconds,
    )
    stale = [d for d in diagnostics if d.is_stale]
    if not stale:
        return []

    stale_ids = {d.task_id for d in stale}
    reason_map = {d.task_id: d.stale_reason for d in stale}

    stmt = select(AsyncTaskRecord).where(
        AsyncTaskRecord.id.in_(stale_ids),
        AsyncTaskRecord.status == "running",
    )
    if task_types is not None:
        stmt = stmt.where(AsyncTaskRecord.task_type.in_(task_types))

    tasks = db_session.execute(stmt).scalars().all()

    # Determine whether warehouse lock cleanup is needed.
    release_lock = False
    if task_types is None or "factor_pipeline" in (task_types or ()):
        release_lock = any(
            t.task_type == "factor_pipeline" for t in tasks
        )

    now = _utcnow_naive()
    recovery_records: list[dict] = []
    for task in tasks:
        previous_status = task.status
        task.status = "failed"
        task.stage = "failed"
        task.finished_at = now
        task.message = (
            f"Recovered as stale: {reason_map.get(task.id, 'unknown')}"
        )
        recovery_records.append(
            {
                "task_id": task.id,
                "previous_status": previous_status,
                "new_status": "failed",
                "stale_reason": reason_map.get(task.id),
                "finished_at": now.isoformat(),
            }
        )

    db_session.commit()

    # Best-effort warehouse lock cleanup for recovered factor_pipeline tasks.
    if release_lock and recovery_records:
        try:
            from app.services.factors.config import get_factor_system_config

            config = get_factor_system_config(db_session)
            cleanup_stale_warehouse_lock(Path(config.warehouse_path))
        except Exception:
            pass  # Non-fatal: lock cleanup is advisory.

    return recovery_records


# ---------------------------------------------------------------------------
# Full diagnostics script
# ---------------------------------------------------------------------------


def run_warehouse_lock_diagnostics(warehouse_path: Path) -> dict[str, Any]:
    """Produce a complete JSON-serialisable diagnostics report.

    Includes warehouse file metadata, lock state, related processes
    (when psutil is available) and DuckDB health.
    """
    # Lazy import to avoid circular dependency with store.py.
    from app.services.factors.store import FactorWarehouse

    lock_info = diagnose_warehouse_lock(warehouse_path)

    # --- Related processes (best-effort) -------------------------------
    related_processes: list[dict] = []

    # Lock owner process details.
    if lock_info.lock_owner_pid is not None and _PSUTIL_AVAILABLE:
        try:
            proc = psutil.Process(lock_info.lock_owner_pid)
            related_processes.append(
                {
                    "pid": proc.pid,
                    "name": proc.name(),
                    "create_time": datetime.fromtimestamp(
                        proc.create_time(), tz=timezone.utc
                    ).isoformat(),
                    "cmdline": (
                        " ".join(proc.cmdline()[:10])
                        if proc.cmdline()
                        else None
                    ),
                    "relation": "lock_owner",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            pass

    # Scan for processes that have the warehouse file open (best-effort).
    if _PSUTIL_AVAILABLE:
        try:
            wh_name = warehouse_path.name
            seen_pids: set[int] = {p["pid"] for p in related_processes}
            for proc in psutil.process_iter(["pid", "name"]):
                if proc.pid in seen_pids or proc.pid == os.getpid():
                    continue
                try:
                    for f in proc.open_files():
                        if wh_name in f.path:
                            related_processes.append(
                                {
                                    "pid": proc.pid,
                                    "name": proc.info.get("name"),
                                    "open_file": f.path,
                                    "relation": "open_file",
                                }
                            )
                            seen_pids.add(proc.pid)
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

    # --- DuckDB health -------------------------------------------------
    duckdb_health: dict[str, Any]
    try:
        warehouse = FactorWarehouse(warehouse_path)
        duckdb_health = warehouse.health().to_dict()
    except Exception as exc:
        duckdb_health = {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    # --- File metadata -------------------------------------------------
    file_size = None
    file_mtime = None
    try:
        stat = warehouse_path.stat()
        file_size = stat.st_size
        file_mtime = datetime.fromtimestamp(
            stat.st_mtime
        ).isoformat()
    except OSError:
        pass

    return {
        "warehouse_path": str(warehouse_path),
        "file_size": file_size,
        "file_mtime": file_mtime,
        "lock_info": {
            "path": lock_info.path,
            "is_locked": lock_info.is_locked,
            "lock_owner_pid": lock_info.lock_owner_pid,
            "lock_owner_process": lock_info.lock_owner_process,
            "lock_acquired_at": (
                lock_info.lock_acquired_at.isoformat()
                if lock_info.lock_acquired_at
                else None
            ),
            "lock_age_seconds": lock_info.lock_age_seconds,
            "diagnostic_method": lock_info.diagnostic_method,
            "notes": list(lock_info.notes),
        },
        "related_processes": related_processes,
        "duckdb_health": duckdb_health,
        "diagnostic_timestamp": _utcnow_naive().isoformat(),
        "psutil_available": _PSUTIL_AVAILABLE,
    }


__all__ = [
    "StaleTaskDiagnostic",
    "WarehouseLockAcquisition",
    "WarehouseLockInfo",
    "WarehouseLockUnavailable",
    "acquire_warehouse_lock",
    "cleanup_stale_warehouse_lock",
    "diagnose_stale_tasks",
    "diagnose_warehouse_lock",
    "recover_stale_tasks",
    "release_warehouse_lock",
    "run_warehouse_lock_diagnostics",
]
