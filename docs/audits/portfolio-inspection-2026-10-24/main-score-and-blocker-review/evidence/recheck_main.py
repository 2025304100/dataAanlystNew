"""Bounded main reconciliation rechecks and related-history lookup; no final reviews."""
import json
from pathlib import Path
import urllib.request
from urllib.parse import urlencode

root = Path(__file__).parent / 'rechecks'
root.mkdir(exist_ok=True)
base = 'http://127.0.0.1:8000/api/v1'

def save(name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def request(path, body=None):
    if body is not None and not path.startswith('/portfolios/1/reconcile?'):
        raise RuntimeError('Only reconciliation recheck is authorized by this script')
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)

mode = request('/inspection-acceptance/status')
assert mode['mode'] == 'inspection_acceptance'
for key in ['background_jobs_enabled', 'automatic_trading_enabled', 'trading_mutations_allowed', 'final_reviews_allowed']:
    assert mode[key] is False, (key, mode)
save('server-mode.json', mode)
queue = request('/portfolios/1/inspection/reconciliation-queue')
save('queue-before.json', queue)
dates = sorted({day['trade_date'] for day in queue['days']
    if day['status'] != 'PASSED' and not day.get('history_only')})
assert len(dates) == 14 and dates[0] == '2026-09-29' and dates[-1] == '2026-10-23', dates
summary = []
for day in dates:
    report = request('/portfolios/1/reconcile?' + urlencode({'X-User': 'codex-local-safe-recheck'}), {'trade_date': day})
    save(day + '-report.json', report)
    assert report['portfolio_id'] == 1 and report['trade_date'] == day
    assert not report['zero_sum_check_passed'] and not report.get('can_confirm')
    assert report['last_reconciled_trade_date'] == '2026-09-28'
    dispatch = next(f for f in report['activity_evidence'] if f['source_code'] == 'execution_dispatch_history')
    assert dispatch['query_status'] == 'CONTEXT_ONLY' and dispatch['proves_execution'] is False
    supplements = request('/portfolios/1/inspection/evidence-supplements?' + urlencode({'trade_date': day}))
    save(day + '-supplements.json', supplements)
    assert supplements['candidate_count'] == 0 and not supplements['requests']
    history = request('/portfolios/1/inspection/history?' + urlencode({'trade_date_from': day, 'trade_date_to': day,
        'report_ref': report['report_id'], 'report_version': report['report_version']}))
    save(day + '-history.json', history)
    assert history['total'] >= 1 and history['relation_status'] == 'matched'
    fact = {'trade_date': day, 'verdict': report['verdict'], 'reason_codes': report['reason_codes'],
        'report_id': report['report_id'], 'report_version': report['report_version'],
        'dispatch_count': dispatch['count'], 'audit_event_id': report.get('audit_event_id'),
        'supplement_candidates': supplements['candidate_count'], 'history_count': history['total'],
        'history_relation': history['relation_status'], 'can_confirm': report.get('can_confirm'),
        'verified_through': report['last_reconciled_trade_date']}
    summary.append(fact)
    save('summary.json', summary)
    print(json.dumps(fact, ensure_ascii=False), flush=True)
for name, path in {
    'runtime': '/portfolios/1/inspection/runtime', 'queue': '/portfolios/1/inspection/reconciliation-queue',
    'job': '/portfolios/1/inspection/reconciliation-job', 'status': '/portfolios/1/status',
    'diagnostics': '/portfolios/1/inspection/data-diagnostics',
    'production-models': '/scoring/models?scope=production&limit=200',
    'mode': '/inspection-acceptance/status',
}.items():
    save(name + '-after.json', request(path))
print('COMPLETED', len(summary), 'DATES; NO_QUALIFIED_SUPPLEMENT_SOURCES', flush=True)
