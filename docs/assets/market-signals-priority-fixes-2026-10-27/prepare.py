from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import shutil
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
folder = Path(__file__).resolve().parent
workspace = folder / 'workspace'
workspace.mkdir(exist_ok=False)
baseline = {}
for dirname in ('app', 'tests', 'scripts', 'alembic'):
    source = root / dirname
    shutil.copytree(source, workspace / dirname,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.db', '*.duckdb', 'node_modules'))
    for path in source.rglob('*.py'):
        if '__pycache__' not in path.parts:
            baseline[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
for filename in ('pytest.ini', 'alembic.ini'):
    shutil.copy2(root / filename, workspace / filename)
for dirname in ('data', 'tmp'):
    (workspace / dirname).mkdir(exist_ok=True)
shutil.copy2(root / 'docs/assets/market-signals-bug-review-2026-10-27/test_additional_bug_review.py',
             workspace / 'tests/test_market_signal_priority_regressions.py')
(folder / 'baseline.json').write_text(json.dumps({'created_at_utc': datetime.now(timezone.utc).isoformat(),
    'sources': baseline}, indent=2), encoding='utf-8')
from scripts.restart_backend_validation import snapshot
state, _ = snapshot()
(folder / 'business-before.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'workspace': str(workspace), 'source_baselines': len(baseline),
                  'active_async': len(state['active_async']), 'active_discovery': len(state['active_discovery'])}))
