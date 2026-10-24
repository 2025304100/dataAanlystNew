"""Retry only the existing 510300 failure under the inspection mutation guard."""
from pathlib import Path
import json
import sys
import time
import uuid
from urllib.request import Request, urlopen

sys.stdout.reconfigure(encoding="utf-8")
root = Path(__file__).parent
base = 'http://127.0.0.1:8000/api/v1'
prior_id = 'ea20e274a59a5dfd8e6544c8739fd38b'

def call(path, payload=None):
    request = Request(base + path, data=json.dumps(payload).encode() if payload else None,
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=20) as response:
        return json.load(response)

def brief(task):
    return {key: task.get(key) for key in ['task_id','portfolio_id','status','message','total','processed','ok_count','failed_count',
        'objects','retryable_operation_ids','parent_task_id','recovery','dimension_recovery','audit_refs']}

mode = call('/inspection-acceptance/status')
assert mode['mode'] == 'inspection_acceptance'
assert not any(mode[key] for key in ['background_jobs_enabled','automatic_trading_enabled','trading_mutations_allowed','final_reviews_allowed'])
prior = call(f'/portfolios/1/inspection/repairs/{prior_id}')
assert prior['status'] == 'partial' and prior['ok_count'] == 2
assert prior['retryable_operation_ids'] == ['2:sync_daily_bars']
target = next(row for row in prior['objects'] if row['operation_id'] == '2:sync_daily_bars')
assert target['symbol'] == '510300' and target['status'] == 'failed'
request_file = root / 'retry-request.json'
if request_file.exists():
    payload = json.loads(request_file.read_text(encoding='utf-8'))
else:
    payload = {'confirmed': True, 'idempotency_key': 'etf-source-failure-' + str(uuid.uuid4())}
    request_file.write_text(json.dumps(payload, indent=2), encoding='utf-8')
task = call(f'/portfolios/1/inspection/repairs/{prior_id}/retry', payload)
assert len(task['objects']) == 1 and task['objects'][0]['symbol'] == '510300'
deadline = time.monotonic() + 60
while task['status'] not in {'done','partial','failed','cancelled'} and time.monotonic() < deadline:
    time.sleep(.7)
    task = call(f"/portfolios/1/inspection/repairs/{task['task_id']}")
after = call(f'/portfolios/1/inspection/repairs/{prior_id}')
assert after['objects'] == prior['objects'], 'The original task was changed'
result = {'mode': mode, 'original': brief(prior), 'retry': brief(task), 'original_unchanged': True,
          'runtime': call('/portfolios/1/inspection/runtime')['permissions']}
(root / 'retry-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'original_unchanged': True, 'retried_symbols': [row['symbol'] for row in task['objects']], 'task': brief(task), 'permissions': result['runtime']}, ensure_ascii=False))
