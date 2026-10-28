"""On-demand Windows host for protected local validation services.

Task Scheduler owns this host, rather than a short-lived command executor Job.
No startup/logon trigger, passwords, business schedules, models or notifications.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4
import xml.etree.ElementTree as ET

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import dev_services

RUNTIME = ROOT / 'tmp' / 'validation-service-host'
TASK_NAME = 'ValidationServices-' + hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:12]
DESCRIPTION = 'dataAanlystNew protected validation host v1 | ' + str(ROOT)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def guard_idle():
    from scripts.restart_backend_validation import snapshot
    state, _ = snapshot()
    if state['revision'] != 'wps_0024_092_market_signals' or state['active_async'] or state['active_discovery']:
        raise RuntimeError('0092 and idle background work are required; no task is cancelled')
    if state['signal_counts']['market_signal_settings'] or state['monitor_count']:
        raise RuntimeError('Signal run settings and monitor must remain disabled')
    if any(row['status'] == 'sending' and row['total'] for row in state['outbox']):
        raise RuntimeError('An existing delivery must finish before service control')


def request_definition(runtime=RUNTIME):
    executable = str(Path(sys.executable).with_name('pythonw.exe'))
    if not Path(executable).is_file():
        raise RuntimeError('A windowless local Python runtime is required')
    args = subprocess.list2cmdline([str(ROOT/'scripts/validation_service_host.py'), 'host'])
    namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', namespace)
    def node(parent, name, value=None):
        child = ET.SubElement(parent, '{'+namespace+'}'+name)
        if value is not None: child.text = value
        return child
    task = ET.Element('{'+namespace+'}Task', version='1.4')
    node(node(task,'RegistrationInfo'),'Description',DESCRIPTION)
    node(task,'Triggers')
    principal = node(node(task,'Principals'),'Principal'); principal.set('id','Owner')
    node(principal,'UserId','CURRENT_USER')
    node(principal,'LogonType','InteractiveToken')
    node(principal,'RunLevel','LeastPrivilege')
    settings = node(task,'Settings')
    for key,value in {'MultipleInstancesPolicy':'IgnoreNew','DisallowStartIfOnBatteries':'false',
        'StopIfGoingOnBatteries':'false','StartWhenAvailable':'false','RunOnlyIfNetworkAvailable':'false',
        'AllowStartOnDemand':'true','Enabled':'true','Hidden':'true','ExecutionTimeLimit':'PT0S'}.items():
        node(settings,key,value)
    restart = node(settings,'RestartOnFailure')
    node(restart,'Interval','PT1M'); node(restart,'Count','3')
    actions = node(task,'Actions'); actions.set('Context','Owner')
    action = node(actions,'Exec')
    node(action,'Command',executable); node(action,'Arguments',args); node(action,'WorkingDirectory',str(ROOT))
    return {'task_name':TASK_NAME,'description':DESCRIPTION,'executable':executable,'arguments':args,
        'xml':ET.tostring(task,encoding='unicode')}


def scheduler(action):
    request = RUNTIME/'task-request.json'
    definition = request_definition()
    write(request,definition)
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',
        str(ROOT/'scripts/validation_service_task.ps1'),'-Action',action,'-RequestPath',str(request)],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    response = json.loads(lines[-1]) if lines else {'ok':False,'error_type':'SchedulerUnavailable'}
    if result.returncode or not response.get('ok'):
        raise RuntimeError('Task Scheduler action failed: '+response.get('error_type','unknown'))
    return response


def matches(record):
    try:
        process = psutil.Process(record['pid'])
        return (abs(process.create_time()-record['created']) < .001
            and Path(process.cwd()).resolve()==Path(record['cwd']).resolve()
            and process.cmdline()==record['command'])
    except (KeyError, psutil.Error, OSError):
        return False


def listener(port):
    return {c.pid for c in psutil.net_connections(kind='tcp')
        if c.status==psutil.CONN_LISTEN and c.laddr.port==port}


def controlled_children(process):
    """Only project descendants and their exact Windows console companion."""
    children=process.children(recursive=True)
    project={child.pid for child in children
        if Path(child.exe()).resolve().is_relative_to(ROOT/'frontend')}
    console=(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/conhost.exe').resolve()
    owned=[]
    for child in children:
        if child.pid in project:
            owned.append(child)
        elif Path(child.exe()).resolve()==console and child.ppid() in project:
            # The console host exits with its owning esbuild process. Never
            # terminate a system executable merely because its name matches.
            continue
        else:
            raise RuntimeError('Unexpected descendant; preserve all processes')
    return owned


@dataclass
class Spec:
    name: str
    command: list[str]
    cwd: Path
    port: int
    env: dict


class Supervisor:
    def __init__(self, specs, runtime, guard=guard_idle, existing=None, clock=time.time):
        self.specs = specs
        self.runtime = runtime
        self.runtime.mkdir(parents=True,exist_ok=True)
        self.guard = guard
        self.clock = clock
        self.records = existing or {}
        self.handles = {}
        self.failures = {spec.name:[] for spec in specs}
        self.retry_at = {}
        self.token = uuid4().hex
        self.status = 'starting'

    def log(self, event, **values):
        self.runtime.mkdir(parents=True,exist_ok=True)
        with (self.runtime/'events.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'time':stamp(),'event':event,**values},ensure_ascii=False)+'\n')

    def tick(self):
        now = self.clock()
        self.status = 'starting'
        for spec in self.specs:
            self.failures[spec.name] = [t for t in self.failures[spec.name] if t > now-300]
            old = self.records.get(spec.name)
            if old and matches(old):
                continue  # Never kill a live process for a transient health/port fault.
            if old:
                handle = self.handles.pop(spec.name,None)
                code = handle.poll() if handle is not None else None
                self.log('process_exit',service=spec.name,pid=old['pid'],created=old['created'],exit_code=code)
                self.records.pop(spec.name,None)
                self.failures[spec.name] = [t for t in self.failures[spec.name] if t > now-300] + [now]
                self.retry_at[spec.name] = now+min(2**len(self.failures[spec.name]),30)
            if listener(spec.port):
                self.status='blocked_port'
                continue
            if now < self.retry_at.get(spec.name,0) or len(self.failures[spec.name])>=5:
                self.status='backoff'
                continue
            try:
                self.guard()
                out = self.runtime/(spec.name+'.out.log')
                err = self.runtime/(spec.name+'.err.log')
                process = dev_services.launch_process(spec.command,spec.cwd,out,err,env=spec.env)
                actual = psutil.Process(process.pid)
                record = {'name':spec.name,'pid':process.pid,'created':actual.create_time(),
                    'cwd':str(spec.cwd),'command':spec.command,'port':spec.port,
                    'mode':'uvicorn' if spec.name=='backend' else 'vite','started_at':dev_services.now_text(),
                    'stdout_log':str(out),'stderr_log':str(err),'validation_mode':True,
                    'background_work_enabled':False,'managed_host_pid':os.getpid()}
                self.records[spec.name]=record
                self.handles[spec.name]=process
                self.log('process_start',service=spec.name,pid=process.pid,created=record['created'])
            except Exception as exc:
                self.status='blocked_guard'
                self.retry_at[spec.name]=now+10
                self.log('start_blocked',service=spec.name,error_type=type(exc).__name__)
        ready = len(self.records)==len(self.specs) and all(matches(self.records.get(s.name,{}))
            and listener(s.port)=={self.records[s.name]['pid']} for s in self.specs)
        if ready:self.status='ready'
        host = psutil.Process()
        value={'version':1,'status':self.status,'token':self.token,'heartbeat':time.time(),
            'host':{'pid':host.pid,'created':host.create_time(),'cwd':host.cwd(),'command':host.cmdline()},
            'services':self.records,'validation_mode':True,'updated_at':stamp()}
        write(self.runtime/'state.json',value)
        return value

    def stop_one(self,name):
        self.guard()
        record=self.records.get(name)
        if not record or not matches(record):return
        process=psutil.Process(record['pid'])
        children=controlled_children(process)
        for current in [process,*children]:
            try:
                current.terminate(); current.wait(timeout=8)
            except psutil.NoSuchProcess:pass
        self.records.pop(name,None)
        self.handles.pop(name,None)
        self.log('controlled_stop',service=name,pid=record['pid'])


def specs():
    from scripts.restart_backend_validation import snapshot
    _,url=snapshot()
    env=dict(os.environ,APP_VALIDATION_MODE='1',AUTO_TRADE_CALENDAR_REFRESH='0',
        AUTO_UNIVERSE_SYNC_ON_STARTUP='0',DATABASE_URL=url.render_as_string(hide_password=False),PYTHONIOENCODING='utf-8')
    backend=dev_services.backend_command()
    backend[0]=str(Path(sys.executable).with_name('python.exe'))
    node=read(RUNTIME/'executables.json').get('node')
    if not node or Path(node).name.casefold()!='node.exe' or not Path(node).is_file():
        raise RuntimeError('Confirmed Node runtime is required')
    frontend=dev_services.vite_command()
    frontend[0]=node
    return [Spec('backend',backend,ROOT,8000,env),
        Spec('frontend',frontend,ROOT/'frontend',5173,dev_services.vite_env())]


def adopt(service_specs):
    old=dev_services.load_state()
    confirmed={}
    for spec in service_specs:
        owners=listener(spec.port)
        if not owners:continue
        record=old.get(spec.name,{})
        if owners!={record.get('pid')}:raise RuntimeError('Occupied port has no confirmed owner')
        process=psutil.Process(record['pid'])
        actual={**record,'created':record.get('created',process.create_time())}
        if (not matches(actual) or actual['command'][1:]!=spec.command[1:]
            or Path(process.exe()).resolve()!=Path(spec.command[0]).resolve()
            or Path(actual['cwd']).resolve()!=spec.cwd.resolve()):
            raise RuntimeError('Existing service identity mismatch')
        if 'created' not in record:
            started=datetime.strptime(record['started_at'],'%Y-%m-%d %H:%M:%S').timestamp()
            if not -1 <= started-process.create_time() <= 120:raise RuntimeError('Stale service instance')
        if spec.name=='backend':
            with build_opener(ProxyHandler({})).open('http://127.0.0.1:8000/health',timeout=5) as response:
                if response.headers.get('X-App-Validation-Mode')!='1':raise RuntimeError('Existing backend is not protected')
        confirmed[spec.name]=actual
    return confirmed


def host():
    import msvcrt
    RUNTIME.mkdir(parents=True,exist_ok=True)
    with (RUNTIME/'host.lock').open('a+b') as lock:
        lock.write(b'0');lock.flush();lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return 0
        service_specs=specs()
        supervisor=Supervisor(service_specs,RUNTIME,existing=adopt(service_specs))
        supervisor.log('host_start',pid=os.getpid(),parent=os.getppid())
        while True:
            state=supervisor.tick()
            request=read(RUNTIME/'control.json')
            if request.get('token')==supervisor.token:
                try:
                    targets=request.get('services',[])
                    if request.get('action')=='stop':targets=[s.name for s in service_specs]
                    for name in targets:
                        if name not in ('backend','frontend'):raise RuntimeError('Invalid managed service')
                        supervisor.stop_one(name)
                    (RUNTIME/'control.json').unlink(missing_ok=True)
                    if request.get('action')=='stop':
                        state.update(status='stopped',services={},heartbeat=time.time())
                        write(RUNTIME/'state.json',state)
                        supervisor.log('host_stop',pid=os.getpid())
                        return 0
                except Exception as exc:
                    supervisor.log('control_blocked',error_type=type(exc).__name__)
                    (RUNTIME/'control.json').unlink(missing_ok=True)
            # Canonical manager state follows confirmed instances; no stale pruning.
            if supervisor.records:
                dev_services.save_state(supervisor.records)
            time.sleep(2)


def start():
    guard_idle()
    node=shutil.which('node')
    if not node:raise RuntimeError('Node runtime was not found')
    write(RUNTIME/'executables.json',{'node':str(Path(node).resolve())})
    scheduler('Register')
    scheduler('Run')
    deadline=time.monotonic()+55
    while time.monotonic()<deadline:
        state=read(RUNTIME/'state.json')
        if state.get('status')=='ready' and time.time()-state.get('heartbeat',0)<8 and matches(state.get('host',{})):
            return state
        time.sleep(.25)
    raise RuntimeError('Managed validation host did not become ready; preserve logs and processes')


def control(action, services):
    guard_idle()
    state=read(RUNTIME/'state.json')
    if not matches(state.get('host',{})):raise RuntimeError('No confirmed managed host')
    write(RUNTIME/'control.json',{'token':state['token'],'action':action,'services':services})
    return {'requested':action,'services':services,'host_pid':state['host']['pid']}


def stop_managed():
    """Explicit stop wins over automatic recovery, including delayed task retries."""
    if not (RUNTIME/'task-request.json').exists():
        return {'managed':False}
    guard_idle()
    state=read(RUNTIME/'state.json')
    if matches(state.get('host',{})):
        control('stop',[])
        deadline=time.monotonic()+35
        while matches(state.get('host',{})):
            if time.monotonic()>=deadline:raise RuntimeError('Managed stop did not complete; preserve live services')
            time.sleep(.2)
        latest=read(RUNTIME/'state.json')
        if any(matches(r) for r in latest.get('services',{}).values()):
            raise RuntimeError('A managed service is still active')
    deadline=time.monotonic()+5
    while scheduler('Query').get('state')==4:
        if time.monotonic()>=deadline:raise RuntimeError('Scheduler still reports a running host')
        time.sleep(.2)
    scheduler('Disable')
    return {'managed':True,'stopped':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('host','start','status','stop','stop-if-managed','recycle','remove'))
    parser.add_argument('--service',choices=('backend','frontend'))
    args=parser.parse_args()
    if args.action=='host':return host()
    if args.action=='start':result=start()
    elif args.action=='status':result=read(RUNTIME/'state.json')
    elif args.action in ('stop','stop-if-managed'):result=stop_managed()
    elif args.action=='remove':
        guard_idle()
        state=read(RUNTIME/'state.json')
        if matches(state.get('host',{})) or any(matches(r) for r in state.get('services',{}).values()):
            raise RuntimeError('Stop confirmed services before removing their host task')
        result=scheduler('Remove')
        (RUNTIME/'task-request.json').unlink(missing_ok=True)
    else:
        if args.action=='recycle' and not args.service:parser.error('--service is required')
        result=control(args.action,[args.service] if args.service else [])
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        # Configuration strings and connection credentials never enter diagnostics.
        if 'host' in sys.argv:
            write(RUNTIME/'host-error.json',{'time':stamp(),'error_type':type(exc).__name__})
        if sys.stderr is not None:
            print(json.dumps({'ok':False,'error_type':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(1)
