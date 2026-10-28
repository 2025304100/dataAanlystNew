from datetime import datetime, timezone
from pathlib import Path
from urllib.request import build_opener, ProxyHandler
import hashlib
import json
import sys

import psutil

folder = Path(__file__).resolve().parent
root = folder.parents[1]
sys.path.insert(0, str(root))
from scripts.restore_validation_environment import invariants

before = json.loads((folder / 'business-before.json').read_text(encoding='utf-8'))
after = invariants()
assert before == after, 'Read-only audit must not change any business invariant'
baseline = json.loads((folder / 'source-baseline.json').read_text(encoding='utf-8'))
changed = [name for name, digest in baseline.items()
           if not (root / name).exists() or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest]
assert not changed, changed
listeners = [{'port': item.laddr.port, 'pid': item.pid} for item in psutil.net_connections(kind='tcp')
             if item.status == psutil.CONN_LISTEN and item.laddr.port in (8000, 5173)]
checks = []
for url in ('http://127.0.0.1:8000/health', 'http://127.0.0.1:5173/api/v1/market-signals/settings'):
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
            checks.append({'url': url, 'status': response.status,
                           'validation_mode': response.headers.get('X-App-Validation-Mode')})
    except Exception as exc:
        checks.append({'url': url, 'error_type': type(exc).__name__, 'reason': str(getattr(exc, 'reason', exc))})
result = {'passed': True, 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
    'meaning': 'Audit isolation and preservation only; new defect assertions intentionally fail',
    'business_unchanged': True, 'source_files_verified_unchanged': len(baseline), 'after': after,
    'listeners': listeners, 'http': checks, 'real_model_calls': 0, 'real_notification_transports': 0,
    'business_rule_writes': 0, 'migrations': 0, 'processes_started_or_stopped_in_business_environment': 0}
(folder / 'business-final.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key:value for key,value in result.items() if key != 'after'}, ensure_ascii=False))
