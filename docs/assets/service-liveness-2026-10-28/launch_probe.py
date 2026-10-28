from pathlib import Path
import json,sys,time
folder=Path(__file__).resolve().parent
sys.path.insert(0,str(folder.parents[1]))
from scripts import dev_services
from job_probe import inspect_job
label=sys.argv[1]
assert label in ('default','escalated')
ready=folder/(label+'-child.json')
assert not ready.exists()
parent=inspect_job()
child=dev_services.launch_process([sys.executable,str(folder/'job_probe.py'),str(ready),'300'],
    folder,folder/(label+'.out.log'),folder/(label+'.err.log'))
deadline=time.monotonic()+8
while not ready.exists():
    assert child.poll() is None
    assert time.monotonic()<deadline
    time.sleep(0.05)
value={'parent':parent,'child':json.loads(ready.read_text()),'pid':child.pid}
(folder/(label+'-launch.json')).write_text(json.dumps(value,indent=2),encoding='utf-8')
print(json.dumps(value))
