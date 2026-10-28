from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,shutil,sys
from urllib.request import build_opener,ProxyHandler
import psutil
from sqlalchemy import create_engine,text

folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
from scripts.restart_backend_validation import snapshot
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
after=invariants()
assert before['content_hashes']==after['content_hashes']
allowed={'market_signal_rules':2,'market_signal_rule_versions':5}
for key,value in before['state']['signal_counts'].items():
    assert after['state']['signal_counts'][key]==value+allowed.get(key,0),key
for key,value in before['state'].items():
    if key not in ('signal_counts','existing_rules','existing_versions'):
        assert after['state'][key]==value,key
for name in ('existing_rules','existing_versions'):
    assert all(after['state'][name][key]==value for key,value in before['state'][name].items())
_,url=snapshot()
engine=create_engine(url,hide_parameters=True,connect_args={'init_command':'SET SESSION TRANSACTION READ ONLY'})
with engine.connect() as connection:
    rules=[]
    for row in connection.execute(text('SELECT id,name,revision,enabled,config_json FROM market_signal_rules ORDER BY id')).mappings():
        item=dict(row)
        config=json.loads(item.pop('config_json'))
        item.update(mode=config['mode'],min_score=config['min_score'],window_days=config['window_days'])
        assert not item['enabled'] and item['mode']=='shadow'
        rules.append(item)
    connection.rollback()
engine.dispose()
restore=json.loads((folder/'runtime-restore/restore.json').read_text(encoding='utf-8'))
for name in ('backend','frontend'):
    p=psutil.Process(restore[name]['pid'])
    assert abs(p.create_time()-restore[name]['created'])<0.001
checks=[]
for uri in ('http://127.0.0.1:8000/health','http://127.0.0.1:5173/','http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(uri,timeout=8) as response:
        checks.append({'url':uri,'status':response.status,'validation_mode':response.headers.get('X-App-Validation-Mode')})
assert all(value['status']==200 for value in checks)
assert checks[0]['validation_mode']=='1' and checks[2]['validation_mode']=='1'
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
assert all(hashlib.sha256((root/item['path']).read_bytes()).hexdigest()==item['candidate_sha256'] for item in applied['files'])
result={'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),'after':after,'rules':rules,
    'allowed_additions':allowed,'content_hashes_unchanged':True,'existing_rules_unchanged':True,
    'real_model_calls':0,'notifications_sent':0,'http':checks,'source_hashes_verified':True}
(folder/'business-final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
prior=folder/'prior-reports'
prior.mkdir(exist_ok=True)
for name in ['变更记录-行情消息P0-2026-10-25.md','使用说明-行情异动提醒-2026-10-27.md',
    '验证报告-后端受控重启与行情异动页面验收-2026-10-27.md']:
    shutil.copy2(root/'docs'/name,prior/name)
print(json.dumps({'passed':True,'content_hashes_unchanged':True,'rule_count':len(rules),
    'allowed_additions':allowed,'http':checks},ensure_ascii=False))
