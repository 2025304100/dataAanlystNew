"""Isolated research training from a server-verified immutable history dataset."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

from app.services.factors.store import FactorWarehouse
from app.services.market_history_contract import cn_history_instrument


def validate_training_features(dataset, specs) -> None:
    """Admit only historical price/quantity formulas for initial ETF research."""
    if dataset.identity.get('asset_type') != 'etf':
        return
    allowed = {'open', 'high', 'low', 'close', 'prev_close', 'volume', 'amount'}
    rejected = []
    for spec in specs:
        dependencies = spec.execution_plan.data_dependencies if spec.execution_plan is not None else {}
        fields = set(dependencies.get('fields', ()))
        if (spec.native or not fields or fields - allowed
                or set(spec.source_tables) - {'raw_daily_bars'}
                or spec.missing_policy == 'impute_zero'):
            rejected.append(spec.code)
    if rejected:
        from app.schemas.errors import factor_structured_error
        raise factor_structured_error(
            'ETF_TRAINING_FEATURE_UNSUPPORTED', title_zh='所选特征暂不能用于ETF训练',
            detail_zh='当前ETF训练只使用已核实的历史价格和成交量。股票财务数据、实时指标、未核实的换手率，以及将缺失值补成零的特征，暂不能用于本次训练。',
            impact='训练任务未执行，现有模型和评分保留。',
            fix_link='/settings/factor-center', retryable=False,
            extras={'factor_codes': rejected, 'allowed_fields': sorted(allowed)},
        )


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
    from app.schemas.errors import factor_structured_error
    actual_types = {cn_history_instrument(symbol)['asset_type'] for symbol in dataset.symbols}
    if actual_types == {'etf'} and (
        dataset.identity.get('asset_type') != 'etf'
        or set(dataset.identity.get('etf_price_review', {})) != set(dataset.symbols)
    ):
        raise factor_structured_error(
            'ETF_HISTORY_REVIEW_REQUIRED', title_zh='ETF历史资料需要重新核实',
            detail_zh='这份ETF历史资料缺少完整的品种和分红复权核对记录。合并历史资料后也必须重新核实，本次不启动训练。',
            impact='原始资料保留，现有模型和评分不变。',
            fix_link='/settings/factor-center', retryable=False,
        )
    asset_type = dataset.identity.get('asset_type', 'stock')
    if asset_type not in {'stock', 'etf'}:
        raise factor_structured_error(
            'ETF_TRAINING_SCOPE_INVALID', title_zh='ETF与股票需要分开训练',
            detail_zh='所选历史资料混有ETF和股票，请使用同一品种的历史资料训练。本次未创建任务。',
            impact='历史资料保留，现有模型和评分不变。',
            fix_link='/settings/factor-center', retryable=False,
        )
    scope = dict(payload.factor_scope_json or {})
    if scope.get('asset_type', asset_type) != asset_type:
        if 'etf' in actual_types:
            raise factor_structured_error(
                'ETF_TRAINING_SCOPE_INVALID', title_zh='训练品种与历史资料不一致',
                detail_zh='所选资料属于ETF，不能按股票训练。本次未创建任务，请核对所选资料。',
                impact='历史资料保留，现有模型和评分不变。',
                fix_link='/settings/factor-center', retryable=False,
            )
        raise ValueError('trusted_history_training_asset_type_conflict')
    if asset_type == 'etf':
        from app.services.factors.ridge_model import ModelGate
        minimum_symbols = ModelGate().minimum_symbols
        if len(dataset.symbols) < minimum_symbols:
            raise factor_structured_error(
                'ETF_TRAINING_POOL_TOO_SMALL', title_zh='ETF数量不足，暂不能训练',
                detail_zh=f'当前历史数据包含{len(dataset.symbols)}只ETF；现有模型需要至少{minimum_symbols}只ETF进行同日比较。已保留行情资料，本次不启动训练。',
                impact='历史数据保留，现有模型和评分不变。',
                fix_link='/settings/factor-center', retryable=False,
                extras={'observed_symbols': len(dataset.symbols), 'minimum_symbols': minimum_symbols},
            )
        scope['asset_type'] = 'etf'
        payload.factor_scope_json = scope
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
        'symbol': symbol, 'name': symbol, **cn_history_instrument(symbol),
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
