from datetime import datetime, timezone
from pathlib import Path
from urllib.request import build_opener, ProxyHandler
import hashlib
import json
import shutil
import sys

folder = Path(__file__).resolve().parent
root = folder.parents[1]
sys.path.insert(0, str(root))
from scripts.restore_validation_environment import invariants

before = invariants()
(folder / 'business-before.json').write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf-8')
workspace = folder / 'workspace'
workspace.mkdir(exist_ok=False)
hashes = {}
for name in ('app', 'tests', 'scripts'):
    shutil.copytree(root / name, workspace / name,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.db', '*.duckdb'))
    for path in (root / name).rglob('*.py'):
        if '__pycache__' not in path.parts:
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
shutil.copy2(root / 'pytest.ini', workspace / 'pytest.ini')
for name in ('tmp', 'data'):
    (workspace / name).mkdir()
(folder / 'source-baseline.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
checks = []
for url in ('http://127.0.0.1:8000/health', 'http://127.0.0.1:5173/api/v1/market-signals/settings'):
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=8) as response:
            checks.append({'url': url, 'status': response.status, 'validation_mode': response.headers.get('X-App-Validation-Mode')})
    except Exception as exc:
        checks.append({'url': url, 'error_type': type(exc).__name__})
(folder / 'runtime-before.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
print(json.dumps({'workspace': str(workspace), 'source_files': len(hashes),
    'active_async': before['state']['active_async'], 'active_discovery': before['state']['active_discovery'],
    'runtime': checks, 'created_at_utc': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
