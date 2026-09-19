from __future__ import annotations

import pytest

from scripts import dev_services


def test_admin_wrapper_cleans_every_supported_service_mode():
    script = (dev_services.ROOT / "scripts" / "dev_services_admin.ps1").read_text(encoding="utf-8")

    assert "app\\.main:app" in script
    assert "vite\\.codex\\.config\\.ts" in script
    assert "universe-standalone-server\\.mjs" in script
    assert "Get-NetTCPConnection -State Listen -LocalPort 8000, 5173" in script
    assert '$connection.LocalPort -eq 5173 -and $ownerProcess.Name -ieq "node.exe"' in script
    assert '$connection.LocalPort -eq 8000 -and $ownerProcess.Name -ieq "python.exe"' in script
    assert "Process-tree termination was incomplete" in script
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


def test_is_pid_running_returns_false_for_missing_process(monkeypatch):
    monkeypatch.setattr(
        dev_services.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert dev_services.is_pid_running(12345) is False


def test_is_pid_running_treats_permission_error_as_running(monkeypatch):
    monkeypatch.setattr(
        dev_services.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(PermissionError()),
    )

    assert dev_services.is_pid_running(12345) is True


def test_is_pid_running_handles_windows_python_system_error(monkeypatch):
    monkeypatch.setattr(
        dev_services.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(SystemError("access denied")),
    )
    monkeypatch.setattr(dev_services.os, "name", "nt")

    assert dev_services.is_pid_running(12345) is True


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
