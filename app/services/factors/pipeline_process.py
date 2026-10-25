"""Supervise one isolated factor process using the task's exact database."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from sqlalchemy import select

from app.models.async_task import AsyncTaskRecord
from app.services.async_tasks import _now, _set_task, is_worker_stop_requested
from app.services.factors.store import external_warehouse_worker
from app.services.factors.process_memory import resident_memory_bytes
from app.services.factors.process_memory import process_tree_pids, terminate_windows_process, WindowsProcessJob

TERMINAL = {"done", "partial", "failed", "cancelled", "interrupted"}
ROOT = Path(__file__).resolve().parents[3]


def _finish(factory, task_id, status, code, message, *, exit_code=None):
    with factory() as db:
        task = db.scalar(select(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id).with_for_update())
        if task is None or task.status in TERMINAL or task.is_terminal_locked:
            return
        _set_task(db, task_id, status=status, stage=status, finished_at=_now(), heartbeat_at=_now(),
            message=message, errors_json=json.dumps([{"code": code, "error_code": code, "severity": "error", "category": "runtime",
                "title_zh": message, "detail_zh": message, "retryable": True,
                "evidence": {"exit_code": exit_code, "worker_isolation": "process", "request_parameters_preserved": True}}], ensure_ascii=False))


def _stop_process(process, known_pids=()):
    if getattr(process, '_factor_tree_stopped', False):
        return
    if sys.platform == "win32":
        job = getattr(process, '_factor_job', None)
        if job is not None:
            job.terminate()
            process.wait(timeout=5)
            process._factor_tree_stopped = True
            return
        # venv's launcher owns a second Python process. Killing only Popen.pid
        # leaves training alive. Direct kernel handles avoid taskkill's WMI
        # dependency, which can be unavailable to the application's account.
        members = {process.pid, *known_pids}
        for known in tuple(members):
            members.update(process_tree_pids(known))
        for member in members - {process.pid}:
            terminate_windows_process(member)
        if process.poll() is None:
            process.kill()
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    process._factor_tree_stopped = True


@contextmanager
def _owned_process(command, warehouse, log_path, environment, observed_pids):
    with external_warehouse_worker(warehouse, wait_for_worker=True), log_path.open("ab") as log:
        job = WindowsProcessJob() if sys.platform == 'win32' else None
        process = None
        try:
            process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | (4 if job else 0)),
                start_new_session=(sys.platform != "win32"))
            if job is not None:
                job.assign_and_resume(process)
            yield process
        finally:
            # Release the warehouse only after all observed descendants stop,
            # including when its launcher has already exited abnormally.
            try:
                if process is not None:
                    _stop_process(process, observed_pids)
            finally:
                if job is not None:
                    job.close()


def _record_resources(factory, task_id, resources):
    with factory() as db:
        task = db.scalar(select(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id).with_for_update())
        if task is None:
            return
        result = json.loads(task.result_json or "{}")
        result["process_resources"] = resources
        _set_task(db, task_id, result_json=json.dumps(result, ensure_ascii=False))


def supervise_factor_pipeline(task_id, *, session_factory, warehouse_path,
                              child_command=None, poll_seconds=0.5, cancel_grace_seconds=2.0,
                              timeout_seconds=None):
    """Keep native exits/OOM outside the Web process; never resize training.

    child_command is an internal process-adapter seam for deterministic crash
    acceptance tests, never exposed in a task payload or an HTTP parameter.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        raise ValueError("Invalid factor task identity")
    bind = session_factory.kw.get("bind")
    url = bind.engine.url if hasattr(bind, "engine") else bind.url
    if not url.database or (url.get_backend_name() == "sqlite" and url.database == ":memory:"):
        raise ValueError("Factor process requires an explicitly bound persistent database")
    database_url = url.render_as_string(hide_password=False)
    with session_factory() as db:
        task = db.get(AsyncTaskRecord, task_id)
        if task is None or task.task_type != "factor_pipeline":
            raise ValueError("Factor process task does not exist in the bound database")
        if task.status in TERMINAL or task.cancel_requested:
            if task.status not in TERMINAL:
                _finish(session_factory, task_id, "cancelled", "FACTOR_WORKER_CANCELLED", "训练任务已取消，未启动计算进程")
            return {"started": False, "exit_code": None}
        payload_digest = hashlib.sha256((task.payload_json or "{}").encode("utf-8")).hexdigest()
        payload = json.loads(task.payload_json or "{}")
    warehouse = Path(warehouse_path).expanduser().resolve()
    log_directory = warehouse.parent / ".factor-worker-logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"{task_id}.log"
    environment = {**os.environ, "FACTOR_PIPELINE_DATABASE_URL": database_url,
        "DATABASE_URL": database_url, "FACTOR_PIPELINE_PAYLOAD_SHA256": payload_digest,
        "FACTOR_PIPELINE_WAREHOUSE_PATH": str(warehouse), "FACTOR_WAREHOUSE_PATH": str(warehouse),
        "AUTO_UNIVERSE_SYNC_ON_STARTUP": "0", "AUTO_TRADE_CALENDAR_REFRESH": "0",
        "PYTHONIOENCODING": "utf-8"}
    command = child_command or [sys.executable, "-B", "-m", "app.services.factors.pipeline_worker", task_id]
    process = None
    cancelled_at = None
    interrupted = False
    timed_out = False
    memory_exceeded = False
    peak_rss = 0
    observed_pids = set()
    started = time.monotonic()
    timeout = float(timeout_seconds if timeout_seconds is not None else os.environ.get("FACTOR_PIPELINE_PROCESS_TIMEOUT_SECONDS", "21600"))
    if not 0 < timeout <= 604800:
        raise ValueError("Factor process timeout must be between zero and seven days")
    rss_limit_mb = int(os.environ.get("FACTOR_PIPELINE_RSS_LIMIT_MB", "4096"))
    if not 64 <= rss_limit_mb <= 131072:
        raise ValueError("Factor process RSS budget must be 64..131072 MiB")
    rss_limit = rss_limit_mb * 1024 * 1024
    try:
        # Validate monitor capability before spawning. Missing OS support never
        # silently changes an explicitly budgeted job into an unlimited job.
        resident_memory_bytes(os.getpid())
        process_tree_pids(os.getpid())
        with _owned_process(command, warehouse, log_path, environment, observed_pids) as process:
            with session_factory() as db:
                task = db.scalar(select(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id).with_for_update())
                if task and task.status not in TERMINAL:
                    _set_task(db, task_id, status="running", stage="initializing", started_at=task.started_at or _now(),
                        heartbeat_at=_now(), worker_thread_id=f"process:{process.pid}",
                        message="独立训练进程已启动，原始训练窗口保持不变")
            while process.poll() is None:
                try:
                    owned_job = getattr(process, '_factor_job', None)
                    members = owned_job.process_ids() if owned_job is not None else process_tree_pids(process.pid)
                    observed_pids.update(members)
                    rss = 0
                    for member in members:
                        try:
                            rss += resident_memory_bytes(member)
                        except OSError:
                            # A short-lived child may exit between snapshots;
                            # an existing but unreadable child is a hard error.
                            remaining = owned_job.process_ids() if owned_job is not None else process_tree_pids(process.pid)
                            if member in remaining and process.poll() is None:
                                raise
                except OSError:
                    if process.poll() is not None:
                        break
                    raise
                peak_rss = max(peak_rss, rss)
                if rss > rss_limit:
                    memory_exceeded = True
                    _stop_process(process, observed_pids)
                    break
                with session_factory() as db:
                    task = db.scalar(select(AsyncTaskRecord).where(AsyncTaskRecord.id == task_id).with_for_update())
                    requested = task is None or bool(task.cancel_requested) or task.status == "cancelled"
                    if task and task.status not in TERMINAL:
                        task.heartbeat_at = _now()
                        task.updated_at = _now()
                        db.commit()
                interrupted = interrupted or is_worker_stop_requested(task_id)
                if requested or interrupted:
                    cancelled_at = cancelled_at or time.monotonic()
                    if time.monotonic() - cancelled_at >= cancel_grace_seconds:
                        _stop_process(process, observed_pids)
                        break
                if time.monotonic() - started >= timeout:
                    timed_out = True
                    _stop_process(process, observed_pids)
                    break
                time.sleep(max(0.01, poll_seconds))
            exit_code = process.wait(timeout=5)
            _stop_process(process, observed_pids)
            _record_resources(session_factory, task_id, {
                "worker_pid": process.pid, "rss_limit_bytes": rss_limit, "peak_rss_bytes": peak_rss,
                "timeout_seconds": timeout, "rss_poll_seconds": max(0.01, poll_seconds),
                "requested_window_days": payload.get("window_days"),
                "requested_validation_days": payload.get("validation_days"),
                "automatically_resized": False,
                "memory_limit_exceeded": memory_exceeded, "exit_code": exit_code,
                "rss_scope": "process_tree",
                "observed_process_ids": sorted(observed_pids),
            })
            with session_factory() as db:
                current = db.get(AsyncTaskRecord, task_id)
                cancelled = cancelled_at is not None or bool(current and current.cancel_requested)
            if memory_exceeded:
                _finish(session_factory, task_id, "failed", "FACTOR_MEMORY_BUDGET_EXCEEDED", "训练进程超过RSS内存预算，已终止；原始训练窗口未自动缩小，未完成模型未启用", exit_code=exit_code)
            elif interrupted:
                _finish(session_factory, task_id, "failed", "FACTOR_WORKER_INTERRUPTED", "服务停止，独立训练进程已中止；没有自动缩窗或重试", exit_code=exit_code)
            elif cancelled:
                _finish(session_factory, task_id, "cancelled", "FACTOR_WORKER_CANCELLED", "独立训练进程已取消，已提交阶段保留，未完成阶段未标成功", exit_code=exit_code)
            elif timed_out:
                _finish(session_factory, task_id, "failed", "FACTOR_WORKER_TIMEOUT", "独立训练进程超过运行预算，已停止；训练参数保持不变", exit_code=exit_code)
            else:
                _finish(session_factory, task_id, "failed", "FACTOR_WORKER_PROCESS_EXITED" if exit_code else "FACTOR_WORKER_NO_RESULT",
                    "独立训练进程异常退出或未提交终态；请检查任务日志，原始训练窗口未改变", exit_code=exit_code)
            return {"started": True, "pid": process.pid, "exit_code": exit_code, "log_path": str(log_path)}
    except Exception:
        if process is not None:
            _stop_process(process, observed_pids)
        _finish(session_factory, task_id, "failed", "FACTOR_WORKER_LAUNCH_FAILED", "独立训练进程无法启动或监督失败；未切换数据库，也未缩小训练窗口")
        raise
    finally:
        if process is not None:
            _stop_process(process, observed_pids)
