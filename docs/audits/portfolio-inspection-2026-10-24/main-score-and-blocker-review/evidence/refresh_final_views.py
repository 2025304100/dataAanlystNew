"""Refresh read-only views after the bounded stock correction and score append."""
import json
from pathlib import Path
import urllib.request

root = Path(__file__).parent / 'final-views'
root.mkdir(exist_ok=False)
for name, path in {
    'mode': '/inspection-acceptance/status',
    'runtime': '/portfolios/1/inspection/runtime',
    'diagnostics': '/portfolios/1/inspection/data-diagnostics',
    'queue': '/portfolios/1/inspection/reconciliation-queue',
    'job': '/portfolios/1/inspection/reconciliation-job',
    'status': '/portfolios/1/status',
    'production-models': '/scoring/models?scope=production&limit=200',
}.items():
    with urllib.request.urlopen('http://127.0.0.1:8000/api/v1' + path, timeout=30) as response:
        result = json.load(response)
    (root / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(name, 'READ')
