from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import shutil
import sys
import xml.etree.ElementTree as ET

folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restart_backend_validation import snapshot
baseline=json.loads((folder/'baseline.json').read_text(encoding='utf-8'))['sources']
files=['app/core/validation_runtime.py','app/main.py','app/api/routes/dashboard.py','app/api/routes/portfolios.py',
    'app/services/market_signal_engine.py','app/services/market_signal_delivery.py','app/services/akshare_utils.py',
    'app/services/akshare_registry.py','app/services/akshare_guard.py','tests/conftest.py',
    'tests/test_market_signal_priority_regressions.py','tests/test_market_signal_priority_mysql57.py',
    'scripts/restore_validation_environment.py']
checks=[]
for filename,expected in [('signal-regression-final.xml',68),('fund-regression-final/regression.xml',198),
        ('fund-mysql-final/mysql.xml',6),('signal-mysql.xml',2),('isolated-ui.xml',1)]:
    document=ET.parse(folder/filename).getroot()
    suites=[document] if document.tag=='testsuite' else list(document.iter('testsuite'))
    counts={key:sum(int(s.attrib.get(key,0)) for s in suites) for key in ('tests','failures','errors','skipped')}
    assert counts=={'tests':expected,'failures':0,'errors':0,'skipped':0}, (filename,counts)
    checks.append({'junit':filename,**counts})
for name in files:
    candidate=folder/'workspace'/name
    compile(candidate.read_text(encoding='utf-8-sig'),str(candidate),'exec')
    target=root/name
    if name in baseline:
        assert target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==baseline[name], f'Concurrent edit: {name}'
    else:
        assert not target.exists(), f'New file already exists: {name}'
state,_=snapshot()
assert not state['active_async'] and not state['active_discovery'], 'Active work; deployment must wait'
import psutil
assert not [c for c in psutil.net_connections(kind='tcp') if c.status==psutil.CONN_LISTEN and c.laddr.port in (8000,5173)], 'Services appeared; verify their identities first'
record={'passed':True,'verified_at':datetime.now(timezone.utc).isoformat(),'regressions':checks,'unique_tests':275,'files':[]}
for name in files:
    target=root/name
    if target.exists():
        backup=folder/'live-code-backup'/name
        backup.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(target,backup)
    item={'path':name,'baseline_sha256':baseline.get(name),
          'candidate_sha256':hashlib.sha256((folder/'workspace'/name).read_bytes()).hexdigest()}
    record['files'].append(item)
(folder/'deployment-ready.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
for item in record['files']:
    target=root/item['path']
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(folder/'workspace'/item['path'],target)
    assert hashlib.sha256(target.read_bytes()).hexdigest()==item['candidate_sha256']
record['applied_at']=datetime.now(timezone.utc).isoformat()
(folder/'applied.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps({'applied':len(files),'validated_unique_tests':275,'baseline_conflicts':0,'processes_stopped':0}))
