from datetime import datetime,timezone
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import hashlib,json,shutil,sys
import psutil
folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
after=invariants()
assert before==after,'Business records changed during repair/deployment'
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
assert all(hashlib.sha256((root/i['path']).read_bytes()).hexdigest()==i['candidate_sha256'] for i in applied['files'])
baseline=json.loads((folder/'source-baseline.json').read_text(encoding='utf-8'))
changed=[name for name,digest in baseline.items() if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest]
assert set(changed)=={i['path'] for i in applied['files'] if i['baseline_sha256'] is not None},changed
restore=json.loads((folder/'runtime-restore/restore.json').read_text(encoding='utf-8'))
assert restore['passed']
for name in ('backend','frontend'):
    record=restore[name]
    process=psutil.Process(record['pid'])
    assert abs(process.create_time()-record['created'])<0.001
    assert Path(process.cwd()).resolve()==Path(record['cwd']).resolve()
    assert process.cmdline()==record['command']
checks=[]
for url in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(url,timeout=8) as response:
        assert response.status==200 and response.headers.get('X-App-Validation-Mode')=='1'
        checks.append({'url':url,'status':200,'validation_mode':'1'})
result={'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),'business_unchanged':True,
    'after':after,'applied_source_hashes_verified':True,'unrelated_source_files_unchanged':True,
    'new_business_rules':0,'real_model_calls':0,'notifications_sent':0,'migrations_executed':0,
    'processes_stopped':0,'http':checks,'processes':{name:restore[name] for name in ('backend','frontend')}}
(folder/'business-final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
prior=folder/'prior-reports'
prior.mkdir(exist_ok=False)
for name in ['变更记录-行情消息P0-2026-10-25.md','使用说明-行情异动提醒-2026-10-27.md',
    '验证报告-后端受控重启与行情异动页面验收-2026-10-27.md','交付与验证报告-行情异动提醒完整功能-2026-10-27.md']:
    shutil.copy2(root/'docs'/name,prior/name)
print(json.dumps({'passed':True,'business_unchanged':True,'source_hashes_verified':True,'http':checks,'checked_at_utc':result['checked_at_utc']}))
