"""Verify the exact final change set against the pre-maintenance snapshot."""
import json
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding='utf-8')
root = Path(__file__).parent
def read(name):
    return json.loads((root / name).read_text(encoding='utf-8'))

before, final = read('before.json'), read('final.json')
old, new = read('before-scores.json'), read('final-scores.json')
stock_result = read('stock-score-refresh-result.json')
prior_by_id = {row['id']: row for row in old['scores']}
after_by_id = {row['id']: row for row in new['scores']}
assert all(after_by_id.get(key) == value for key, value in prior_by_id.items())
assert old['scoring_configs'] == new['scoring_configs']
additions = [row for row in new['scores'] if row['id'] not in prior_by_id]
assert {row['id'] for row in additions} == {79581, 79582, 79583}
receipts = read('score-refresh-result.json')['completed'] + stock_result['completed']
for receipt in receipts:
    row = after_by_id[receipt['score_id']]
    assert row['trade_date'] == '2026-10-23' and row['weight_mode'] == 'manual'
    assert row['factor_model_run_id'] is None and row['factor_set_id'] is None
    assert row['pit_safety'] == 'NOT_PIT_SAFE'
    for key in ['quality_score', 'timing_score', 'priority_score']:
        assert row[key] == receipt[key]

comparisons = {key: before[key] == final[key] for key in [
    'tables', 'main', 'active_tasks', 'auto_trade_tasks', 'automatic_dispatches', 'universe_etf']}
assert all(comparisons.values()), comparisons
assert not final['active_tasks'] and len(final['auto_trade_tasks']) == 16 and len(final['automatic_dispatches']) == 66
differences = []
for sid, rows in before['market_rows'].items():
    prior = {row['id']: row for row in rows}
    after = {row['id']: row for row in final['market_rows'][sid]}
    assert prior.keys() == after.keys()
    for key, value in prior.items():
        if value != after[key]:
            differences.append({'symbol_id': int(sid), 'id': key, 'before': value, 'after': after[key]})
assert len(differences) == 1 and differences[0]['symbol_id'] == 3215
correction = stock_result['historical_bar_corrections'][0]
assert differences[0]['before'] == correction['before'] and differences[0]['after'] == correction['after']
assert differences[0]['after']['trade_date'] == '2026-08-07'
assert before['audit_max'] == 3778 and final['audit_max'] == 3796
runtime = read('final-views/runtime.json')
assert runtime['permissions'] == {'allow_new_buys': False, 'allow_risk_exits': True,
    'allow_auto_recovery': False, 'requires_manual_ack': True}
assert runtime['recovery_summary']['can_confirm'] is False
assert final['main']['last_reconciled_trade_date'] == '2026-09-28'
mode = read('final-views/mode.json')
for key in ['background_jobs_enabled', 'automatic_trading_enabled', 'trading_mutations_allowed', 'final_reviews_allowed']:
    assert mode[key] is False
assert read('final-views/production-models.json') == []
reports = read('rechecks/summary.json')
assert len(reports) == 14 and all(not row['can_confirm'] and row['supplement_candidates'] == 0 and row['history_relation'] == 'matched' for row in reports)
for report in reports:
    detail = read('rechecks/' + report['trade_date'] + '-report.json')
    source_by_code = {row['source_code']: row for row in detail['activity_evidence']}
    for name in ['sim_orders', 'sim_trades', 'cash_ledger']:
        assert source_by_code[name]['query_status'] == 'QUERIED' and source_by_code[name]['count'] == 0
scenarios = {row['symbol']: row['scenarios'] for row in read('score-sensitivity.json')}
for receipt in receipts:
    scenario = scenarios[receipt['symbol']]['single_day_aug07_correction_proposal' if receipt['symbol'] == '600428' else 'stored_prices']
    for key in ['quality_score', 'timing_score', 'priority_score']:
        assert receipt[key] == scenario[key]
report = {'passed': True, 'protected_table_count': len(before['tables']), 'comparisons': comparisons,
    'all_original_scores_preserved': len(prior_by_id), 'scoring_configs_unchanged': True,
    'new_scores': receipts, 'verified_bar_corrections': stock_result['historical_bar_corrections'],
    'market_bar_additions': 0, 'market_bar_deletions': 0, 'market_bar_corrections': len(differences),
    'original_000001_and_510300_histories_unchanged': True,
    'successful_sync_steps_rerun': False, 'offline_score_reproduction_passed': True,
    'audit_before': before['audit_max'], 'audit_after': final['audit_max'], 'new_audit_count': 18,
    'rechecked_days': len(reports), 'all_report_history_links_matched': True, 'qualified_supplement_candidates': 0,
    'permissions': runtime['permissions'], 'final_review_can_confirm': False, 'mode': mode,
    'production_ready_models': 0, 'model_or_strategy_changed': False, 'trading_resumed': False,
    'baseline_accepted': False, 'admission_unfrozen': False}
(root / 'final-safety-comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key: value for key, value in report.items() if key not in ['new_scores', 'verified_bar_corrections']}, ensure_ascii=False))
