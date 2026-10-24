"""Read-only protected-state capture for the quick-check UI acceptance."""
from pathlib import Path
import json
import sys
import urllib.request

root = Path(__file__).resolve().parent
repo = root.parents[1]
source = repo / 'tmp/inspection-etf-fallback-2026-10-24/capture_safety.py'
code = source.read_text(encoding='utf-8')
assert code.count('root = Path(__file__).parent') == 1
with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/inspection-acceptance/status', timeout=10) as response:
    mode = json.load(response)
assert mode['mode'] == 'inspection_acceptance'
assert all(mode[key] is False for key in ['background_jobs_enabled', 'automatic_trading_enabled', 'trading_mutations_allowed', 'final_reviews_allowed'])
(root / (sys.argv[1] + '-mode.json')).write_text(json.dumps(mode, ensure_ascii=False, indent=2), encoding='utf-8')
exec(compile(code.replace('root = Path(__file__).parent', 'root = Path(' + repr(str(root)) + ')'), str(source), 'exec'),
    {'__name__': '__main__', '__file__': str(source)})
