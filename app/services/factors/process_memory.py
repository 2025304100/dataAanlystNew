"""RSS measurement without optional packages; unsupported platforms fail closed."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time


class WindowsProcessJob:
    """Own the whole worker tree before its initially suspended entry runs."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", BasicLimits), ("IO", ctypes.c_ulonglong * 6),
                ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            failure = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(failure)

    def assign_and_resume(self, process):
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        # CPython's Popen owns this native process handle. It remains valid
        # even when the launcher's numeric PID disappears.
        if not kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        process._factor_job = self

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            available = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel.OpenThread(2, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        return
                    finally:
                        kernel.CloseHandle(thread)
                available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Cannot find suspended factor worker thread")
        finally:
            kernel.CloseHandle(snapshot)

    def terminate(self):
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        if not kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_longlong * 4), ("PageFaults", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD),
                ("TerminatedProcesses", wintypes.DWORD)]

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            accounting = Accounting()
            if not kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.ActiveProcesses == 0:
                return
            time.sleep(0.01)
        raise OSError("Factor process job did not terminate")

    def process_ids(self) -> set[int]:
        ctypes, wintypes, kernel = self.ctypes, self.wintypes, self.kernel
        capacity = 32
        while capacity <= 65536:
            class ProcessList(ctypes.Structure):
                _fields_ = [("Assigned", wintypes.DWORD), ("Listed", wintypes.DWORD),
                            ("Ids", ctypes.c_size_t * capacity)]
            result = ProcessList()
            if kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(result), ctypes.sizeof(result), None):
                return {int(result.Ids[index]) for index in range(result.Listed)}
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
            capacity = max(capacity * 2, int(result.Assigned) + 16)
        raise OSError('Factor worker job exceeds process enumeration budget')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def process_is_running(pid: int) -> bool:
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith('linux'):
        try:
            status = Path(f'/proc/{int(pid)}/stat').read_text(encoding='ascii')
        except FileNotFoundError:
            return False
        return status.rsplit(')', 1)[1].strip().split()[0] != 'Z'
    raise OSError('Process liveness supervision is unsupported on this platform')


def terminate_windows_process(pid: int) -> None:
    """Terminate one owned descendant and wait for its kernel handle to exit."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0001 | 0x00100000, False, int(pid))
    if not handle:
        failure = ctypes.get_last_error()
        if failure == 87:  # The PID already exited.
            return
        if failure == 5:
            # Windows can deny TERMINATE for a process that has already
            # exited while still exposing its signalled synchronization
            # handle. Do not confuse that with a live inaccessible worker.
            exited_handle = kernel.OpenProcess(0x00100000, False, int(pid))
            if exited_handle:
                try:
                    if kernel.WaitForSingleObject(exited_handle, 0) == 0:
                        return
                finally:
                    kernel.CloseHandle(exited_handle)
        raise ctypes.WinError(failure)
    try:
        if not kernel.TerminateProcess(handle, 1):
            if kernel.WaitForSingleObject(handle, 0) != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 5000) != 0:
            raise OSError("Factor worker descendant did not terminate")
    finally:
        kernel.CloseHandle(handle)


def process_tree_pids(pid: int) -> set[int]:
    """Include the Windows venv launcher and actual Python/BLAS descendants."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        parents = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
                raise ctypes.WinError(ctypes.get_last_error())
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise ctypes.WinError(ctypes.get_last_error())
                    break
        finally:
            kernel.CloseHandle(snapshot)
        found = {int(pid)}
        while True:
            expanded = found | {child for child, parent in parents.items() if parent in found}
            if expanded == found:
                return found
            found = expanded
    if sys.platform.startswith("linux"):
        found, pending = {int(pid)}, [int(pid)]
        while pending:
            parent = pending.pop()
            try:
                task_paths = list(Path(f"/proc/{parent}/task").iterdir())
            except FileNotFoundError:
                continue
            for task in task_paths:
                try:
                    children = (task / "children").read_text(encoding="ascii").split()
                except FileNotFoundError:
                    continue
                for child in map(int, children):
                    if child not in found:
                        found.add(child)
                        pending.append(child)
        return found
    raise OSError("Process tree supervision is unsupported on this platform")


def resident_memory_bytes(pid: int) -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, int(pid))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                raise ctypes.WinError(ctypes.get_last_error())
            return int(counters.WorkingSetSize)
        finally:
            kernel.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        # statm is a kernel snapshot; field two is the resident page count.
        values = Path(f"/proc/{int(pid)}/statm").read_text(encoding="ascii").split()
        if len(values) < 2:
            raise OSError("Cannot obtain process resident memory")
        return int(values[1]) * os.sysconf("SC_PAGE_SIZE")
    raise OSError("Process RSS supervision is unsupported on this platform")
