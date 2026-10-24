"""Offline exact comparison of protected state and append-only scores."""
import json
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding='utf-8')

root = Path(__file__).parent
def read(name):
    return json.loads((root / name).read_text(encoding='utf-8'))
before, after = read('before.json'), read('after.json')
old, new = read('before-scores.json'), read('after-scores.json')
prior_by_id = {row['id']: row for row in old['scores']}
after_by_id = {row['id']: row for row in new['scores']}
assert all(after_by_id.get(key) == value for key, value in prior_by_id.items())
assert old['scoring_configs'] == new['scoring_configs']
additions = [row for row in new['scores'] if row['id'] not in prior_by_id]
assert len(additions) == 1
added = additions[0]
assert added['id'] == 79581 and added['symbol_id'] == 2 and added['trade_date'] == '2026-10-23'
assert added['scoring_config_id'] == 5 and added['weight_mode'] == 'manual'
assert added['factor_model_run_id'] is None and added['factor_set_id'] is None
comparisons = {key: before[key] == after[key] for key in [
    'tables', 'market_rows', 'main', 'active_tasks', 'auto_trade_tasks', 'automatic_dispatches', 'universe_etf']}
assert all(comparisons.values()), comparisons
assert not after['active_tasks'] and len(after['auto_trade_tasks']) == 16 and len(after['automatic_dispatches']) == 66
assert before['audit_max'] == 3778 and after['audit_max'] == 3793
runtime = read('rechecks/runtime-after.json')
assert runtime['permissions'] == {'allow_new_buys': False, 'allow_risk_exits': True,
    'allow_auto_recovery': False, 'requires_manual_ack': True}
assert runtime['recovery_summary']['can_confirm'] is False
mode = read('rechecks/mode-after.json')
for key in ['background_jobs_enabled', 'automatic_trading_enabled', 'trading_mutations_allowed', 'final_reviews_allowed']:
    assert mode[key] is False
assert read('rechecks/production-models-after.json') == []
report = {'passed': True, 'protected_table_count': len(before['tables']), 'comparisons': comparisons,
    'old_scores_preserved': len(prior_by_id), 'scoring_configs_unchanged': True,
    'scores_appended': [{'id': added['id'], 'symbol': '510300', 'trade_date': added['trade_date'],
        'preset_name': added['scoring_preset_name'], 'published_at': added['published_at'],
        'weight_mode': added['weight_mode'], 'pit_safety': added['pit_safety']}],
    'audit_before': before['audit_max'], 'audit_after': after['audit_max'],
    'new_audit_count': after['audit_max'] - before['audit_max'],
    'rechecked_days': len(read('rechecks/summary.json')), 'permissions': runtime['permissions'],
    'final_review_can_confirm': False, 'mode': mode,
    'model_or_strategy_changed': False, 'market_bars_changed': False,
    'accepted_new_baseline': False, 'trading_resumed': False, 'admission_unfrozen': False}
(root / 'safety-comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
