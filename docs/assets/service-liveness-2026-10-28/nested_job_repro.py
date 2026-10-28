"""Reproduce only on owned diagnostic processes, never project services."""
from pathlib import Path
from ctypes import wintypes
import ctypes,json,os,subprocess,sys,time
import psutil
from job_probe import Limits

folder=Path(__file__).resolve().parent
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.CreateJobObjectW.argtypes=[wintypes.LPVOID,wintypes.LPCWSTR]
kernel.CreateJobObjectW.restype=wintypes.HANDLE
kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
kernel.SetInformationJobObject.restype=wintypes.BOOL
kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
kernel.AssignProcessToJobObject.restype=wintypes.BOOL
kernel.CloseHandle.argtypes=[wintypes.HANDLE]
kernel.CloseHandle.restype=wintypes.BOOL
if len(sys.argv)>1 and sys.argv[1]=='child':
    sys.path.insert(0,str(folder.parents[1]))
    from scripts import dev_services
    go=folder/'nested-go'
    deadline=time.monotonic()+15
    while not go.exists():
        assert time.monotonic()<deadline
        time.sleep(.03)
    proc=dev_services.launch_process([sys.executable,str(folder/'job_probe.py'),str(folder/'nested-child.json'),'60'],
        folder,folder/'nested.out.log',folder/'nested.err.log')
    time.sleep(60)
    sys.exit(0)

outer=kernel.CreateJobObjectW(None,None)
inner=kernel.CreateJobObjectW(None,None)
assert outer and inner
proc=None
try:
    for handle,flags in ((outer,0x2000),(inner,0x2800)):
        limits=Limits(); limits.flags=flags
        assert kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)),ctypes.get_last_error()
    proc=subprocess.Popen([sys.executable,__file__,'child'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert kernel.AssignProcessToJobObject(outer,int(proc._handle)),ctypes.get_last_error()
    assert kernel.AssignProcessToJobObject(inner,int(proc._handle)),ctypes.get_last_error()
    (folder/'nested-go').touch()
    ready=folder/'nested-child.json'
    deadline=time.monotonic()+10
    while not ready.exists():
        assert proc.poll() is None
        assert time.monotonic()<deadline
        time.sleep(.03)
    value=json.loads(ready.read_text())
    child=psutil.Process(value['pid'])
    kernel.CloseHandle(inner);inner=None
    kernel.CloseHandle(outer);outer=None
    proc.wait(timeout=5)
    try:
        child.wait(timeout=5)
        killed=True
    except psutil.TimeoutExpired: killed=False
    result={'reproduced':killed,'parent_exit':proc.returncode,'child_policy':value,
        'description':'Existing launcher breaks immediate job but remains in outer kill-on-close job'}
    (folder/'nested-job-repro.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
finally:
    if inner:kernel.CloseHandle(inner)
    if outer:kernel.CloseHandle(outer)
    if proc is not None and proc.poll() is None:
        proc.terminate();proc.wait(timeout=5)
