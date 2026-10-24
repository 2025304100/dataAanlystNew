"""Offline score sensitivity for disputed historical prices; never a live score or qualification."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sys
from types import SimpleNamespace

root = Path(__file__).parent
sys.path.insert(0, str(root.resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8')
from app.services.scoring_config_engine import (
    _compute_builtin_factors, evaluate_dimension_scores, _aggregate_final_scores, parse_config_json,
)
from app.services.market_data_sources.sources.sina_history_evidence import parse_sina_history_bundle
from app.services.market_history_evidence import HistoryRequest, _parse_tencent_response

facts = json.loads((root / 'preflight.json').read_text(encoding='utf-8'))
refreshed = json.loads((root / 'score-refresh-result.json').read_text(encoding='utf-8'))

def calculate(entry, rows):
    config = parse_config_json(SimpleNamespace(**entry['config']))
    factors = _compute_builtin_factors([SimpleNamespace(**row) for row in rows], SimpleNamespace(**entry['symbol']))
    dims, detail = evaluate_dimension_scores(factors, config)
    quality, timing, priority, stage, action = _aggregate_final_scores(dims, config, factors)
    return {'quality_score': quality, 'timing_score': timing, 'priority_score': priority,
        'stage': stage, 'action': action, 'dimensions': dims, 'reference_only': True}

results = []
for entry in facts['score_inputs']:
    code = entry['symbol']['symbol']
    stored = entry['bars']
    baseline = calculate(entry, stored)
    scenarios = {'stored_prices': baseline}
    if entry['symbol']['asset_type'] == 'etf':
        live = next(row for row in refreshed['completed'] if row['symbol'] == code)
        for key in ['quality_score', 'timing_score', 'priority_score']:
            assert baseline[key] == live[key], (key, baseline, live)
    else:
        start = date.fromisoformat(stored[-50]['trade_date'])
        end = date.fromisoformat(facts['trade_date'])
        folder = root / 'rule-input-receipts' / code
        sina = parse_sina_history_bundle(entry['symbol']['market'] + code,
            (folder / 'history.response').read_bytes(), (folder / 'shares.response').read_bytes(),
            (folder / 'qfq.response').read_bytes(), start, end, adjust='qfq')
        tx, mode = _parse_tencent_response(HistoryRequest(entry['symbol']['market'] + code, start, end, 'qfq'),
            (root / 'second-source-receipts' / code / 'qfq.response').read_bytes())
        assert mode == 'qfqday'
        for label, frame, date_key in [('sina_last50_qfq_prices', sina, 'trade_date'), ('tencent_last50_qfq_prices', tx, 'date')]:
            by_day = {str(row[date_key])[:10]: row for row in frame.to_dict(orient='records')}
            rows = deepcopy(stored)
            for row in rows[-50:]:
                reference = by_day[row['trade_date']]
                for field in ['open', 'high', 'low', 'close']:
                    row[field] = float(reference[field])
            scenarios[label] = calculate(entry, rows)
        if code == '600428':
            rows = deepcopy(stored)
            by_day = {str(row['trade_date'])[:10]: row for row in sina.to_dict(orient='records')}
            row = next(row for row in rows if row['trade_date'] == '2026-08-07')
            for field in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                row[field] = float(by_day['2026-08-07'][field])
            scenarios['single_day_aug07_correction_proposal'] = calculate(entry, rows)
        for label, value in scenarios.items():
            value['change_from_stored'] = {key: round(value[key] - baseline[key], 4)
                for key in ['quality_score', 'timing_score', 'priority_score']}
    result = {'symbol': code, 'name': entry['symbol']['name'], 'trade_date': facts['trade_date'],
        'config_name': entry['config']['name'], 'live_writes': False, 'network_access': False,
        'scenarios': scenarios}
    results.append(result)
    print(json.dumps(result, ensure_ascii=False))
(root / 'score-sensitivity.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
