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

previous = json.loads((folder / 'business-final.json').read_text(encoding='utf-8'))
assert previous['passed']
current = invariants()
assert current == previous['after'], 'Business state changed since final acceptance'
restore = json.loads((folder / 'runtime-restore/restore.json').read_text(encoding='utf-8'))
identities = {}
for name in ('backend', 'frontend'):
    expected = restore[name]
    process = psutil.Process(expected['pid'])
    assert abs(process.create_time() - expected['created']) < 0.001, name
    assert Path(process.cwd()).resolve() == Path(expected['cwd']).resolve(), name
    assert Path(process.exe()).resolve() == Path(expected['exe']).resolve(), name
    assert process.cmdline() == expected['command'], name
    identities[name] = {'pid': process.pid, 'created': process.create_time()}
checks = []
for uri in ('http://127.0.0.1:8000/health', 'http://127.0.0.1:5173/',
            'http://127.0.0.1:5173/api/v1/market-signals/settings'):
    with build_opener(ProxyHandler({})).open(uri, timeout=8) as response:
        checks.append({'url': uri, 'status': response.status,
                       'validation_mode': response.headers.get('X-App-Validation-Mode')})
assert all(item['status'] == 200 for item in checks)
assert checks[0]['validation_mode'] == checks[2]['validation_mode'] == '1'
applied = json.loads((folder / 'applied.json').read_text(encoding='utf-8'))
for item in applied['files']:
    assert hashlib.sha256((root / item['path']).read_bytes()).hexdigest() == item['candidate_sha256'], item['path']
result = {'passed': True, 'checked_at_utc': datetime.now(timezone.utc).isoformat(),
          'business_state_unchanged_since_acceptance': True, 'processes': identities,
          'http': checks, 'source_hashes_verified': True, 'reads_only': True}
(folder / 'final-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
