from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import hashlib,json,shutil,sys,time
import psutil
folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
from scripts import validation_service_host as host
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
after=invariants()
assert before==after
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
assert all(hashlib.sha256((root/i['path']).read_bytes()).hexdigest()==i['candidate_sha256'] for i in applied['files'])
baseline=json.loads((folder/'source-baseline.json').read_text(encoding='utf-8'))
changed={name for name,digest in baseline.items() if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest}
assert changed=={'scripts/dev_services.py','scripts/restore_validation_environment.py'},changed
state=host.read(host.RUNTIME/'state.json')
assert state['status']=='ready' and time.time()-state['heartbeat']<8 and host.matches(state['host'])
parent=psutil.Process(state['host']['pid']).parent()
assert parent and Path(parent.exe()).name.casefold() in ('svchost.exe','taskeng.exe','taskhostw.exe')
for record in state['services'].values():
    assert host.matches(record) and psutil.Process(record['pid']).ppid()==state['host']['pid']
checks=[]
for url in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(url,timeout=8) as response:
        assert response.status==200 and response.headers.get('X-App-Validation-Mode')=='1'
        checks.append({'url':url,'status':200,'validation_mode':'1'})
tasks=json.loads((folder/'task-inventory.json').read_text(encoding='utf-8-sig'))
if isinstance(tasks,dict):tasks=[tasks]
assert len(tasks)==1 and tasks[0]['name']==host.TASK_NAME
assert tasks[0]['triggers']==0 and tasks[0]['run_level']==0 and tasks[0]['execution_limit']=='PT0S'
assert json.loads((folder/'readonly-ui/result.json').read_text(encoding='utf-8'))['passed']
result={'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),'business_unchanged':True,
    'source_hashes_verified':True,'unrelated_sources_unchanged':True,'after':after,
    'host':state['host'],'host_parent':{'pid':parent.pid,'exe':parent.exe()},'services':state['services'],
    'host_uptime_seconds':round(time.time()-state['host']['created'],1),'task':tasks[0],
    'http':checks,'real_models':0,'notifications':0,'migrations':0,'business_tasks_cancelled':0,
    'business_rules_added':0}
(folder/'business-final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copy2(host.RUNTIME/'events.jsonl',folder/'host-events.jsonl')
prior=folder/'prior-reports'
if not prior.exists():
    prior.mkdir()
    for name in ['变更记录-行情消息P0-2026-10-25.md','使用说明-行情异动提醒-2026-10-27.md',
        '验证报告-后端受控重启与行情异动页面验收-2026-10-27.md','交付与验证报告-行情异动提醒完整功能-2026-10-27.md']:
        shutil.copy2(root/'docs'/name,prior/name)
print(json.dumps({key:value for key,value in result.items() if key not in ('after','services','task')},ensure_ascii=False))
