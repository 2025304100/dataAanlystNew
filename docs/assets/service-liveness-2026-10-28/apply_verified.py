from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,shutil,subprocess,sys
import xml.etree.ElementTree as ET
folder=Path(__file__).resolve().parent
root=folder.parents[1]
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
assert invariants()==before
stages=[]
for name,count in [('regression-complete-v2.xml',44),('native-complete.xml',1)]:
    cases=list(ET.parse(folder/name).getroot().iter('testcase'))
    assert len(cases)==count and not any(c.find(tag) is not None for c in cases for tag in ('failure','error','skipped'))
    stages.append({'junit':name,'passed':count})
names=['scripts/dev_services.py','scripts/dev_services_admin.ps1','scripts/restore_validation_environment.py',
    'scripts/validation_service_host.py','scripts/validation_service_task.ps1',
    'tests/test_validation_service_host.py','tests/test_validation_service_task_windows.py']
baseline=json.loads((folder/'source-baseline.json').read_text(encoding='utf-8'))
assert subprocess.run(['git','diff','--quiet','--','scripts/dev_services_admin.ps1'],cwd=root).returncode==0
baseline['scripts/dev_services_admin.ps1']=hashlib.sha256((root/'scripts/dev_services_admin.ps1').read_bytes()).hexdigest()
items=[]
for name in names:
    old=baseline.get(name)
    assert (hashlib.sha256((root/name).read_bytes()).hexdigest()==old) if old else not (root/name).exists(),name
    items.append({'path':name,'baseline_sha256':old,
        'candidate_sha256':hashlib.sha256((folder/'workspace'/name).read_bytes()).hexdigest()})
manifest={'passed':True,'unique_tests':45,'stages':stages,'files':items,
    'verified_at_utc':datetime.now(timezone.utc).isoformat()}
for item in items:
    name=item['path']
    if (root/name).exists():
        target=folder/'live-backup'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(root/name,target)
    shutil.copy2(folder/'workspace'/name,root/name)
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==item['candidate_sha256']
(folder/'applied.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
from scripts import validation_service_host as host
write=host.write
write(folder/'reviewable-task-request.json',host.request_definition())
print(json.dumps({'passed':True,'files_applied':len(items),'unique_tests':45,
    'reviewable_task':str(folder/'reviewable-task-request.json'),'business_unchanged':True}))
