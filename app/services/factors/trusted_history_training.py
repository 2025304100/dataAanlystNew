"""Isolated research training from a server-verified immutable history dataset."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

from app.services.factors.store import FactorWarehouse


def load_training_dataset(payload, *, freeze: bool = False):
    from app.services.market_history_evidence import load_trusted_history_dataset

    try:
        dataset = load_trusted_history_dataset(payload.trusted_history_dataset_id)
    except ValueError as exc:
        raise ValueError('trusted_history_dataset_invalid:' + str(exc)) from exc
    if (not payload.train_model or payload.materialize_scores or payload.mirror_only
            or payload.universe_source is not None
            or payload.window_days != 250 or payload.validation_days != 50):
        raise ValueError('trusted_history_requires_training_only_fixed_250_50')
    first, last = dataset.calendar[0], dataset.calendar[-1]
    exchange_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    if last > exchange_now.date() or (
        last == exchange_now.date() and (exchange_now.hour, exchange_now.minute) < (15, 15)
    ):
        raise ValueError('trusted_history_dataset_end_not_closed')
    for key, expected in (('start_date', first), ('end_date', last), ('data_cutoff_date', last)):
        supplied = getattr(payload, key)
        if supplied is not None and supplied != expected:
            raise ValueError('trusted_history_date_range_conflict:' + key)
        setattr(payload, key, expected)
    if len(dataset.calendar) < 274:
        raise ValueError('trusted_history_requires_at_least_274_raw_trade_dates')
    hp = dict(payload.hyperparameters_json or {})
    expected_manifest = hp.get('expected_trusted_history_manifest_sha256')
    if expected_manifest is not None and expected_manifest != dataset.manifest_sha256:
        raise ValueError('trusted_history_reviewed_manifest_changed')
    policy = hp.get('fit_policy', 'cross_sectional_rank_v1')
    if policy != 'cross_sectional_rank_v1':
        raise ValueError('trusted_history_fit_policy_conflict')
    identity = dataset.identity
    if not freeze and hp.get('trusted_history_dataset') != identity:
        raise ValueError('trusted_history_dataset_changed_after_dispatch')
    hp.update(fit_policy=policy, trusted_history_dataset=identity,
              research_evaluation_status='previously_observed_development_comparison')
    payload.hyperparameters_json = hp
    return dataset


def build_training_warehouse(dataset, task_id: str) -> FactorWarehouse:
    """Create a fresh task-local warehouse; never consult the default warehouse."""
    if not isinstance(task_id, str) or re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', task_id) is None:
        raise ValueError('trusted_history_invalid_task_id')
    root = dataset.directory.resolve()
    run_dir = root / 'runs'
    if not run_dir.resolve().is_relative_to(root):
        raise ValueError('trusted_history_run_path_escape')
    run_dir.mkdir(exist_ok=True)
    path = (run_dir / (task_id + '.duckdb')).resolve()
    if path.parent != run_dir.resolve() or path.exists():
        raise ValueError('trusted_history_run_path_conflict')
    warehouse = FactorWarehouse(path)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    records = []
    for index, row in enumerate(dataset.bars.to_dict('records'), 1):
        records.append({
            **row, 'adjust': 'qfq', 'business_symbol_id': None, 'universe_symbol_id': None,
            'source': 'trusted_history:' + dataset.dataset_id,
            'source_origin': 'trusted_history_dataset', 'source_row_id': index,
            'source_updated_at': now, 'ingested_at': now,
            'batch_id': 'trusted-history-' + task_id,
        })
    warehouse.upsert_daily_bars(records, source_key='trusted_history_dataset', watermark=len(records))
    warehouse.upsert_records('raw_asset_universe', [{
        'symbol': symbol, 'name': symbol, 'asset_type': 'stock',
        'market': ('sh' if symbol.startswith('6') else 'bj' if symbol.startswith(('4', '8', '9')) else 'sz'),
        'region': 'cn', 'is_active': True, 'source': 'trusted_history_dataset',
        'source_row_id': index, 'updated_at': now,
    } for index, symbol in enumerate(dataset.symbols, 1)])
    return warehouse


def complete_training_dates(dataset, warehouse, *, start_date: date, end_date: date,
                            threshold: float = .90, as_of: datetime | None = None):
    """Apply the same closed-market and 90% rule to the frozen research cohort."""
    with warehouse.connection(read_only=True) as conn:
        counts = dict(conn.execute('''SELECT trade_date, COUNT(DISTINCT symbol)
            FROM raw_daily_bars WHERE adjust='qfq' GROUP BY trade_date''').fetchall())
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    accepted, skipped = [], []
    expected = len(dataset.symbols)
    for day in dataset.calendar:
        if not start_date <= day <= end_date:
            continue
        observed = counts.get(day, 0)
        closed = day < now.date() or (day == now.date() and (now.hour, now.minute) >= (15, 15))
        ratio = observed / expected if expected else 0.
        if expected and ratio >= threshold and closed:
            accepted.append(day)
        else:
            skipped.append({'trade_date': day, 'observed_symbols': observed,
                'expected_symbols': expected, 'coverage': ratio,
                'reason': 'INCOMPLETE_DAILY_BAR_CROSS_SECTION' if closed else 'DAILY_BAR_NOT_CLOSED'})
    return {'accepted_dates': accepted, 'skipped_dates': skipped,
            'selected_end_date': accepted[-1] if accepted else None,
            'minimum_bar_coverage': threshold, 'denominator': 'frozen_research_cohort',
            'expected_symbols': expected}
