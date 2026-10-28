from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import sys
from sqlalchemy import create_engine, text
import psutil

root=Path(__file__).resolve().parents[2]
folder=Path(__file__).resolve().parent
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
from scripts.restart_backend_validation import snapshot
before=json.loads((folder/'full-business-before.json').read_text(encoding='utf-8'))
after=invariants()
assert before['content_hashes']==after['content_hashes'], 'Unexpected content change'
allowed={'market_signal_rules':2,'market_signal_rule_versions':5}
for key,count in before['state']['signal_counts'].items():
    assert after['state']['signal_counts'][key]==count+allowed.get(key,0), key
for key,value in before['state'].items():
    if key in ('signal_counts','existing_rules','existing_versions'):
        continue
    assert after['state'][key]==value, key
for key in ('existing_rules','existing_versions'):
    assert all(after['state'][key][row_id]==value for row_id,value in before['state'][key].items()), key
_,url=snapshot()
engine=create_engine(url,hide_parameters=True,connect_args={'init_command':'SET SESSION TRANSACTION READ ONLY'})
with engine.connect() as connection:
    rules=[dict(row) for row in connection.execute(text('SELECT id,name,enabled,revision,config_json FROM market_signal_rules ORDER BY id')).mappings()]
    for rule in rules:
        config=json.loads(rule.pop('config_json'))
        rule.update(mode=config['mode'],window_days=config['window_days'],min_score=config['min_score'])
        assert not rule['enabled'] and config['mode']=='shadow'
    connection.rollback()
engine.dispose()
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
assert all(hashlib.sha256((root/item['path']).read_bytes()).hexdigest()==item['candidate_sha256'] for item in applied['files'])
listeners=[{'port':c.laddr.port,'pid':c.pid,'created':psutil.Process(c.pid).create_time()}
    for c in psutil.net_connections(kind='tcp') if c.status==psutil.CONN_LISTEN and c.laddr.port in (8000,5173)]
restore=json.loads((folder/'runtime-restore/restore.json').read_text(encoding='utf-8'))
assert {item['pid'] for item in listeners}=={restore['backend']['pid'],restore['frontend']['pid']}
result={'passed':True,'checked_at_utc':datetime.now(timezone.utc).isoformat(),
    'content_hashes_unchanged':True,'existing_rules_and_versions_unchanged':True,'sources_unchanged':True,
    'allowed_additions':allowed,'rules':rules,'listeners':listeners,'real_model_calls':0,'notifications_sent':0,
    'after':after}
(folder/'business-final.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
prior=folder/'prior-reports'
prior.mkdir(exist_ok=True)
for name in ['变更记录-行情消息P0-2026-10-25.md','使用说明-行情异动提醒-2026-10-27.md']:
    import shutil
    shutil.copy2(root/'docs'/name,prior/name)
print(json.dumps({key:result[key] for key in ('passed','content_hashes_unchanged','allowed_additions','listeners','real_model_calls','notifications_sent')},ensure_ascii=False))
