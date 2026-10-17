"""Public managed launcher at the Win32/Popen boundary; starts no processes."""
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import dev_services


@pytest.mark.parametrize("in_job,is_job_ok,query_ok,limit_flags,allow_breakaway", [
    (True, True, True, 0x2800, True),
    (True, True, True, 0x2000, False),
    (True, True, True, 0x3000, False),
    (False, True, True, 0x2800, False),
    (True, False, True, 0x2800, False),
    (True, True, False, 0x2800, False),
])
def test_launcher_requests_job_breakaway_only_when_current_job_explicitly_allows_it(
    tmp_path, monkeypatch, in_job, is_job_ok, query_ok, limit_flags, allow_breakaway,
):
    def in_job_query(_process, _job, output):
        ctypes.cast(output, ctypes.POINTER(wintypes.BOOL)).contents.value = in_job
        return is_job_ok

    def limits_query(_job, information_class, output, _size, _returned):
        assert information_class == 9  # JobObjectExtendedLimitInformation
        # Public Win32 ABI: LimitFlags follows two 64-bit time-limit fields.
        ctypes.cast(output, ctypes.POINTER(wintypes.DWORD))[4] = limit_flags
        return query_ok

    kernel = SimpleNamespace(GetCurrentProcess=Mock(return_value=-1),
        IsProcessInJob=Mock(side_effect=in_job_query),
        QueryInformationJobObject=Mock(side_effect=limits_query))
    monkeypatch.setattr(dev_services.ctypes, "WinDLL", lambda *_a, **_kw: kernel, raising=False)
    monkeypatch.setattr(dev_services, "os", SimpleNamespace(name="nt", environ={}))
    popen = Mock(return_value=object())
    monkeypatch.setattr(dev_services.subprocess, "Popen", popen)
    dev_services.launch_process(["isolated-program"], tmp_path,
        tmp_path / "out.log", tmp_path / "err.log")
    flags = popen.call_args.kwargs["creationflags"]
    base = dev_services.subprocess.DETACHED_PROCESS | dev_services.subprocess.CREATE_NEW_PROCESS_GROUP
    expected = base | (0x01000000 if allow_breakaway else 0)
    assert flags == expected
    assert popen.call_count == 1


def test_launcher_preserves_flags_when_win32_job_query_is_unavailable(tmp_path, monkeypatch):
    def unavailable(*_a, **_kw):
        raise OSError("isolated Win32 query failure")
    monkeypatch.setattr(dev_services.ctypes, "WinDLL", unavailable, raising=False)
    monkeypatch.setattr(dev_services, "os", SimpleNamespace(name="nt", environ={}))
    popen = Mock(return_value=object())
    monkeypatch.setattr(dev_services.subprocess, "Popen", popen)
    dev_services.launch_process(["isolated-program"], tmp_path,
        tmp_path / "out.log", tmp_path / "err.log")
    assert popen.call_args.kwargs["creationflags"] == (
        dev_services.subprocess.DETACHED_PROCESS | dev_services.subprocess.CREATE_NEW_PROCESS_GROUP)


def test_posix_launcher_keeps_new_session_without_reading_windows_jobs(tmp_path, monkeypatch):
    kernel = Mock(side_effect=AssertionError("POSIX must not inspect Win32"))
    monkeypatch.setattr(dev_services.ctypes, "WinDLL", kernel, raising=False)
    monkeypatch.setattr(dev_services, "os", SimpleNamespace(name="posix", environ={}))
    popen = Mock(return_value=object())
    monkeypatch.setattr(dev_services.subprocess, "Popen", popen)
    dev_services.launch_process(["isolated-program"], tmp_path,
        tmp_path / "out.log", tmp_path / "err.log")
    assert popen.call_args.kwargs["start_new_session"] is True
    assert "creationflags" not in popen.call_args.kwargs
