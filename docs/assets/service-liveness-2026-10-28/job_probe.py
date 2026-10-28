from pathlib import Path
import ctypes
from ctypes import wintypes
import json,os,sys,time

class Limits(ctypes.Structure):
    _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',wintypes.DWORD),
        ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active_limit',wintypes.DWORD),
        ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('schedule',wintypes.DWORD),
        ('io',ctypes.c_uint64*6),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
        ('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]

def inspect_job():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype=wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD,wintypes.LPVOID]
    kernel.QueryInformationJobObject.restype=wintypes.BOOL
    job=wintypes.BOOL()
    ok=kernel.IsProcessInJob(kernel.GetCurrentProcess(),None,ctypes.byref(job))
    limits=Limits()
    query=bool(kernel.QueryInformationJobObject(None,9,ctypes.byref(limits),ctypes.sizeof(limits),None)) if job.value else False
    return {'pid':os.getpid(),'parent':os.getppid(),'query_ok':bool(ok),'in_job':bool(job.value),
        'limits_ok':query,'limits_error':ctypes.get_last_error() if job.value and not query else None,
        'flags':hex(limits.flags),'kill_on_close':bool(limits.flags&0x2000),
        'breakaway_ok':bool(limits.flags&0x800),'silent_breakaway_ok':bool(limits.flags&0x1000)}

if __name__=='__main__':
    value=inspect_job()
    print(json.dumps(value),flush=True)
    if len(sys.argv)>1:
        Path(sys.argv[1]).write_text(json.dumps(value),encoding='utf-8')
    if len(sys.argv)>2:
        time.sleep(float(sys.argv[2]))
