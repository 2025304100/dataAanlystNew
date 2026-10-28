from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,shutil,sys
import xml.etree.ElementTree as ET
folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
assert invariants()==before,'Business state changed during isolated verification'
stages=[]
identities=set()
for name,count in [('main-regression-final.xml',93),('mysql-final.xml',16),('legacy-regression.xml',206)]:
    cases=list(ET.parse(folder/name).getroot().iter('testcase'))
    assert len(cases)==count
    assert not any(c.find(tag) is not None for c in cases for tag in ('failure','error','skipped'))
    for case in cases:
        identity=case.attrib.get('classname','')+'::'+case.attrib['name']
        assert identity not in identities,identity
        identities.add(identity)
    stages.append({'junit':name,'passed':count})
assert len(identities)==315
names=['app/api/routes/notifications.py','app/services/notifications/outbox.py',
    'app/services/notifications/dispatcher.py','app/services/market_signal_delivery.py',
    'app/services/notifications/delivery_state.py','tests/test_notification_worker_regressions.py',
    'tests/test_notification_worker_mysql57.py','tests/offline_notification_regression_worker.py']
baseline=json.loads((folder/'source-baseline.json').read_text(encoding='utf-8'))
items=[]
for name in names:
    current=root/name
    old=baseline.get(name)
    assert (current.exists() and hashlib.sha256(current.read_bytes()).hexdigest()==old) if old else not current.exists(),name
    source=folder/'workspace'/name
    items.append({'path':name,'baseline_sha256':old,'candidate_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
manifest={'passed':True,'unique_tests':315,'stages':stages,'files':items,
    'verified_at_utc':datetime.now(timezone.utc).isoformat()}
(folder/'deployment-ready.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
for item in items:
    name=item['path']
    current=root/name
    if current.exists():
        backup=folder/'live-code-backup'/name
        backup.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(current,backup)
    current.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(folder/'workspace'/name,current)
    assert hashlib.sha256(current.read_bytes()).hexdigest()==item['candidate_sha256']
manifest['applied_at_utc']=datetime.now(timezone.utc).isoformat()
(folder/'applied.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'passed':True,'unique_tests':315,'files_applied':len(items),'business_unchanged':True}))
