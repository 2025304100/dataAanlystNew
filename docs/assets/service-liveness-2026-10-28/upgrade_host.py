from pathlib import Path
import hashlib,json,shutil,sys,time
import xml.etree.ElementTree as ET
folder=Path(__file__).resolve().parent
root=folder.parents[1]
applied=json.loads((folder/'applied.json').read_text(encoding='utf-8'))
cases=list(ET.parse(folder/'regression-final-all.xml').getroot().iter('testcase'))
assert len(cases)==46 and not any(c.find(t) is not None for c in cases for t in ('failure','error','skipped'))
shutil.copy2(folder/'applied.json',folder/'applied-initial.json')
for name in ('scripts/validation_service_host.py','tests/test_validation_service_host.py'):
    item=next(i for i in applied['files'] if i['path']==name)
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==item['candidate_sha256']
    if name.startswith('scripts/'):
        shutil.copy2(root/name,folder/'host-before-console-fix.py')
    shutil.copy2(folder/'workspace'/name,root/name)
    item['candidate_sha256']=hashlib.sha256((root/name).read_bytes()).hexdigest()
applied['unique_tests']=47
applied['stages']=[{'junit':'regression-final-all.xml','passed':46},{'junit':'native-complete.xml','passed':1}]
(folder/'applied.json').write_text(json.dumps(applied,ensure_ascii=False,indent=2),encoding='utf-8')
sys.path.insert(0,str(root))
from scripts import validation_service_host as host
from scripts.restore_validation_environment import invariants
before=json.loads((folder/'business-before.json').read_text(encoding='utf-8'))
assert invariants()==before
state=host.read(host.RUNTIME/'state.json')
host.guard_idle()
# Upgrade the first host without weakening its old descendant guard. The new
# controller recognizes only the owned esbuild tree and never kills conhost.
stopper=host.Supervisor(host.specs(),folder/'upgrade-control',existing=dict(state['services']))
stopper.stop_one('frontend')
host.control('stop',[])
deadline=time.monotonic()+25
while host.matches(state['host']):
    assert time.monotonic()<deadline,'Old host did not stop; preserve all processes'
    time.sleep(.2)
deadline=time.monotonic()+5
while host.scheduler('Query')['state']==4:
    assert time.monotonic()<deadline
    time.sleep(.2)
host.scheduler('Disable')
assert invariants()==before
print(json.dumps({'passed':True,'old_host_stopped':True,'business_unchanged':True,'unique_tests':47}))
