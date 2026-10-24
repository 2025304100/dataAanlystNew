"""Apply the existing cross-provider contract to saved stock receipts; prepare one bounded correction."""
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sys

root = Path(__file__).parent
sys.path.insert(0, str(root.resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8')
from app.services.market_data_sources.sources.sina_history_evidence import parse_sina_history_bundle
from app.services.market_history_contract import history_source_tag
from app.services.market_history_evidence import (
    HistoryRequest, _parse_tencent_response, _validate_normalized, _CROSS_PROVIDER_PRICE_ATOL,
)

preflight = json.loads((root / 'preflight.json').read_text(encoding='utf-8'))
assert _CROSS_PROVIDER_PRICE_ATOL == 0.02
proofs = []
for entry in preflight['score_inputs']:
    if entry['symbol']['asset_type'] != 'stock':
        continue
    code = entry['symbol']['symbol']
    bars = entry['bars'][-50:]
    folder = root / 'rule-input-receipts' / code
    frames = parse_sina_history_bundle(entry['symbol']['market'] + code,
        (folder / 'history.response').read_bytes(), (folder / 'shares.response').read_bytes(),
        (folder / 'qfq.response').read_bytes(), date.fromisoformat(bars[0]['trade_date']),
        date.fromisoformat(preflight['trade_date']), adjust='qfq')
    _validate_normalized(frames)
    sina = {str(row['trade_date'])[:10]: row for row in frames.to_dict(orient='records')}
    tx, mode = _parse_tencent_response(HistoryRequest(entry['symbol']['market'] + code,
        date.fromisoformat(bars[0]['trade_date']), date.fromisoformat(preflight['trade_date']), 'qfq'),
        (root / 'second-source-receipts' / code / 'qfq.response').read_bytes())
    assert mode == 'qfqday'
    tencent = {str(row['date'])[:10]: row for row in tx.to_dict(orient='records')}
    assert set(sina) == {row['trade_date'] for row in bars}
    cross_max = max(abs(float(sina[row['trade_date']][field]) - float(tencent[row['trade_date']][field]))
        for row in bars for field in ['open', 'high', 'low', 'close'])
    assert cross_max <= _CROSS_PROVIDER_PRICE_ATOL + 1e-12, (code, cross_max)
    conflicts = []
    for row in bars:
        max_gap = max(abs(row[field] - float(ref[row['trade_date']][field]))
            for ref in [sina, tencent] for field in ['open', 'high', 'low', 'close'])
        if max_gap > _CROSS_PROVIDER_PRICE_ATOL + 1e-6:
            conflicts.append(row['trade_date'])
    recent_amount_gap = max(abs(row['amount'] - float(sina[row['trade_date']]['amount'])) for row in bars[-20:])
    assert recent_amount_gap <= 0.010001
    correction = None
    if code == '000001':
        assert conflicts == [], conflicts
    elif code == '600428':
        assert conflicts == ['2026-08-07'], conflicts
        original = next(row for row in bars if row['trade_date'] == conflicts[0])
        replacement = dict(original)
        for field in ['open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate']:
            replacement[field] = float(sina[original['trade_date']][field])
            assert math.isfinite(replacement[field]) and replacement[field] >= 0
        replacement['source'] = history_source_tag(frames) + ':qfq'
        assert replacement['source'] == 'akshare:sina:cn-stock:v1:qfq'
        correction = {'before': original, 'after': replacement,
            'reason': 'single_legacy_bar_exceeds_existing_cross_provider_price_contract',
            'independent_tencent_qfq_ohlc': {field: float(tencent[original['trade_date']][field])
                for field in ['open', 'high', 'low', 'close']}}
    else:
        raise AssertionError(code)
    receipt_paths = [*sorted(folder.glob('*.response')), root / 'second-source-receipts' / code / 'qfq.response']
    proof = {'symbol': code, 'symbol_id': entry['symbol']['id'], 'config_id': entry['config']['id'],
        'trade_date': preflight['trade_date'], 'window_start': bars[0]['trade_date'], 'verified_dates': len(bars),
        'existing_price_contract': 'app.services.market_history_evidence._CROSS_PROVIDER_PRICE_ATOL',
        'cross_provider_price_atol_CNY': _CROSS_PROVIDER_PRICE_ATOL, 'cross_provider_max_ohlc_gap': cross_max,
        'recent_amount_max_gap_CNY': recent_amount_gap, 'unresolved_price_conflicts': [],
        'original_bar_conflicts': conflicts, 'correction': correction, 'sina_evidence': frames.attrs['sina_evidence'],
        'receipts': [{'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in receipt_paths], 'network_access': False, 'database_writes': False,
        'pit_historical_availability_certified': False, 'status': 'VERIFIED_AFTER_SPECIFIED_SINGLE_DAY_CORRECTION' if correction else 'VERIFIED_UNCHANGED'}
    proofs.append(proof)
    print(json.dumps({key: proof[key] for key in ['symbol', 'status', 'cross_provider_price_atol_CNY',
        'cross_provider_max_ohlc_gap', 'recent_amount_max_gap_CNY', 'original_bar_conflicts']}, ensure_ascii=False))
(root / 'stock-inputs-final-proof.json').write_text(json.dumps(proofs, ensure_ascii=False, indent=2), encoding='utf-8')
