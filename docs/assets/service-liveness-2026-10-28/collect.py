from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import ctypes
from ctypes import wintypes
import hashlib,json,os,sys
import psutil

folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
from scripts import dev_services

class Limits(ctypes.Structure):
    _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',wintypes.DWORD),
        ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active_limit',wintypes.DWORD),
        ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('schedule',wintypes.DWORD),
        ('io',ctypes.c_uint64*6),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
        ('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
kernel.OpenProcess.restype=wintypes.HANDLE
kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
kernel.IsProcessInJob.restype=wintypes.BOOL
kernel.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,wintypes.LPVOID]
kernel.QueryInformationJobObject.restype=wintypes.BOOL
kernel.CloseHandle.argtypes=[wintypes.HANDLE]
kernel.CloseHandle.restype=wintypes.BOOL
limits=Limits()
query_ok=bool(kernel.QueryInformationJobObject(None,9,ctypes.byref(limits),ctypes.sizeof(limits),None))
query_error=ctypes.get_last_error() if not query_ok else None
records=[]
for pid in (os.getpid(),os.getppid(),69992,69712,18212,56128):
    item={'pid':pid}
    try:
        p=psutil.Process(pid)
        item.update(created=p.create_time(),parent=p.ppid(),exe=p.exe(),cwd=p.cwd(),command=p.cmdline())
        handle=kernel.OpenProcess(0x1000,False,pid)
        if handle:
            try:
                job=wintypes.BOOL()
                item['job_query_ok']=bool(kernel.IsProcessInJob(handle,None,ctypes.byref(job)))
                item['in_job']=bool(job.value)
            finally: kernel.CloseHandle(handle)
    except (psutil.NoSuchProcess,psutil.AccessDenied) as exc:
        item['error']=type(exc).__name__
    records.append(item)
checks=[]
for url in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    try:
        with build_opener(ProxyHandler({})).open(url,timeout=5) as response:
            checks.append({'url':url,'status':response.status,'validation_mode':response.headers.get('X-App-Validation-Mode')})
    except Exception as exc: checks.append({'url':url,'error':type(exc).__name__})
result={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'boot_time':psutil.boot_time(),
    'job_policy':{'query_ok':query_ok,'error':query_error,'size':ctypes.sizeof(limits),'flags':hex(limits.flags),
        'breakaway_ok':bool(limits.flags&0x800),'silent_breakaway_ok':bool(limits.flags&0x1000),
        'kill_on_close':bool(limits.flags&0x2000),'launcher_requests_breakaway':dev_services._windows_job_allows_breakaway()},
    'processes':records,'http':checks}
(folder/'runtime-before.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
(folder/'business-before.json').write_text(json.dumps(invariants(),ensure_ascii=False,indent=2),encoding='utf-8')
baseline={}
for name in ('app','scripts','tests'):
    for p in (root/name).rglob('*.py'):
        if '__pycache__' not in p.parts:
            baseline[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
(folder/'source-baseline.json').write_text(json.dumps(baseline,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
