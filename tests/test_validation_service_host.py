"""Managed service boundary: task policy, owned processes and observable recovery."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.request import build_opener,ProxyHandler
import xml.etree.ElementTree as ET

import psutil
import pytest

from scripts import validation_service_host as host

pytestmark=pytest.mark.whitebox


def test_task_is_on_demand_least_privilege_and_has_no_72_hour_limit():
    definition=host.request_definition()
    xml=ET.fromstring(definition['xml'])
    ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert list(xml.find('t:Triggers',ns))==[]
    assert xml.findtext('t:Principals/t:Principal/t:RunLevel',namespaces=ns)=='LeastPrivilege'
    assert xml.findtext('t:Principals/t:Principal/t:LogonType',namespaces=ns)=='InteractiveToken'
    assert xml.findtext('t:Settings/t:ExecutionTimeLimit',namespaces=ns)=='PT0S'
    assert xml.findtext('t:Settings/t:MultipleInstancesPolicy',namespaces=ns)=='IgnoreNew'
    assert xml.findtext('t:Settings/t:DisallowStartIfOnBatteries',namespaces=ns)=='false'
    assert xml.findtext('t:Settings/t:StopIfGoingOnBatteries',namespaces=ns)=='false'
    assert Path(definition['executable']).name=='pythonw.exe'


def test_task_arguments_quote_the_exact_project_script(monkeypatch,tmp_path):
    root=tmp_path/'project with spaces'
    monkeypatch.setattr(host,'ROOT',root)
    value=host.request_definition()
    assert '"'+str(root/'scripts/validation_service_host.py')+'"' in value['arguments']
    assert value['arguments'].endswith(' host')
    assert 'DATABASE_URL' not in value['xml'] and 'password' not in value['xml']


@pytest.fixture
def service(tmp_path):
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    exit_file=tmp_path/'exit'
    program=tmp_path/'health_server.py'
    program.write_text('''from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
import sys
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'ok')
 def log_message(self,*args):pass
server=HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler)
server.timeout=.1
while not Path(sys.argv[2]).exists():server.handle_request()
server.server_close()
sys.exit(23)
''',encoding='utf-8')
    spec=host.Spec('backend',[sys.executable,str(program),str(port),str(exit_file)],tmp_path,port,dict(os.environ))
    clock=[time.time()]
    supervisor=host.Supervisor([spec],tmp_path/'host',guard=lambda:None,clock=lambda:clock[0])
    try:
        yield supervisor,spec,exit_file,clock
    finally:
        supervisor.stop_one('backend')


def ready(supervisor):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        value=supervisor.tick()
        if value['status']=='ready':return value
        time.sleep(.05)
    pytest.fail('Isolated health server not ready')


def test_guarded_start_never_launches_while_business_work_is_active(tmp_path,monkeypatch):
    spec=host.Spec('backend',['never-executed'],tmp_path,65530,{})
    def busy():raise RuntimeError('Business work active')
    monkeypatch.setattr(host,'listener',lambda _:set())
    def forbidden(*args,**kwargs):raise AssertionError('Must not launch')
    monkeypatch.setattr(host.dev_services,'launch_process',forbidden)
    supervisor=host.Supervisor([spec],tmp_path,guard=busy)
    state=supervisor.tick()
    assert state['status']=='blocked_guard' and state['services']=={}


def test_unknown_port_owner_is_preserved(tmp_path,monkeypatch):
    spec=host.Spec('backend',['never-executed'],tmp_path,65530,{})
    monkeypatch.setattr(host,'listener',lambda _:{123456})
    supervisor=host.Supervisor([spec],tmp_path,guard=lambda:pytest.fail('Must not control foreign owner'))
    assert supervisor.tick()['status']=='blocked_port'
    assert supervisor.records=={}


def test_unexpected_exit_has_exit_code_and_recovers_after_backoff(service):
    supervisor,spec,exit_file,clock=service
    initial=ready(supervisor)
    first=initial['services']['backend'].copy()
    exit_file.touch()
    supervisor.handles['backend'].wait(timeout=5)
    assert supervisor.tick()['status']=='backoff'
    exit_file.unlink()
    clock[0]+=3
    second=ready(supervisor)
    assert second['services']['backend']['pid']!=first['pid']
    events=[json.loads(line) for line in (supervisor.runtime/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(row['event']=='process_exit' and row['exit_code']==23 for row in events)
    with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{spec.port}/health',timeout=2) as response:
        assert response.status==200


def test_temporary_listener_fault_does_not_kill_a_live_owned_process(service,monkeypatch):
    supervisor,_,_,_=service
    initial=ready(supervisor)
    record=initial['services']['backend'].copy()
    monkeypatch.setattr(host,'listener',lambda _:set())
    value=supervisor.tick()
    assert value['status']!='ready'
    assert value['services']['backend']['pid']==record['pid'] and host.matches(record)


def test_pid_reuse_record_never_terminates_the_current_process(service):
    supervisor,_,_,_=service
    initial=ready(supervisor)
    real=initial['services']['backend'].copy()
    supervisor.records['backend']['created']+=100
    supervisor.stop_one('backend')
    assert host.matches(real)
    supervisor.records['backend']=real


def test_recovery_budget_expires_instead_of_blocking_forever(tmp_path,monkeypatch):
    spec=host.Spec('backend',['never-executed'],tmp_path,65530,{})
    clock=[1000.]
    supervisor=host.Supervisor([spec],tmp_path,clock=lambda:clock[0],guard=lambda:pytest.fail('Stop before launch'))
    supervisor.failures['backend']=[999.]*5
    monkeypatch.setattr(host,'listener',lambda _:set())
    assert supervisor.tick()['status']=='backoff'
    # Once the rolling window expires, the normal guarded recovery path runs.
    clock[0]=1400
    supervisor.guard=lambda:(_ for _ in ()).throw(RuntimeError('Guard observed'))
    assert supervisor.tick()['status']=='blocked_guard'


def test_atomic_state_reader_does_not_delete_exit_evidence(tmp_path):
    path=tmp_path/'state.json'
    host.write(path,{'status':'stopped','exit_code':23})
    assert host.read(path)=={'status':'stopped','exit_code':23}
    assert path.exists()


def test_stop_without_a_registered_host_does_not_touch_legacy_services(tmp_path,monkeypatch):
    monkeypatch.setattr(host,'RUNTIME',tmp_path)
    monkeypatch.setattr(host,'guard_idle',lambda:pytest.fail('No managed host to control'))
    assert host.stop_managed()=={'managed':False}


def test_explicit_stop_disables_pending_scheduler_recovery(tmp_path,monkeypatch):
    monkeypatch.setattr(host,'RUNTIME',tmp_path)
    host.write(tmp_path/'task-request.json',{'managed':True})
    host.write(tmp_path/'state.json',{'status':'stopped','services':{}})
    actions=[]
    monkeypatch.setattr(host,'guard_idle',lambda:None)
    def scheduler(action):
        actions.append(action)
        return {'state':3}
    monkeypatch.setattr(host,'scheduler',scheduler)
    assert host.stop_managed()=={'managed':True,'stopped':True}
    assert actions==['Query','Disable']


def test_active_work_prevents_explicit_stop_or_scheduler_changes(tmp_path,monkeypatch):
    monkeypatch.setattr(host,'RUNTIME',tmp_path)
    host.write(tmp_path/'task-request.json',{'managed':True})
    def busy():raise RuntimeError('Active work must not be interrupted')
    monkeypatch.setattr(host,'guard_idle',busy)
    monkeypatch.setattr(host,'scheduler',lambda _:pytest.fail('Do not change scheduler'))
    with pytest.raises(RuntimeError):host.stop_managed()


def test_host_uses_confirmed_node_and_normal_python_with_protection(tmp_path,monkeypatch):
    from sqlalchemy.engine import make_url
    from scripts import restart_backend_validation
    node=tmp_path/'node.exe';node.touch()
    monkeypatch.setattr(host,'RUNTIME',tmp_path)
    host.write(tmp_path/'executables.json',{'node':str(node)})
    monkeypatch.setattr(restart_backend_validation,'snapshot',lambda:({},make_url('sqlite:///:memory:')))
    specs=host.specs()
    assert specs[1].command[0]==str(node)
    assert Path(specs[0].command[0]).name=='python.exe'
    assert specs[0].env['APP_VALIDATION_MODE']=='1'
    assert specs[0].env['AUTO_UNIVERSE_SYNC_ON_STARTUP']=='0'


def test_owned_esbuild_console_companion_is_not_treated_as_a_foreign_service(tmp_path,monkeypatch):
    monkeypatch.setattr(host,'ROOT',tmp_path)
    esbuild=SimpleNamespace(pid=11,exe=lambda:str(tmp_path/'frontend/node_modules/@esbuild/win32-x64/esbuild.exe'))
    console=SimpleNamespace(pid=12,exe=lambda:str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/conhost.exe'),ppid=lambda:11)
    process=SimpleNamespace(children=lambda **_: [esbuild,console])
    assert host.controlled_children(process)==[esbuild]


def test_unrelated_console_is_preserved_even_if_it_has_the_same_filename(tmp_path,monkeypatch):
    monkeypatch.setattr(host,'ROOT',tmp_path)
    console=SimpleNamespace(pid=12,exe=lambda:str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/conhost.exe'),ppid=lambda:999)
    process=SimpleNamespace(children=lambda **_: [console])
    with pytest.raises(RuntimeError):host.controlled_children(process)


def test_normal_start_reuses_registered_protected_host_instead_of_executor_children(tmp_path,monkeypatch):
    monkeypatch.setattr(host,'RUNTIME',tmp_path)
    host.write(tmp_path/'task-request.json',{'managed':True})
    called=[]
    monkeypatch.setattr(host,'start',lambda:called.append('managed'))
    monkeypatch.setattr(host.dev_services,'ensure_runtime_dir',lambda:None)
    monkeypatch.setattr(host.dev_services,'load_state',lambda:{})
    monkeypatch.setattr(host.dev_services,'print_status',lambda _:None)
    monkeypatch.setattr(host.dev_services,'start_backend',lambda _:pytest.fail('Do not launch executor-bound backend'))
    assert host.dev_services.command_start()==0 and called==['managed']


def test_file_entrypoint_can_import_its_host_without_pythonpath_or_project_cwd(tmp_path):
    script=host.ROOT/'scripts/dev_services.py'
    command=('import sys,runpy; '
        f'sys.path.insert(0,{str(script.parent)!r}); runpy.run_path({str(script)!r}); '
        'from scripts import validation_service_host; print("managed-import-ok")')
    env={key:value for key,value in os.environ.items() if key not in ('PYTHONPATH','PYTHONHOME')}
    result=subprocess.run([sys.executable,'-E','-c',command],cwd=tmp_path,env=env,
        capture_output=True,text=True,timeout=8,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='managed-import-ok'
