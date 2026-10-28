from pathlib import Path
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone

root=Path(__file__).resolve().parents[2]
folder=Path(__file__).resolve().parent
workspace=folder/'workspace'
workspace.mkdir(exist_ok=False)
sys.path.insert(0,str(root))
from scripts.restore_validation_environment import invariants
before=invariants()
(folder/'business-before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
for name in ('app','tests','scripts','alembic'):
    shutil.copytree(root/name,workspace/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.db','*.duckdb'))
for name in ('pytest.ini','alembic.ini'):
    shutil.copy2(root/name,workspace/name)
for name in ('tmp','data'):
    (workspace/name).mkdir(exist_ok=True)
files=['app/services/market_signal_engine.py','app/services/market_signal_delivery.py',
    'app/services/market_signals.py','app/services/akshare_guard.py','app/services/akshare_utils.py',
    'app/services/notifications/outbox.py','app/services/notifications/dispatcher.py',
    'app/services/market_event_collection_lock.py','app/services/market_signal_quotes.py']
(folder/'source-baseline.json').write_text(json.dumps({name:hashlib.sha256((root/name).read_bytes()).hexdigest()
    for name in files},indent=2),encoding='utf-8')
print(json.dumps({'workspace':str(workspace),'sources':len(files),'created_at':datetime.now(timezone.utc).isoformat()}))
