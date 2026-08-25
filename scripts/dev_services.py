from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / "tmp" / "dev-services"
STATE_FILE = RUNTIME_DIR / "state.json"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173
HOST = "127.0.0.1"
MAX_LOG_BYTES = 20 * 1024 * 1024


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> None:
    ensure_runtime_dir()
    if not state:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists, but the current token cannot query it.
        return True
    except SystemError:
        # Python 3.12 on Windows can surface ERROR_ACCESS_DENIED from
        # os.kill(pid, 0) as SystemError instead of PermissionError.
        return os.name == "nt"
    except OSError:
        return False
    return True


def is_port_open(port: int, host: str = HOST, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(port: int, proc: subprocess.Popen[str], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_open(port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return is_port_open(port)


def wait_for_port_closed(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_port_open(port):
            return True
        time.sleep(0.25)
    return not is_port_open(port)


def append_log_banner(label: str, out_log: Path, err_log: Path) -> None:
    stamp = f"[{now_text()}] {label}"
    for path in (out_log, err_log):
        try:
            # A fresh launch gets a fresh log. Keeping old startup exceptions
            # in stderr makes the launcher look failed even after a later
            # process started successfully.
            if path.name == "backend.err.log":
                path.write_text("", encoding="utf-8")
            elif path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                path.write_text("", encoding="utf-8")
        except OSError:
            # A stale elevated process may still own the file. Startup will
            # report the process error; log maintenance must not hide it.
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {stamp} ===\n")


def launch_process(command: list[str], cwd: Path, out_log: Path, err_log: Path, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    append_log_banner(" ".join(command), out_log, err_log)
    stdout_handle = out_log.open("ab")
    stderr_handle = err_log.open("ab")
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": stdout_handle,
        "stderr": stderr_handle,
        "env": env or os.environ.copy(),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(command, **kwargs)
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return proc


def stop_pid(pid: int) -> bool:
    if not is_pid_running(pid):
        return True
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            return not is_pid_running(pid)
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return not is_pid_running(pid)
    return True


def prune_state(state: dict[str, Any]) -> dict[str, Any]:
    changed = False
    for name in list(state.keys()):
        record = state.get(name)
        pid = record.get("pid") if isinstance(record, dict) else None
        port = {"backend": BACKEND_PORT, "frontend": FRONTEND_PORT}.get(name)
        if (
            not isinstance(pid, int)
            or not is_pid_running(pid)
            or (port is not None and not is_port_open(port))
        ):
            state.pop(name, None)
            changed = True
    if changed:
        save_state(state)
    return state


def backend_command() -> list[str]:
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(BACKEND_PORT)]


def vite_command() -> list[str]:
    return ["node", "./node_modules/vite/bin/vite.js", "--config", "vite.codex.config.ts", "--host", HOST, "--port", str(FRONTEND_PORT)]


def standalone_command() -> list[str]:
    return ["node", "frontend/scripts/universe-standalone-server.mjs"]


def service_logs(name: str) -> tuple[Path, Path]:
    return RUNTIME_DIR / f"{name}.out.log", RUNTIME_DIR / f"{name}.err.log"


def record_for(name: str, pid: int, port: int, mode: str, cwd: Path, command: list[str], out_log: Path, err_log: Path) -> dict[str, Any]:
    return {
        "name": name,
        "pid": pid,
        "port": port,
        "mode": mode,
        "cwd": str(cwd),
        "command": command,
        "started_at": now_text(),
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }


def start_backend(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    record = state.get("backend")
    if isinstance(record, dict) and is_pid_running(record.get("pid")) and is_port_open(BACKEND_PORT):
        print(f"backend already running on http://{HOST}:{BACKEND_PORT}/ (pid={record['pid']})")
        return record, False
    if is_port_open(BACKEND_PORT):
        raise RuntimeError(f"port {BACKEND_PORT} is already in use by an unmanaged process")

    out_log, err_log = service_logs("backend")
    proc = launch_process(backend_command(), ROOT, out_log, err_log)
    # Backend startup performs MySQL initialization and may take longer than
    # the frontend. Avoid killing a healthy process while it is still warming
    # up on a cold database connection.
    if not wait_for_port(BACKEND_PORT, proc, timeout=120.0):
        stop_pid(proc.pid)
        exit_code = proc.poll()
        raise RuntimeError(
            f"backend failed to start within 120s (pid={proc.pid}, exit_code={exit_code}, "
            f"port_open={is_port_open(BACKEND_PORT)}); check {err_log}"
        )

    record = record_for("backend", proc.pid, BACKEND_PORT, "uvicorn", ROOT, backend_command(), out_log, err_log)
    state["backend"] = record
    save_state(state)
    print(f"backend started on http://{HOST}:{BACKEND_PORT}/ (pid={proc.pid})")
    return record, True


def vite_env() -> dict[str, str]:
    env = os.environ.copy()
    esbuild_binary = ROOT / "frontend" / "node_modules" / "@esbuild" / "win32-x64" / "esbuild.exe"
    if esbuild_binary.exists():
        env.setdefault("ESBUILD_BINARY_PATH", str(esbuild_binary))
    return env


def try_start_frontend_vite(out_log: Path, err_log: Path) -> subprocess.Popen[str] | None:
    proc = launch_process(vite_command(), ROOT / "frontend", out_log, err_log, env=vite_env())
    if wait_for_port(FRONTEND_PORT, proc, timeout=8.0):
        return proc
    stop_pid(proc.pid)
    return None


def try_start_frontend_standalone(out_log: Path, err_log: Path) -> subprocess.Popen[str] | None:
    proc = launch_process(standalone_command(), ROOT, out_log, err_log)
    if wait_for_port(FRONTEND_PORT, proc, timeout=8.0):
        return proc
    stop_pid(proc.pid)
    return None


def start_frontend(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    record = state.get("frontend")
    if isinstance(record, dict) and is_pid_running(record.get("pid")) and is_port_open(FRONTEND_PORT):
        print(f"frontend already running on http://{HOST}:{FRONTEND_PORT}/ (pid={record['pid']}, mode={record.get('mode', '-')})")
        return record, False
    if is_port_open(FRONTEND_PORT):
        raise RuntimeError(f"port {FRONTEND_PORT} is already in use by an unmanaged process")

    out_log, err_log = service_logs("frontend")
    proc = try_start_frontend_vite(out_log, err_log)
    mode = "vite"
    if proc is None:
        proc = try_start_frontend_standalone(out_log, err_log)
        mode = "standalone"
    if proc is None:
        raise RuntimeError(f"frontend failed to start; check {err_log}")

    command = vite_command() if mode == "vite" else standalone_command()
    cwd = ROOT / "frontend" if mode == "vite" else ROOT
    record = record_for("frontend", proc.pid, FRONTEND_PORT, mode, cwd, command, out_log, err_log)
    state["frontend"] = record
    save_state(state)
    print(f"frontend started on http://{HOST}:{FRONTEND_PORT}/ (pid={proc.pid}, mode={mode})")
    return record, True


def stop_services(state: dict[str, Any]) -> bool:
    next_state: dict[str, Any] = {}
    success = True
    for name, port in (("frontend", FRONTEND_PORT), ("backend", BACKEND_PORT)):
        record = state.get(name)
        pid = record.get("pid") if isinstance(record, dict) else None
        port_open = is_port_open(port)
        if not port_open:
            print(f"{name} is not running")
            continue
        if isinstance(pid, int) and is_pid_running(pid):
            requested = stop_pid(pid)
            closed = wait_for_port_closed(port, timeout=15.0)
            if closed:
                print(f"stopped {name} (pid={pid})")
            else:
                details: list[str] = []
                if not requested:
                    details.append("terminate request failed")
                if not closed:
                    details.append("port still busy")
                print(f"failed to stop {name} (pid={pid}): {', '.join(details)}")
                success = False
                if isinstance(record, dict):
                    next_state[name] = record
        elif port_open:
            print(f"{name} is busy on port {port}, but it is not managed by this script")
            success = False
        else:
            print(f"{name} is not running")
    save_state(next_state)
    return success


def print_status(state: dict[str, Any]) -> None:
    state = prune_state(state)
    for name, port in (("backend", BACKEND_PORT), ("frontend", FRONTEND_PORT)):
        record = state.get(name)
        if (
            isinstance(record, dict)
            and is_pid_running(record.get("pid"))
            and is_port_open(port)
        ):
            print(f"{name:8} running  pid={record['pid']} port={port} mode={record.get('mode', '-')}")
            print(f"         logs: {record.get('stdout_log')} | {record.get('stderr_log')}")
        elif is_port_open(port):
            print(f"{name:8} busy     port={port} (occupied by an unmanaged process)")
        else:
            print(f"{name:8} stopped  port={port}")
    entry = f"http://{HOST}:{FRONTEND_PORT}/" if is_port_open(FRONTEND_PORT) else (f"http://{HOST}:{BACKEND_PORT}/" if is_port_open(BACKEND_PORT) else "-")
    docs = f"http://{HOST}:{BACKEND_PORT}/docs" if is_port_open(BACKEND_PORT) else "-"
    print(f"entry    {entry}")
    print(f"docs     {docs}")


def command_start() -> int:
    ensure_runtime_dir()
    state = prune_state(load_state())
    backend_started = False
    try:
        _, backend_started = start_backend(state)
        start_frontend(state)
        print_status(state)
        return 0
    except Exception as exc:
        if backend_started:
            backend = state.get("backend")
            pid = backend.get("pid") if isinstance(backend, dict) else None
            if isinstance(pid, int):
                stop_pid(pid)
            state.pop("backend", None)
            save_state(state)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def command_stop() -> int:
    ensure_runtime_dir()
    success = stop_services(load_state())
    print_status(load_state())
    return 0 if success else 1


def command_restart() -> int:
    ensure_runtime_dir()
    success = stop_services(load_state())
    if not success:
        print("ERROR: stop phase did not finish cleanly", file=sys.stderr)
        print_status(load_state())
        return 1
    time.sleep(1.0)
    return command_start()


def command_status() -> int:
    ensure_runtime_dir()
    print_status(load_state())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local frontend/backend dev services")
    parser.add_argument("command", choices=["start", "stop", "restart", "status"])
    args = parser.parse_args()

    if args.command == "start":
        return command_start()
    if args.command == "stop":
        return command_stop()
    if args.command == "restart":
        return command_restart()
    return command_status()


if __name__ == "__main__":
    raise SystemExit(main())
