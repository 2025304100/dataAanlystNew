"""Explicitly enabled native Scheduler/Job test; temporary health server only."""
from ctypes import wintypes
from pathlib import Path
import ctypes
import json
import os
import subprocess
import sys
import time
from urllib.request import build_opener,ProxyHandler
from uuid import uuid4

import psutil
import pytest

from scripts import validation_service_host as host

pytestmark=[pytest.mark.whitebox,pytest.mark.skipif(os.name!='nt' or
    os.environ.get('VALIDATION_SERVICE_TASK_NATIVE')!='1',reason='Explicit native Task Scheduler acceptance')]


def test_scheduler_owned_listener_survives_caller_job_close(tmp_path,monkeypatch,record_property):
    bridge=host.ROOT/'scripts/validation_service_task.ps1'
    action_root=tmp_path/'action'
    (action_root/'scripts').mkdir(parents=True)
    ready=tmp_path/'ready.json'
    stop=tmp_path/'stop'
    action_root.joinpath('scripts/validation_service_host.py').write_text('''from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import json,os,time
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'ok')
 def log_message(self,*args):pass
server=HTTPServer(('127.0.0.1',0),Handler);server.timeout=.1
Path(READY).write_text(json.dumps({'pid':os.getpid(),'port':server.server_port}))
deadline=time.monotonic()+120
while not Path(STOP).exists() and time.monotonic()<deadline:server.handle_request()
server.server_close()
'''.replace('READY',repr(str(ready))).replace('STOP',repr(str(stop))),encoding='utf-8')
    monkeypatch.setattr(host,'ROOT',action_root)
    monkeypatch.setattr(host,'TASK_NAME','ValidationServices-'+uuid4().hex[:12])
    monkeypatch.setattr(host,'DESCRIPTION','dataAanlystNew temporary liveness acceptance | '+str(action_root))
    request=tmp_path/'task.json'
    host.write(request,host.request_definition())
    def task(action):
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(bridge),
            '-Action',action,'-RequestPath',str(request)],capture_output=True,text=True,timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        value=json.loads(result.stdout.strip().splitlines()[-1])
        assert result.returncode==0 and value['ok'],value
        return value
    class Limits(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',wintypes.DWORD),
            ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active_limit',wintypes.DWORD),
            ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('schedule',wintypes.DWORD),
            ('io',ctypes.c_uint64*6),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
            ('peak_process_memory',ctypes.c_size_t),('peak_job_memory',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[wintypes.LPVOID,wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype=wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD]
    kernel.SetInformationJobObject.restype=wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    job=None;caller=None;registered=False
    go=tmp_path/'go'
    caller_code=tmp_path/'caller.py'
    caller_code.write_text('''from pathlib import Path
import subprocess,time
while not Path(GO).exists():time.sleep(.02)
subprocess.run(COMMAND,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
time.sleep(120)
'''.replace('GO',repr(str(go))).replace('COMMAND',repr(['powershell.exe','-NoProfile','-NonInteractive','-File',str(bridge),
        '-Action','Run','-RequestPath',str(request)])),encoding='utf-8')
    try:
        task('Register');registered=True
        job=kernel.CreateJobObjectW(None,None)
        limits=Limits();limits.flags=0x2000
        assert job and kernel.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits))
        caller=subprocess.Popen([sys.executable,str(caller_code)],creationflags=subprocess.CREATE_NO_WINDOW)
        assert kernel.AssignProcessToJobObject(job,int(caller._handle)),ctypes.get_last_error()
        go.touch()
        deadline=time.monotonic()+15
        while not ready.exists():
            assert caller.poll() is None
            assert time.monotonic()<deadline
            time.sleep(.05)
        identity=json.loads(ready.read_text())
        actual=psutil.Process(identity['pid'])
        created=actual.create_time()
        kernel.CloseHandle(job);job=None
        caller.wait(timeout=5)
        for _ in range(3):
            with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{identity['port']}/health",timeout=2) as response:
                assert response.status==200
            assert psutil.Process(identity['pid']).create_time()==created
            time.sleep(.2)
        record_property('scheduler_listener_survived_job_close',True)
        record_property('temporary_task_name',host.TASK_NAME)
        host.write(tmp_path/'native-result.json',{'passed':True,'task':host.TASK_NAME,'pid':identity['pid'],
            'created':created,'caller_exited':True,'health_checks':3})
    finally:
        if job:kernel.CloseHandle(job)
        if caller is not None and caller.poll() is None:
            caller.terminate();caller.wait(timeout=5)
        stop.touch()
        if registered:
            deadline=time.monotonic()+10
            while task('Query')['state']==4:
                assert time.monotonic()<deadline
                time.sleep(.2)
            task('Remove')
            assert task('Query')['registered'] is False
