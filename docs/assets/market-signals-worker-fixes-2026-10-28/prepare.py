from pathlib import Path
import hashlib,json,shutil,sys
root=Path(__file__).resolve().parents[2]
folder=Path(__file__).resolve().parent
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
before=invariants()
assert not before['state']['active_async'] and not before['state']['active_discovery']
(folder/'business-before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
workspace=folder/'workspace'
workspace.mkdir(exist_ok=False)
baseline={}
for name in ('app','tests','scripts'):
    shutil.copytree(root/name,workspace/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.db','*.duckdb'))
    for p in (root/name).rglob('*.py'):
        if '__pycache__' not in p.parts:
            baseline[p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
shutil.copy2(root/'pytest.ini',workspace/'pytest.ini')
for name in ('tmp','data'):
    (workspace/name).mkdir()
audit=root/'docs/assets/market-signals-worker-review-2026-10-28'
for old,new in [('test_worker_delivery_review.py','test_notification_worker_regressions.py'),
                ('test_worker_mysql_review.py','test_notification_worker_mysql57.py'),
                ('offline_delivery_review_worker.py','offline_notification_regression_worker.py')]:
    body=(audit/old).read_text(encoding='utf-8').replace('test_worker_delivery_review','test_notification_worker_regressions').replace('offline_delivery_review_worker','offline_notification_regression_worker')
    (workspace/'tests'/new).write_text(body,encoding='utf-8')
(folder/'source-baseline.json').write_text(json.dumps(baseline,indent=2),encoding='utf-8')
print(json.dumps({'workspace':str(workspace),'baseline_files':len(baseline)}))
