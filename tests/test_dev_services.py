from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import dev_services


pytestmark = pytest.mark.whitebox


def test_service_launch_preserves_previous_stderr_and_opens_fresh_current_log(tmp_path):
    output, error = tmp_path / 'backend.out.log', tmp_path / 'backend.err.log'
    previous = b'prior process failure\r\n'
    error.write_bytes(previous)
    dev_services.append_log_banner('new isolated launch', output, error)
    archives = list(tmp_path.glob('backend.err.previous-*.log'))
    assert len(archives) == 1 and archives[0].read_bytes() == previous
    assert 'new isolated launch' in error.read_text(encoding='utf-8')
    assert 'prior process failure' not in error.read_text(encoding='utf-8')


def test_service_launch_keeps_old_log_if_archiving_is_denied(tmp_path, monkeypatch):
    from pathlib import Path
    output, error = tmp_path / 'backend.out.log', tmp_path / 'backend.err.log'
    error.write_text('keep exit evidence\n', encoding='utf-8')
    monkeypatch.setattr(Path, 'rename', lambda *_: (_ for _ in ()).throw(PermissionError()))
    dev_services.append_log_banner('new isolated launch', output, error)
    content = error.read_text(encoding='utf-8')
    assert 'keep exit evidence' in content and 'new isolated launch' in content
def test_admin_wrapper_requires_confirmed_identity_and_never_falls_back_to_port_type():
    script = (dev_services.ROOT / "scripts" / "dev_services_admin.ps1").read_text(encoding="utf-8")

    # Behavioral selection cases are exercised against the pure PowerShell
    # policy in test_dev_services_process_policy.py, without UAC or processes.
    assert "dev_services_process_policy.ps1" in script
    assert "Test-ProjectServiceProcess -Process $inspected" in script
    assert "Test-ProjectServiceProcess -Process $currentProcess" in script
    assert "Get-NetTCPConnection -State Listen -LocalPort 8000, 5173" in script
    assert "refusing to stop unconfirmed process" in script
    assert "$isExpectedOwner" not in script
    assert "/T /F" not in script
    assert "taskkill.exe /PID $pidValue /F" in script
    assert "Stop-Process -Id $pidValue -Force" in script
    assert '$ErrorActionPreference = "Continue"' in script
    assert "Test-LocalPortOpen -Port 8000" in script
    assert "Test-LocalPortOpen -Port 5173" in script


@pytest.mark.parametrize("entrypoint", ["start.bat", "restart.bat", "stop.bat"])
def test_service_entrypoints_use_admin_wrapper(entrypoint):
    content = (dev_services.ROOT / entrypoint).read_text(encoding="utf-8")

    assert "dev_services_admin.ps1" in content


@pytest.mark.parametrize("entrypoint", ["start.bat", "restart.bat"])
def test_start_entrypoints_launch_manager_after_admin_cleanup(entrypoint):
    content = (dev_services.ROOT / entrypoint).read_text(encoding="utf-8")

    assert "scripts\\dev_services.py start" in content
    assert "C:\\Python312\\python.exe" in content


def test_admin_wrapper_does_not_launch_elevated_services():
    script = (dev_services.ROOT / "scripts" / "dev_services_admin.ps1").read_text(encoding="utf-8")

    assert '"scripts/dev_services.py" $managerAction' not in script
    assert "Returning to normal user mode for startup" in script


def test_posix_is_pid_running_returns_false_for_missing_process(monkeypatch):
    monkeypatch.setattr(
        dev_services, "os",
        SimpleNamespace(
            name="posix",
            kill=lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
        ),
    )

    assert dev_services.is_pid_running(12345) is False


def test_posix_is_pid_running_treats_permission_error_as_running(monkeypatch):
    monkeypatch.setattr(
        dev_services, "os",
        SimpleNamespace(
            name="posix",
            kill=lambda _pid, _signal: (_ for _ in ()).throw(PermissionError()),
        ),
    )

    assert dev_services.is_pid_running(12345) is True


