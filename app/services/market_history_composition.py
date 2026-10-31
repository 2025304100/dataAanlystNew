"""Immutable union of receipt-verified history, with conflicting keys excluded.

This preserves every source dataset and its research/as-of limitations. It does
not certify historical availability, write business tables or select on returns.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
import shutil

import numpy as np
import pandas as pd

POLICY = 'exclude_nonidentical_symbol_dates_v1'
FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate')
SAFE_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
SHA = re.compile(r'^[a-f0-9]{64}$')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':'), default=str).encode('utf-8')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parents(references):
    from app.services.market_history_evidence import load_trusted_history_dataset
    if (not isinstance(references, list) or not 2 <= len(references) <= 128
            or any(not isinstance(r, dict) or set(r) != {'dataset_id', 'manifest_sha256'} for r in references)):
        raise ValueError('history_composition_invalid_sources')
    loaded = []
    seen = set()
    for ref in references:
        if not SAFE_ID.fullmatch(ref['dataset_id']) or not SHA.fullmatch(ref['manifest_sha256']):
            raise ValueError('history_composition_invalid_source_identity')
        if ref['dataset_id'] in seen:
            raise ValueError('history_composition_duplicate_source')
        seen.add(ref['dataset_id'])
        dataset = load_trusted_history_dataset(ref['dataset_id'])
        manifest = json.loads((dataset.directory/'manifest.json').read_bytes())
        # A flat graph keeps verification bounded and prevents cycles. A future
        # composition must explicitly retain the original leaf references.
        if manifest.get('schema_version') != 1:
            raise ValueError('history_composition_requires_leaf_sources')
        if dataset.manifest_sha256 != ref['manifest_sha256']:
            raise ValueError('history_composition_source_changed')
        loaded.append((manifest['created_at'], dataset))
    return [d for _, d in sorted(loaded, key=lambda item: (item[0], item[1].dataset_id))]


def merge_history_sources(datasets, symbols):
    """Exact comparisons; retain calendar and denominator despite quarantine."""
    symbols = tuple(symbols)
    if (not symbols or symbols != tuple(sorted(set(symbols)))
            or any(not isinstance(s, str) or not re.fullmatch(r'\d{6}', s) for s in symbols)
            or any(not set(d.symbols).issubset(symbols) for d in datasets)):
        raise ValueError('history_composition_cohort_conflict')
    rows, selected, conflicts = {}, {}, {}
    calendar = tuple(sorted({day for dataset in datasets for day in dataset.calendar}))
    for dataset in datasets:
        for row in dataset.bars.itertuples(index=False):
            key = row.symbol, pd.Timestamp(row.trade_date)
            values = tuple(float(getattr(row, f)) for f in FIELDS)
            if not all(np.isfinite(values)):
                raise ValueError('history_composition_nonfinite_source')
            if key in conflicts:
                conflicts[key]['observations'][dataset.dataset_id] = dict(zip(FIELDS, values))
            elif key not in rows:
                rows[key], selected[key] = values, dataset.dataset_id
            elif rows[key] != values:
                conflicts[key] = {'symbol': key[0], 'trade_date': str(key[1].date()),
                    'observations': {selected[key]: dict(zip(FIELDS, rows[key])),
                                     dataset.dataset_id: dict(zip(FIELDS, values))}}
                del rows[key], selected[key]
    if not rows:
        raise ValueError('history_composition_no_uncontested_samples')
    frame = pd.DataFrame([dict(symbol=s, trade_date=d, **dict(zip(FIELDS, values)))
                          for (s, d), values in sorted(rows.items(), key=lambda item: (item[0][1], item[0][0]))])
    if set(frame.trade_date.dt.date) != set(calendar):
        # Never drop an exchange date and thereby change T+1/T+5 horizons.
        raise ValueError('history_composition_whole_calendar_day_quarantined')
    lineage = {}
    for (symbol, day), source in selected.items():
        lineage.setdefault((source, symbol), []).append(str(day.date()))
    quality = {'policy': POLICY, 'performance_based_selection': False,
        'cohort_symbols': list(symbols), 'cohort_size': len(symbols),
        'conflict_rows': len(conflicts), 'conflicts': [conflicts[k] for k in sorted(conflicts)],
        'per_symbol': {s: {'accepted_rows': int(frame.symbol.eq(s).sum())} for s in symbols},
        'selection': [{'dataset_id': source, 'symbol': symbol, 'dates': sorted(days)}
                      for (source, symbol), days in sorted(lineage.items())]}
    return frame, calendar, quality


def write_composed_history(*, dataset_id, references, symbols, plan_id, plan_sha256):
    from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT, load_trusted_history_dataset
    if not SAFE_ID.fullmatch(dataset_id) or not SHA.fullmatch(plan_id) or not SHA.fullmatch(plan_sha256):
        raise ValueError('history_composition_invalid_identity')
    datasets = _parents(references)
    frame, calendar, quality = merge_history_sources(datasets, symbols)
    root = TRUSTED_HISTORY_DATASET_ROOT.resolve()
    destination = (root/dataset_id).resolve()
    if not destination.is_relative_to(root) or destination.exists():
        raise ValueError('history_composition_immutable_conflict')
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.composition-', dir=root))
    try:
        frame.to_parquet(stage/'bars.parquet', index=False)
        (stage/'quality.json').write_bytes(_encode(quality))
        manifest = {'id': dataset_id, 'schema_version': 2, 'research_only': True,
            'retrospective': True, 'point_in_time_certified': False,
            'adjust': 'qfq', 'units': {'volume': 'share', 'amount': 'CNY', 'turnover_rate': 'percentage_point'},
            'bars_path': 'bars.parquet', 'bars_sha256': _sha(stage/'bars.parquet'),
            'quality_path': 'quality.json', 'quality_sha256': _sha(stage/'quality.json'),
            'cohort_symbols': list(symbols), 'calendar': [str(d) for d in calendar],
            'date_range': {'start': str(calendar[0]), 'end': str(calendar[-1])},
            'plan_id': plan_id, 'plan_sha256': plan_sha256,
            'composition': {'policy': POLICY, 'parents': sorted(references, key=lambda r: r['dataset_id'])},
            'created_at': datetime.now(timezone.utc).isoformat()}
        (stage/'manifest.json').write_bytes(_encode(manifest))
        from app.services.market_history_evidence import _commit_staged_directory
        _commit_staged_directory(stage, destination)
    finally:
        if stage.exists():
            assert stage.parent == root and stage.name.startswith('.composition-')
            shutil.rmtree(stage)
    return load_trusted_history_dataset(dataset_id)


def load_composed_history(dataset_id, directory, payload):
    """Recompute the selection and all field values from verified leaf inputs."""
    from app.services.market_history_evidence import TrustedHistoryDataset
    try:
        manifest = json.loads(payload)
        if (manifest['id'] != dataset_id or manifest['schema_version'] != 2
                or manifest['research_only'] is not True or manifest['retrospective'] is not True
                or manifest['point_in_time_certified'] is not False or manifest['adjust'] != 'qfq'
                or manifest['units'] != {'volume': 'share', 'amount': 'CNY', 'turnover_rate': 'percentage_point'}
                or manifest['bars_path'] != 'bars.parquet' or manifest['quality_path'] != 'quality.json'
                or not SHA.fullmatch(manifest['plan_id']) or not SHA.fullmatch(manifest['plan_sha256'])
                or manifest['composition']['policy'] != POLICY):
            raise ValueError('history_composition_invalid_manifest')
        for name in ('bars', 'quality'):
            path = directory/manifest[name+'_path']
            if path.is_symlink() or _sha(path) != manifest[name+'_sha256']:
                raise ValueError('history_composition_content_changed')
        # Reject nested sources before recursion, even if the files were edited.
        from app.services.market_history_evidence import TRUSTED_HISTORY_DATASET_ROOT
        for ref in manifest['composition']['parents']:
            if not SAFE_ID.fullmatch(ref['dataset_id']):
                raise ValueError('history_composition_invalid_source_identity')
            parent_path = TRUSTED_HISTORY_DATASET_ROOT/ref['dataset_id']/'manifest.json'
            if json.loads(parent_path.read_bytes()).get('schema_version') != 1:
                raise ValueError('history_composition_requires_leaf_sources')
        datasets = _parents(manifest['composition']['parents'])
        expected, calendar, quality = merge_history_sources(datasets, manifest['cohort_symbols'])
        actual = pd.read_parquet(directory/'bars.parquet')
        if (manifest['calendar'] != [str(d) for d in calendar]
                or manifest['date_range'] != {'start': str(calendar[0]), 'end': str(calendar[-1])}
                or json.loads((directory/'quality.json').read_bytes()) != quality):
            raise ValueError('history_composition_selection_changed')
        try:
            pd.testing.assert_frame_equal(actual, expected, check_exact=True)
        except AssertionError as exc:
            raise ValueError('history_composition_field_value_mismatch') from exc
        digest = hashlib.sha256(payload).hexdigest()
        identity = {'dataset_id': dataset_id, 'manifest_sha256': digest,
            'bars_sha256': manifest['bars_sha256'], 'cohort_symbols': manifest['cohort_symbols'],
            'research_only': True, 'retrospective': True, 'point_in_time_certified': False,
            'adjustment_asof_certified': False, 'adjust': 'qfq', 'units': manifest['units'],
            'date_range': manifest['date_range'], 'composition': manifest['composition'],
            'conflict_rows_excluded': quality['conflict_rows'],
            'plan_id': manifest['plan_id'], 'plan_sha256': manifest['plan_sha256']}
        return TrustedHistoryDataset(dataset_id, directory, digest, actual,
            tuple(manifest['cohort_symbols']), calendar, identity)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('history_composition_unreadable') from exc
