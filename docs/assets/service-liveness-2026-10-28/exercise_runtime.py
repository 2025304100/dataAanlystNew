from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import hashlib,json,sys,time
import psutil
folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.validation_service_host import RUNTIME,read,control,matches,guard_idle
from scripts.restore_validation_environment import invariants
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
assert invariants()==before
initial=read(RUNTIME/'state.json')
assert initial['status']=='ready' and matches(initial['host'])
host=psutil.Process(initial['host']['pid'])
parent=host.parent()
assert parent is not None
changes=[]
for name in (sys.argv[1:] or ('backend','frontend')):
    assert name in ('backend','frontend')
    guard_idle()
    old=read(RUNTIME/'state.json')['services'][name].copy()
    control('recycle',[name])
    deadline=time.monotonic()+50
    while time.monotonic()<deadline:
        state=read(RUNTIME/'state.json')
        record=state.get('services',{}).get(name,{})
        if (state.get('status')=='ready' and record.get('pid')!=old['pid'] and matches(record)
                and psutil.Process(record['pid']).ppid()==host.pid):
            changes.append({'service':name,'old_pid':old['pid'],'new_pid':record['pid'],
                'created':record['created'],'parent_pid':host.pid})
            break
        time.sleep(.25)
    else:raise RuntimeError('Controlled recycle did not produce an independent managed instance: '+name)
checks=[]
for url in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(url,timeout=8) as response:
        assert response.status==200 and response.headers.get('X-App-Validation-Mode')=='1'
        checks.append({'url':url,'status':200,'validation_mode':'1'})
assert invariants()==before
state=read(RUNTIME/'state.json')
result={'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),
    'host':state['host'],'host_parent':{'pid':parent.pid,'exe':parent.exe()},
    'services':state['services'],'controlled_recycles':changes,'http':checks,
    'business_unchanged':True,'real_models':0,'notifications':0}
(folder/'business-runtime.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
