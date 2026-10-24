"""Verify no protected state changed during the UI acceptance."""
from pathlib import Path
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
root = Path(__file__).parent
def read(name): return json.loads((root / name).read_text(encoding='utf-8'))
before, after, browser = read('before.json'), read('after.json'), read('browser-result.json')
assert browser['passed'] and not browser['errors'] and not browser['httpErrors'] and not browser['blockedWrites']
keys = ['tables', 'market_rows', 'main', 'active_tasks', 'auto_trade_tasks', 'automatic_dispatches', 'universe_etf']
comparisons = {key: before[key] == after[key] for key in keys}
assert all(comparisons.values()), comparisons
assert not after['active_tasks']
assert after['audit_max'] == before['audit_max'] + 1 == browser['live_recheck']['audit_event_id']
assert browser['live_recheck']['trade_date'] == '2026-09-30' and browser['live_recheck']['can_confirm'] is False
assert after['main']['last_reconciled_trade_date'] == '2026-09-28'
views = {}
for name, path in {'runtime': '/portfolios/1/inspection/runtime', 'queue': '/portfolios/1/inspection/reconciliation-queue',
    'mode': '/inspection-acceptance/status'}.items():
    with urllib.request.urlopen('http://127.0.0.1:8000/api/v1' + path, timeout=20) as response:
        views[name] = json.load(response)
    (root / ('final-' + name + '.json')).write_text(json.dumps(views[name], ensure_ascii=False, indent=2), encoding='utf-8')
runtime = views['runtime']
assert runtime['permissions'] == {'allow_new_buys': False, 'allow_risk_exits': True, 'allow_auto_recovery': False, 'requires_manual_ack': True}
assert runtime['recovery_summary']['can_confirm'] is False
assert read('before-mode.json') == read('after-mode.json') == views['mode']
result = {'passed': True, 'protected_table_count': len(before['tables']), 'comparisons': comparisons,
    'audit_before': before['audit_max'], 'audit_after': after['audit_max'], 'new_audit_count': 1,
    'only_mutation': 'main / 2026-09-30 explicit reconciliation recheck',
    'verified_through': after['main']['last_reconciled_trade_date'], 'permissions': runtime['permissions'],
    'recovery_can_confirm': False, 'automatic_trading_tasks': len(after['auto_trade_tasks']),
    'automatic_trade_dispatches': len(after['automatic_dispatches']), 'active_tasks': [],
    'trading_resumed': False, 'orders_created': False, 'baseline_accepted': False, 'admission_unfrozen': False,
    'market_data_changed': False, 'model_or_strategy_changed': False, 'backend_restarted': False}
(root / 'safety.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
