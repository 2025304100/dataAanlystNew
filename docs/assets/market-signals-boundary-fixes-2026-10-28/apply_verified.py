from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,shutil,sys
import xml.etree.ElementTree as ET
import psutil

folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
baseline=json.loads((folder/'source-baseline.json').read_text(encoding='utf-8'))
files=['app/services/market_signal_engine.py','app/services/market_signal_delivery.py','app/services/market_signals.py',
    'app/services/akshare_guard.py','app/services/akshare_utils.py','tests/test_market_signal_boundary_regressions.py',
    'tests/test_market_signal_boundary_mysql57.py']
stages=[]
identities=set()
for name,total in [('signal-regression.xml',94),('fund-regression/regression.xml',198),
                   ('fund-mysql/mysql.xml',6),('native.xml',5),('ui-and-legacy.xml',8)]:
    document=ET.parse(folder/name).getroot()
    cases=list(document.iter('testcase'))
    assert len(cases)==total,(name,len(cases))
    assert not any(case.find(tag) is not None for case in cases for tag in ('failure','error','skipped')),name
    for case in cases:
        key=(case.attrib.get('classname'),case.attrib['name'])
        assert key not in identities,key
        identities.add(key)
    stages.append({'junit':name,'passed':total})
assert len(identities)==311
for name in files:
    candidate=folder/'workspace'/name
    compile(candidate.read_text(encoding='utf-8-sig'),str(candidate),'exec')
    target=root/name
    if name in baseline:
        assert target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==baseline[name],f'Concurrent edit: {name}'
    else:
        assert not target.exists(),f'New file conflict: {name}'
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
assert invariants()==before,'Business state changed; inspect before deployment'
assert not [c for c in psutil.net_connections(kind='tcp') if c.status==psutil.CONN_LISTEN and c.laddr.port in (8000,5173)],'A service appeared; verify its owner before proceeding'
record={'passed':True,'verified_at_utc':datetime.now(timezone.utc).isoformat(),'unique_tests':311,'stages':stages,'files':[]}
for name in files:
    target=root/name
    if target.exists():
        backup=folder/'live-code-backup'/name
        backup.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(target,backup)
    record['files'].append({'path':name,'baseline_sha256':baseline.get(name),
        'candidate_sha256':hashlib.sha256((folder/'workspace'/name).read_bytes()).hexdigest()})
(folder/'deployment-ready.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
for item in record['files']:
    target=root/item['path']
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(folder/'workspace'/item['path'],target)
    assert hashlib.sha256(target.read_bytes()).hexdigest()==item['candidate_sha256']
record['applied_at_utc']=datetime.now(timezone.utc).isoformat()
(folder/'applied.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps({'applied_files':len(files),'validated_unique_tests':len(identities),'baseline_conflicts':0,'business_unchanged':True}))
