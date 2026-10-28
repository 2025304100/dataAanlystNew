from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,shutil,sys

root=Path(__file__).resolve().parents[2]
folder=Path(__file__).resolve().parent
workspace=folder/'workspace'
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
before=invariants()
assert not before['state']['active_async'] and not before['state']['active_discovery']
(folder/'business-before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
workspace.mkdir(exist_ok=False)
baseline={}
for name in ('app','tests','scripts','alembic'):
    shutil.copytree(root/name,workspace/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.db','*.duckdb'))
    for path in (root/name).rglob('*.py'):
        if '__pycache__' not in path.parts:
            baseline[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
for name in ('pytest.ini','alembic.ini'):
    shutil.copy2(root/name,workspace/name)
for name in ('data','tmp'):
    (workspace/name).mkdir(exist_ok=True)
shutil.copy2(root/'docs/assets/market-signals-boundary-review-2026-10-28/test_boundary_review_20261028.py',
    workspace/'tests/test_market_signal_boundary_regressions.py')
(folder/'source-baseline.json').write_text(json.dumps(baseline,indent=2),encoding='utf-8')
print(json.dumps({'workspace':str(workspace),'source_baselines':len(baseline),'created_at':datetime.now(timezone.utc).isoformat()}))