@pytest.mark.parametrize("error, running", [(5, True), (87, False)])
def test_windows_unqueryable_pid_is_preserved_only_for_access_denied(monkeypatch, error, running):
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=None),
        WaitForSingleObject=Mock(),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(dev_services, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(dev_services.ctypes, "WinDLL", lambda *_a, **_kw: kernel32, raising=False)
    monkeypatch.setattr(dev_services.ctypes, "get_last_error", lambda: error, raising=False)

    assert dev_services.is_pid_running(12345) is running


@pytest.mark.parametrize("wait_result, running", [(0x102, True), (0, False), (0xFFFFFFFF, False)])
def test_windows_pid_query_releases_handle_for_every_wait_result(monkeypatch, wait_result, running):
    closed_handles = []
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=321),
        WaitForSingleObject=Mock(return_value=wait_result),
        CloseHandle=Mock(side_effect=lambda handle: closed_handles.append(handle)),
    )
    monkeypatch.setattr(dev_services, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(dev_services.ctypes, "WinDLL", lambda *_a, **_kw: kernel32, raising=False)

    assert dev_services.is_pid_running(12345) is running
    assert closed_handles == [321]


def test_stop_services_clears_stale_pid_when_port_is_closed(monkeypatch):
    stopped_pids: list[int] = []
    saved_states: list[dict] = []
    monkeypatch.setattr(dev_services, "is_port_open", lambda _port: False)
    monkeypatch.setattr(dev_services, "is_pid_running", lambda _pid: True)
    monkeypatch.setattr(dev_services, "stop_pid", lambda pid: stopped_pids.append(pid) or True)
    monkeypatch.setattr(dev_services, "save_state", lambda state: saved_states.append(dict(state)))

    result = dev_services.stop_services({
        "frontend": {"pid": 32616},
        "backend": {"pid": 24496},
    })

    assert result is True
    assert stopped_pids == []
    assert saved_states == [{}]


def test_prune_state_removes_record_when_managed_port_is_closed(monkeypatch):
    saved_states: list[dict] = []
    monkeypatch.setattr(dev_services, "is_pid_running", lambda _pid: True)
    monkeypatch.setattr(dev_services, "is_port_open", lambda _port: False)
    monkeypatch.setattr(dev_services, "save_state", lambda state: saved_states.append(dict(state)))

    state = dev_services.prune_state({"backend": {"pid": 24496}})

    assert state == {}
    assert saved_states == [{}]


@pytest.mark.skipif(os.name != "nt", reason="Windows detached-process lifecycle")
@pytest.mark.parametrize("entrypoint", ["pid", "status"])
def test_windows_status_checks_keep_detached_managed_process_alive(tmp_path, entrypoint):
    ready = tmp_path / "ready.json"
    child_code = (
        "import json,pathlib,socket,sys,time; "
        "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'port':s.getsockname()[1]})); "
        "time.sleep(60)"
    )
    proc = dev_services.launch_process(
        [sys.executable, "-u", "-c", child_code, str(ready)],
        tmp_path, tmp_path / "child.out.log", tmp_path / "child.err.log",
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert proc.poll() is None, "isolated child exited before becoming ready"
            assert time.monotonic() < deadline, "isolated child did not become ready"
            time.sleep(0.025)
        port = json.loads(ready.read_text())["port"]
        runtime = tmp_path / "state"
        checker_code = (
            "import sys; from pathlib import Path; from scripts import dev_services as d; "
            f"d.RUNTIME_DIR=Path({str(runtime)!r}); d.STATE_FILE=d.RUNTIME_DIR/'state.json'; "
            # Both probes are restricted to this test's own ephemeral listener.
            f"d.BACKEND_PORT={port}; d.FRONTEND_PORT={port}; "
            f"d.save_state({{'backend': {{'pid': {proc.pid}, 'mode': 'test-sleep'}}}}); "
            + (
                f"print(d.is_pid_running({proc.pid}))"
                if entrypoint == "pid"
                else "sys.argv=['dev_services.py','status']; raise SystemExit(d.main())"
            )
        )
        checker = subprocess.run(
            [sys.executable, "-c", checker_code], cwd=dev_services.ROOT,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        assert checker.returncode == 0, checker.stderr
        # The process handle is the independent source of truth; asking the
        # launcher a second time would repeat the bug and cannot prove safety.
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.3)
        expected = "True" if entrypoint == "pid" else f"running  pid={proc.pid}"
        assert expected in checker.stdout
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)
        assert dev_services.is_pid_running(proc.pid) is False
